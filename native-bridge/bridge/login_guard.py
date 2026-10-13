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

A missing state file is a first run. A state file that exists but cannot be
read or parsed fails closed: automatic logins are refused, no writer replaces
the file (pending attributions are held in memory only), status() reports the
problem, and it is logged once per process until the file is repaired or
removed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
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


class StateUnreadable(Exception):
    """The state file exists but cannot be read or parsed. Never treated as
    "no state": that would hand out a fresh login allowance (fail open) and
    let the next write replace the recorded attempts and backoff."""


# Pending fingerprints that could not be written because the state file was
# unreadable: the attribution lives in this process only (see set_pending).
_MEM_PENDING: dict[str, str] = {}
_UNREADABLE_LOGGED = [False]


def _warn_unreadable(err: StateUnreadable) -> None:
    """Log the unreadable state file once per process (stderr reaches the
    per-camera tutk log and the web app's log)."""
    if _UNREADABLE_LOGGED[0]:
        return
    _UNREADABLE_LOGGED[0] = True
    print(f"login guard: {err}; automatic logins are refused and the file is left "
          "untouched until it is repaired or removed", file=sys.stderr, flush=True)


def _load() -> dict:
    """The guard state. A missing file is an empty state (first run); a file
    that exists but cannot be read, is not valid JSON, is not an object or
    holds non-numeric counters raises StateUnreadable."""
    try:
        with open(STATE_PATH) as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise StateUnreadable("cannot read %s: %s" % (
            STATE_PATH, getattr(e, "strerror", None) or e)) from e
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
        for key in ("attempts", "next_allowed"):
            v = d.get(key)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise ValueError(f"{key} is not a number")
    except (ValueError, TypeError) as e:
        raise StateUnreadable("cannot parse %s: %s" % (STATE_PATH, e)) from e
    return d


def _save(d: dict) -> None:
    cs._atomic_write(STATE_PATH, json.dumps(d), mode=0o600)  # noqa: SLF001


def delay_for(attempt: int) -> int:
    """Seconds to wait after the `attempt`-th consecutive login (1-based)."""
    return int(min(BASE * (2 ** max(0, attempt - 1)), MAX_DELAY))


def try_acquire(now: float | None = None) -> tuple[bool, str]:
    """Reserve one automatic login. (True, "") if allowed (already recorded),
    else (False, reason). Fails closed: if the state can't be read or stored,
    no login."""
    now = time.time() if now is None else now
    try:
        with _locked():
            try:
                st = _load()
            except StateUnreadable as e:
                _warn_unreadable(e)
                return False, f"login guard state unreadable ({e})"
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
    fast as the camera reconnects.

    An unreadable state file is left as it is (logged once): rewriting it
    would replace the recorded backoff with a guess."""
    try:
        with _locked():
            try:
                st = _load()
            except StateUnreadable as e:
                _warn_unreadable(e)
                return
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
    previous pending key. Raises OSError if the state can't be stored.

    If the state file exists but is unreadable it is NOT rewritten (that would
    drop the recorded attempts and backoff): the fingerprint is kept in this
    process's memory only, and the problem is logged once."""
    cam = camera or ""
    with _locked():
        try:
            st = _load()
        except StateUnreadable as e:
            _MEM_PENDING[cam] = fingerprint
            _warn_unreadable(e)
            return
        _MEM_PENDING.pop(cam, None)       # superseded by the stored entry
        pend = _pending(st)
        pend[cam] = fingerprint
        st["pending_keys"] = pend
        _save(st)


def record_video(fingerprint: str) -> bool:
    """A session forwarded its first frame with the key `fingerprint`. If that
    is a pending refreshed key, the login that fetched it worked: reset the
    escalation like record_success() (next_allowed untouched) and clear the
    pending entry. Any other key changes nothing. Returns True on a reset.
    Raises OSError if the state can't be read-locked or stored.

    With an unreadable state file nothing is written (logged once); a match
    against a fingerprint held in memory only clears that entry and returns
    False, since no reset could be recorded."""
    with _locked():
        mem_hits = [cam for cam, fp in _MEM_PENDING.items() if fp == fingerprint]
        try:
            st = _load()
        except StateUnreadable as e:
            for cam in mem_hits:
                del _MEM_PENDING[cam]
            _warn_unreadable(e)
            return False
        pend = _pending(st)
        hits = [cam for cam, fp in pend.items() if fp == fingerprint]
        # A fingerprint parked in memory while the file was unreadable still
        # counts once the file is readable again, unless the stored state
        # already has a newer pending key for that camera.
        hits += [cam for cam in mem_hits if cam not in pend]
        for cam in mem_hits:
            del _MEM_PENDING[cam]
        if not hits:
            return False
        for cam in hits:
            pend.pop(cam, None)
        st["pending_keys"] = pend
        st.update(attempts=0, last_ok=time.time())
        _save(st)
        return True


def status() -> dict:
    """Current counters. With an unreadable state file `attempts` and
    `next_allowed` are None (unknown) and `error` says why; automatic logins
    are refused until the file is repaired or removed."""
    try:
        st = _load()
    except StateUnreadable as e:
        return {"attempts": None, "max_attempts": MAX_ATTEMPTS, "next_allowed": None,
                "error": f"login guard state unreadable ({e})"}
    return {"attempts": int(st.get("attempts") or 0), "max_attempts": MAX_ATTEMPTS,
            "next_allowed": st.get("next_allowed") or 0, "error": None}
