#!/usr/bin/env python3
"""
login_guard.py — account-wide brake on automatic Owlet cloud logins.

Every automatic camera-key refresh is a full Firebase *password* login plus a
KMS fetch. Before this guard a camera that was offline, contended or wedged
triggered one per stream (re)start and per failed reconnect — ~150/hour/camera,
and the Owlet KMS answered HTTP 429 to that pattern. The limit lived in each
process's memory, so every go2rtc relaunch reset it.

This keeps the state in one file shared by every stream process (flock'd):

  * each automatic login attempt is recorded BEFORE it is made;
  * the next one is allowed only after an exponential delay:
      OWLET_LOGIN_BACKOFF (60 s) * 2^(attempt-1), capped at
      OWLET_LOGIN_BACKOFF_MAX (21600 s = 6 h);
  * after OWLET_LOGIN_MAX_ATTEMPTS (default 5) attempts without a refreshed key
    that actually delivered video, automatic logins stop completely. The bridge
    keeps connecting with the saved key; only a successful manual "Connect /
    refresh key" in the control panel (or video from a key that an automatic
    refresh just fetched) re-arms it. Re-arming resets the attempt count only:
    the pending interval since the last login still has to elapse.

With the defaults a failure episode costs at most 5 logins, spread over
60+120+240+480 s (~15 min), then none.

"Video from a key that an automatic refresh just fetched" is tracked in the
same file as a *pending* key per camera: when a refresh returns a key that
differs from the one held, its fingerprint (a SHA-256 over uid, authkey and
AV password; never the key itself) is stored under `pending_keys`. It stays
pending across failed sessions and process restarts until a session with
that exact key delivers video (record_video(): attempts -> 0, pending
cleared) or the next changed-key fetch for that camera supersedes it. Video
from any other key (the saved key, another camera's) leaves the guard alone.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time

import config_store as cs

STATE_PATH = os.path.join(cs.SECRETS_DIR, "login-guard.json")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


BASE = _int_env("OWLET_LOGIN_BACKOFF", 60)
MAX_DELAY = _int_env("OWLET_LOGIN_BACKOFF_MAX", 6 * 3600)
MAX_ATTEMPTS = _int_env("OWLET_LOGIN_MAX_ATTEMPTS", 5)


@contextlib.contextmanager
def _locked():
    os.makedirs(os.path.dirname(STATE_PATH), mode=0o700, exist_ok=True)
    fd = os.open(STATE_PATH + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:   # non-POSIX dev box: best effort
            pass
        yield
    finally:
        os.close(fd)


def _load() -> dict:
    try:
        with open(STATE_PATH) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    cs._atomic_write(STATE_PATH, json.dumps(d), mode=0o600)  # noqa: SLF001


def delay_for(attempt: int) -> int:
    """Seconds to wait after the `attempt`-th consecutive login (1-based)."""
    return int(min(BASE * (2 ** max(0, attempt - 1)), MAX_DELAY))


def try_acquire(now: float | None = None) -> tuple[bool, str]:
    """Reserve one automatic login. (True, "") if allowed (already recorded),
    else (False, reason). Fails closed: if the state can't be stored, no login."""
    now = time.time() if now is None else now
    try:
        with _locked():
            st = _load()
            attempts = int(st.get("attempts") or 0)
            if attempts >= MAX_ATTEMPTS:
                return False, (f"automatic login cap reached ({attempts}/{MAX_ATTEMPTS}); "
                               "use 'Connect / refresh key' in the control panel")
            wait = float(st.get("next_allowed") or 0) - now
            if wait > 0:
                return False, f"login backoff, next allowed in {int(wait)}s"
            attempts += 1
            st.update(attempts=attempts, last_attempt=now,
                      next_allowed=now + delay_for(attempts))
            _save(st)
            return True, ""
    except OSError as e:
        return False, f"login guard state unavailable ({e.strerror or e})"


def record_success() -> None:
    """A refreshed key delivered video, or a manual key fetch worked: reset the
    escalation (attempts -> 0, so the cap and the doubling start over).

    `next_allowed` is left untouched: try_acquire() is the only place that
    moves it, so after any automatic login the next one stays blocked for at
    least BASE seconds whatever happens in between. Clearing it here let a
    transient failure, a login and a saved-key session with video repeat as
    fast as the camera reconnects."""
    try:
        with _locked():
            st = _load()
            if st.get("attempts"):
                st.update(attempts=0, last_ok=time.time())
                _save(st)
    except OSError:
        pass


def key_fingerprint(uid, authkey, av_password) -> str:
    """Hex SHA-256 identifying a camera key without storing any of it."""
    def b(v):
        if v is None:
            return b""
        return v if isinstance(v, bytes) else str(v).encode()
    return hashlib.sha256(b"\0".join((b(uid), b(authkey), b(av_password)))).hexdigest()


def _pending(st: dict) -> dict:
    p = st.get("pending_keys")
    return {str(k): v for k, v in p.items() if isinstance(v, str)} if isinstance(p, dict) else {}


def set_pending(camera: str, fingerprint: str) -> None:
    """An automatic refresh fetched a NEW key for `camera`: remember its
    fingerprint until it delivers video. Replaces (supersedes) that camera's
    previous pending key. Raises OSError if the state can't be stored."""
    with _locked():
        st = _load()
        pend = _pending(st)
        pend[camera or ""] = fingerprint
        st["pending_keys"] = pend
        _save(st)


def record_video(fingerprint: str) -> bool:
    """A session forwarded its first frame with the key `fingerprint`. If that
    is a pending refreshed key, the login that fetched it worked: reset the
    escalation like record_success() (next_allowed untouched) and clear the
    pending entry. Any other key changes nothing. Returns True on a reset.
    Raises OSError if the state can't be read-locked or stored."""
    with _locked():
        st = _load()
        pend = _pending(st)
        hits = [cam for cam, fp in pend.items() if fp == fingerprint]
        if not hits:
            return False
        for cam in hits:
            del pend[cam]
        st["pending_keys"] = pend
        st.update(attempts=0, last_ok=time.time())
        _save(st)
        return True


def status() -> dict:
    st = _load()
    return {"attempts": int(st.get("attempts") or 0), "max_attempts": MAX_ATTEMPTS,
            "next_allowed": st.get("next_allowed") or 0}
