#!/usr/bin/env python3
"""
config_store.py — the bridge's settings model + go2rtc stream generation.

Single source of truth, shared by webapp.py (the UI), render_streams.py (boot)
and keepalive.py. It stores **account-level** credentials plus a list of
**cameras**, and from that renders:

  * one env file per camera        -> /config/cameras/<name>.env
  * a generated go2rtc config       -> /config/go2rtc.gen.yaml
    (one `exec:` stream per camera, named by the camera's slug)

Backwards compatible: the old single-camera flat config (region/email/…/uid at
the top level) is migrated into a one-element `cameras` list named `owlet`, so an
existing Frigate URL `rtsp://host:8554/owlet` keeps working untouched.

Stdlib only — the go2rtc YAML is hand-rendered so we don't need PyYAML.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import secrets
import threading

# A WebRTC candidate is "<host-or-ip>:<port>" (v4, bracketed v6, or hostname).
_CAND_RE = re.compile(r"^[\w.\[\]:-]+:\d{1,5}$")


def _tmp_name(path: str) -> str:
    """A temp file name next to `path` that no other writer (thread or
    process) will pick: <name>.tmp-<pid>-<random>."""
    return f"{path}.tmp-{os.getpid()}-{secrets.token_hex(6)}"


def _atomic_write(path: str, text: str, mode: int | None = 0o600) -> None:
    """Write text to path atomically: unique tmp file + fsync + os.replace. A
    crash, SIGKILL, or full disk mid-write can never leave a half-written/empty
    file (which previously could silently wipe the saved config), and two
    concurrent writers never share a temp file. The file is created with
    `mode` (default 0600: everything written here is a credential or derived
    from one) with no world-readable window; mode=None keeps the process umask."""
    tmp = _tmp_name(path)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                 0o666 if mode is None else mode)
    try:
        if mode is not None:
            try:
                os.chmod(tmp, mode)   # umask may have narrowed it; never widened
            except OSError:
                pass
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _sh_squote(v) -> str:
    """Single-quote a value for a sourced shell env file so credential/UI values
    containing $(...), backticks, ;, newlines, etc. can never execute when the
    go2rtc exec sources it with `. <file>`."""
    s = str(v).replace("\n", " ").replace("\r", " ")
    return "'" + s.replace("'", "'\\''") + "'"

# The TUTK license key baked into the Owlet Android app (recovered by decompiling
# it). Pre-filled so a camera connects out of the box.
APP_LICENSE_KEY = (
    "AQAAAGHr2tF3sL8TGR+XirMqZSd8hKY3eBRqKIceLcUSy2okTWYU27qQmwzBORp3tw1yoqiX7l+"
    "yoikFTI+Dzh9M+utHJ/3UBjL8FkYk4kuTSdcE6FtpD3Gidjxnmu2z9TONdpEx15uXvTATqSexOC"
    "GDcldb3xtVXRmH0GoVx9SPKwVPaj7/iYJnPaaURxPzEbEr2Yfd0ckSZoZ8jRH5jxmcJdob"
)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/owlet.yaml")
ENV_PATH = os.environ.get("ENV_PATH", "/config/owlet.env")          # legacy primary env (fallback path)
GEN_PATH = os.environ.get("GO2RTC_GEN", "/config/go2rtc.gen.yaml")  # generated go2rtc config
CONFIG_DIR = os.path.dirname(CONFIG_PATH) or "/config"
CAM_DIR = os.path.join(CONFIG_DIR, "cameras")

# go2rtc listen ports INSIDE the container (host mapping is separate).
G_HTTP = int(os.environ.get("GO2RTC_HTTP_PORT", "1984"))
G_RTSP = int(os.environ.get("GO2RTC_RTSP_PORT", "8554"))
G_WEBRTC = int(os.environ.get("GO2RTC_WEBRTC_PORT", "8555"))

# go2rtc's HTTP API can rewrite its own config (POST /api/config) and re-exec
# itself (POST /api/restart), and an `exec:` source runs arbitrary commands, so
# a reachable API is remote code execution. Bind it to container loopback by
# default: the control panel, keepalive and watchdog all talk to it on
# 127.0.0.1. GO2RTC_API_LISTEN=":1984" opts back into listening on the
# container network (authentication below still applies to non-loopback peers).
G_API_LISTEN = (os.environ.get("GO2RTC_API_LISTEN") or "").strip() or f"127.0.0.1:{G_HTTP}"
# RTSP must be reachable from Frigate over the shared Docker network, so it
# listens on all container interfaces, but with username/password required.
# go2rtc 1.9.x skips the check for loopback peers (internal/rtsp/rtsp.go), so
# the in-container exec publishers, keepalive and watchdog keep working.
G_RTSP_LISTEN = (os.environ.get("GO2RTC_RTSP_LISTEN") or "").strip() or f":{G_RTSP}"
G2_USER_DEFAULT = "owlet"

DEFAULT_CAM_NAME = "owlet"

# Account-level settings (shared by every camera).
ACCOUNT_FIELDS = ["region", "email", "password", "av_account",
                  "iotype_start", "av_channel", "license_key", "region_code",
                  "webrtc_candidate", "ui_user", "ui_pass", "forget_account"]
ACCOUNT_DEFAULTS: dict[str, str] = {
    "region": "world", "email": "", "password": "",
    "av_account": "admin", "iotype_start": "511", "av_channel": "0",
    "license_key": APP_LICENSE_KEY, "region_code": "3",
    "webrtc_candidate": "", "ui_user": "", "ui_pass": "", "forget_account": "",
}

# Per-camera settings.
CAMERA_FIELDS = ["name", "camera_dsn", "uid", "authkey", "av_password",
                 "av_security_mode", "skip_speakerstart", "spk_vol"]
CAMERA_DEFAULTS: dict[str, str] = {k: "" for k in CAMERA_FIELDS}

# Home Assistant / MQTT — settable in the UI (env vars still work as defaults).
MQTT_FIELDS = ["enabled", "host", "port", "user", "password", "prefix"]
MQTT_DEFAULTS: dict[str, str] = {
    "enabled": "", "host": "", "port": "1883", "user": "", "password": "",
    "prefix": "homeassistant",
}

# The proven ffmpeg repackage pipeline (kept identical to the single-cam build):
# no `-fflags nobuffer` (it served codec-less RTSP); analyzeduration/probesize so
# it locks onto the first keyframe; wallclock timestamps so bursty P2P frames
# don't drift/stutter.
FFMPEG = ("ffmpeg -hide_banner -loglevel warning -fflags +genpts "
          "-use_wallclock_as_timestamps 1 -analyzeduration 5000000 "
          "-probesize 5000000 -f h264 -i - -c copy -f rtsp -rtsp_transport tcp {output}")


# --------------------------------------------------------------------------- #
# names
# --------------------------------------------------------------------------- #
def slugify(name: str, fallback: str = DEFAULT_CAM_NAME) -> str:
    """A go2rtc-/URL-safe stream name from a human nickname."""
    s = re.sub(r"[^a-z0-9_-]+", "", (name or "").strip().lower().replace(" ", "_"))
    s = s.strip("-_")
    return s or fallback


def _unique(name: str, used: set[str]) -> str:
    if name not in used:
        return name
    i = 2
    while f"{name}-{i}" in used:
        i += 1
    return f"{name}-{i}"


# --------------------------------------------------------------------------- #
# load / migrate
# --------------------------------------------------------------------------- #
def _empty() -> dict:
    cfg = dict(ACCOUNT_DEFAULTS)
    cfg["cameras"] = []
    cfg["mqtt"] = dict(MQTT_DEFAULTS)
    return cfg


def _migrate_flat(data: dict) -> list[dict]:
    """Old config stored a single camera's fields at the top level — fold them
    into a one-element cameras list named 'owlet' (keeps existing URLs working)."""
    cam = {k: (data.get(k) or "") for k in CAMERA_FIELDS}
    cam["name"] = slugify(data.get("camera_name") or DEFAULT_CAM_NAME)
    has = any(cam.get(k) for k in ("camera_dsn", "uid", "authkey"))
    return [cam] if has else []


def _normalize_cameras(cams) -> list[dict]:
    out: list[dict] = []
    used: set[str] = set()
    for c in cams or []:
        if not isinstance(c, dict):
            continue
        cam = {k: (c.get(k) or "") for k in CAMERA_FIELDS}
        cam["name"] = _unique(slugify(cam.get("name")), used)
        used.add(cam["name"])
        out.append(cam)
    return out


def load_config() -> dict:
    """Return {account fields…, 'cameras': [ {camera fields…}, … ]}."""
    cfg = _empty()
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        raw = open(CONFIG_PATH).read()
    except Exception:  # noqa: BLE001
        return cfg
    data: dict = {}
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw) or {}
        except Exception:  # noqa: BLE001
            # Corrupt JSON: DON'T silently fall through to an empty config (the
            # next save would persist that, wiping every camera). Preserve the
            # original so it can be recovered by hand, then return defaults.
            try:
                os.replace(CONFIG_PATH, CONFIG_PATH + ".bad")
            except OSError:
                pass
            return cfg
    else:
        # tolerate an old "key: value" (yaml-ish) flat config
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, _, v = line.partition(":")
            data[k.strip()] = v.strip().strip("\"'")

    for k in ACCOUNT_FIELDS:
        v = data.get(k)
        # don't let an empty saved value clobber a baked-in default
        if (v is None or v == "") and ACCOUNT_DEFAULTS.get(k):
            continue
        if v is not None:
            cfg[k] = v

    cams = data.get("cameras")
    cfg["cameras"] = _normalize_cameras(cams) if isinstance(cams, list) and cams \
        else _migrate_flat(data)

    mq = data.get("mqtt") if isinstance(data.get("mqtt"), dict) else {}

    def _mqv(k: str):
        v = mq.get(k)
        # port/prefix have meaningful defaults — don't let a blank override them
        if (v is None or v == "") and k in ("port", "prefix"):
            return MQTT_DEFAULTS[k]
        return v if v is not None else MQTT_DEFAULTS[k]
    cfg["mqtt"] = {k: _mqv(k) for k in MQTT_FIELDS}
    return cfg


def mqtt_settings(cfg: dict | None = None) -> dict:
    """Effective MQTT config: UI settings, falling back to env vars. `enabled`
    is true if explicitly enabled in the UI or an OWLET_MQTT_HOST env is set."""
    cfg = cfg or load_config()
    m = dict(MQTT_DEFAULTS)
    m.update(cfg.get("mqtt") or {})
    env = os.environ
    host = m.get("host") or env.get("OWLET_MQTT_HOST", "")
    out = {
        "host": host,
        "port": str(m.get("port") or env.get("OWLET_MQTT_PORT") or "1883"),
        "user": m.get("user") or env.get("OWLET_MQTT_USER", ""),
        "password": m.get("password") or env.get("OWLET_MQTT_PASS", ""),
        "prefix": m.get("prefix") or env.get("OWLET_MQTT_PREFIX") or "homeassistant",
    }
    ui_enabled = str(m.get("enabled")).lower() in ("1", "true", "on", "yes")
    out["enabled"] = bool(host) and (ui_enabled or bool(env.get("OWLET_MQTT_HOST")))
    return out


def find_camera(cfg: dict, name: str) -> dict | None:
    for c in cfg.get("cameras") or []:
        if c.get("name") == name:
            return c
    return None


# --------------------------------------------------------------------------- #
# save + generate
# --------------------------------------------------------------------------- #
def _account_of(cfg: dict) -> dict:
    return {k: cfg.get(k, ACCOUNT_DEFAULTS[k]) for k in ACCOUNT_FIELDS}


class ConfigWriteError(OSError):
    """The config (or the files generated from it) could not be written."""


def _write_config(cfg: dict, regenerate: bool = True) -> None:
    """Persist config (JSON) and optionally regenerate the per-camera envs +
    go2rtc config. Raises ConfigWriteError on any failure."""
    account = _account_of(cfg)
    cameras = _normalize_cameras(cfg.get("cameras"))
    out = dict(account)
    out["cameras"] = cameras
    mq = cfg.get("mqtt")
    if isinstance(mq, dict):
        out["mqtt"] = {k: mq.get(k, MQTT_DEFAULTS[k]) for k in MQTT_FIELDS}
    try:
        os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
        _atomic_write(CONFIG_PATH, json.dumps(out, indent=2))
    except OSError as e:
        raise ConfigWriteError(f"cannot write {CONFIG_PATH}: {e.strerror or e}") from e
    if regenerate:
        _regenerate(cfg)


def _regenerate(cfg: dict) -> None:
    """Rewrite the per-camera envs + go2rtc config from `cfg`. Cheap and
    idempotent. Raises ConfigWriteError on failure."""
    try:
        generate(_account_of(cfg), _normalize_cameras(cfg.get("cameras")))
    except OSError as e:
        raise ConfigWriteError(f"config saved, but the generated stream files could "
                               f"not be written: {e.strerror or e}") from e


def save_config(cfg: dict, regenerate: bool = True) -> bool:
    """Persist `cfg` as-is (under the config lock) and regenerate the
    per-camera envs + go2rtc config. Returns False if the config folder isn't
    writable. For read-modify-write use config_transaction() instead: a
    load_config()/save_config() pair can overwrite a concurrent writer.
    Raises RuntimeError inside an open config_transaction() in this thread:
    the transaction's commit would overwrite what this wrote."""
    if getattr(_CFG_DEPTH, "txn", None) is not None:
        raise RuntimeError("save_config() inside config_transaction(): modify the "
                           "transaction's dict instead")
    try:
        with _config_file_lock():
            _write_config(cfg, regenerate=regenerate)
    except OSError:
        return False
    return True


# One lock for every config writer: a threading.RLock serialises the threads
# of one process (the web UI's request handlers and background workers), and
# fcntl.flock on <config dir>/.owlet.lock serialises processes (the stream
# processes persisting refreshed keys, render_streams at boot, the web UI).
# Nested use in one thread is re-entrant (the flock is taken only once).
_CFG_RLOCK = threading.RLock()
_CFG_DEPTH = threading.local()


@contextlib.contextmanager
def _config_file_lock():
    with _CFG_RLOCK:
        depth = getattr(_CFG_DEPTH, "n", 0)
        fd = None
        if depth == 0:
            try:
                os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
                fd = os.open(os.path.join(CONFIG_DIR, ".owlet.lock"),
                             os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as e:
                raise ConfigWriteError(f"cannot lock the config in {CONFIG_DIR}: "
                                       f"{e.strerror or e}") from e
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:   # non-POSIX dev box: the RLock still applies
                pass
            except OSError as e:
                os.close(fd)
                raise ConfigWriteError(f"cannot lock the config: {e.strerror or e}") from e
        _CFG_DEPTH.n = depth + 1
        try:
            yield
        finally:
            _CFG_DEPTH.n = depth
            if fd is not None:
                os.close(fd)   # releases the flock


def config_lock():
    """Hold the shared config lock (threads + processes, re-entrant per
    thread) without a transaction, for a read-then-act sequence that must not
    interleave with a writer, e.g. load the config and render the files from
    it. config_transaction() may be used inside it."""
    return _config_file_lock()


@contextlib.contextmanager
def config_transaction(regenerate: bool = True):
    """Read-modify-write the config under the shared config lock:

        with config_transaction() as cfg:
            cfg["cameras"].append(...)

    Loads the config, yields the dict, and on a normal exit writes it back
    atomically (unique temp file + os.replace) and, if `regenerate`, rewrites
    the generated env/go2rtc files. If the body raises, nothing is written. If
    the dict was not changed (and a config file already exists), the JSON is
    not rewritten, but with `regenerate` the generated files still are: a
    previous save may have committed the JSON and then failed to render them,
    and re-submitting the same settings must repair that, not report success
    over stale files. Write failures raise ConfigWriteError (an OSError).

    Nesting in one thread joins the open transaction: the inner block gets the
    SAME dict and writes nothing itself; the outermost block commits every
    change (and regenerates if any level asked for it). If an inner block
    raises, its changes are rolled back in the shared dict before the
    exception propagates, so an outer block that catches it commits only its
    own changes."""
    outer = getattr(_CFG_DEPTH, "txn", None)
    if outer is not None:
        cfg = outer["cfg"]
        snapshot = copy.deepcopy(cfg)
        try:
            yield cfg
        except BaseException:
            cfg.clear()
            cfg.update(snapshot)
            raise
        if regenerate:
            outer["regenerate"] = True
        return
    with _config_file_lock():
        cfg = load_config()
        before = copy.deepcopy(cfg)
        existed = os.path.exists(CONFIG_PATH)
        state = {"cfg": cfg, "regenerate": regenerate}
        _CFG_DEPTH.txn = state
        try:
            yield cfg
        finally:
            _CFG_DEPTH.txn = None
        regenerate = state["regenerate"]
        if existed and cfg == before:
            if regenerate:
                _regenerate(cfg)
            return
        _write_config(cfg, regenerate=regenerate)


def forget_account_requested(cfg: dict) -> bool:
    """OWLET_FORGET_ACCOUNT=1 (env) or "forget_account": "1" in owlet.yaml:
    once every camera has a stored key, blank the Owlet email/password so no
    further cloud logins can happen (the bridge then runs on the saved keys)."""
    flag = os.environ.get("OWLET_FORGET_ACCOUNT") or str(cfg.get("forget_account") or "")
    return flag.strip().lower() in ("1", "true", "yes", "on")


def forget_account_if_keyed(cfg: dict) -> bool:
    """Blank email/password in `cfg` (in place) if requested and every camera
    already has a UID + AuthKey. Returns True if something was blanked."""
    cams = cfg.get("cameras") or []
    if not forget_account_requested(cfg) or not (cfg.get("email") or cfg.get("password")):
        return False
    if not cams or not all(c.get("uid") and c.get("authkey") for c in cams):
        return False
    cfg["email"] = ""
    cfg["password"] = ""
    return True


def persist_camera_key(name: str, creds: dict) -> bool:
    """Store a freshly fetched UID/AuthKey/AV password for camera `name` so
    every later (re)start reuses it instead of logging in again. Applies
    forget_account_if_keyed afterwards. Returns True if the config changed.
    Raises ConfigWriteError if the change could not be persisted."""
    with config_transaction(regenerate=True) as cfg:
        cam = find_camera(cfg, name)
        if cam is None or not creds.get("uid"):
            return False
        changed = False
        for k_cfg, k_new in (("uid", "uid"), ("authkey", "authkey"),
                             ("av_password", "av_password")):
            v = creds.get(k_new)
            if v and cam.get(k_cfg) != v:
                cam[k_cfg] = v
                changed = True
        return forget_account_if_keyed(cfg) or changed


def camera_env(account: dict, cam: dict) -> dict[str, str]:
    """The OWLET_* env tutk_client.py reads for one camera."""
    env = {
        "OWLET_REGION": account.get("region", "world"),
        "OWLET_EMAIL": account.get("email", ""),
        "OWLET_PASSWORD": account.get("password", ""),
        "OWLET_AV_ACCOUNT": account.get("av_account", "admin"),
        "OWLET_IOTYPE_START": account.get("iotype_start", "511"),
        "OWLET_AV_CHANNEL": account.get("av_channel", "0"),
        "OWLET_LICENSE_KEY": account.get("license_key", APP_LICENSE_KEY),
        "OWLET_REGION_CODE": account.get("region_code", "3"),
        "OWLET_CAMERA_DSN": cam.get("camera_dsn", ""),
        "OWLET_UID": cam.get("uid", ""),
        "OWLET_AUTHKEY": cam.get("authkey", ""),
        "OWLET_AV_PASSWORD": cam.get("av_password", ""),
        "OWLET_CAMERA_NAME": cam.get("name", ""),
    }
    if cam.get("av_security_mode"):
        env["OWLET_AV_SECURITY_MODE"] = cam["av_security_mode"]
    if cam.get("skip_speakerstart"):
        env["OWLET_SKIP_SPEAKERSTART"] = "1"
    if str(cam.get("spk_vol", "")).strip() != "":
        env["OWLET_SPK_VOL"] = str(cam["spk_vol"]).strip()
    return env


def _write_env(path: str, env: dict[str, str]) -> None:
    lines = ["# generated by owlet-bridge — do not edit (rewritten on save)"]
    for k, v in env.items():
        lines.append(f"{k}={_sh_squote(v)}")
    _atomic_write(path, "\n".join(lines) + "\n")


def _exec_source(name: str) -> str:
    """go2rtc `exec:` source for one camera: source its env, set up the talk +
    audio FIFOs, run tutk_client (H.264 -> stdout) piped to ffmpeg, repackaged
    to RTSP.

    THE VIDEO PIPELINE IS UNCHANGED from the proven :latest build: still one
    ffmpeg with a single H.264 stdin input and `-c:v copy`. Camera audio is NOT
    muxed here. It used to be attempted as a second `-i <fifo>` on this ffmpeg,
    which is what the old "VIDEO-ONLY, audio intentionally omitted" note was
    about, and there are two independent reasons never to go back to that:

      * a named-pipe input blocks ffmpeg until a writer appears, delaying (or
        preventing) the RTSP publish, so consumers get "Invalid data found
        when processing input" — i.e. no picture; and
      * `_audio_probe` STOPS writing while talk-back is active (half-duplex is
        mandatory: simultaneous TUTK send+recv wedges the camera's single AV
        session). A starved audio input in the same mux makes ffmpeg hold video
        back on DTS interleave, so every press of "Talk" would hitch the
        picture.

    Instead the audio rides its own producer (`_audio_source`) which go2rtc
    merges into the same stream. If audio never starts, starves, or dies, the
    video producer neither knows nor cares — go2rtc just serves video.

    This function's only audio-related job is to hand tutk_client the FIFO to
    write into (OWLET_AUDIO_FIFO) and to own that FIFO's lifecycle, exactly as
    it already does for the talk FIFO.

    OWLET_TALK_FIFO is still set up so the web-panel "Talk" / lullaby buttons
    work (webapp writes PCM → FIFO → tutk_client sends to camera).

    The ffmpeg flags match the PROVEN :latest pipeline:
      * `-fflags +genpts` — NOT `nobuffer`. `nobuffer` emits RTSP before the
        H.264 SPS/PPS is parsed, so every consumer decodes nothing.
      * generous analyzeduration/probesize (5s/5MB) to lock onto the first IDR.
      * wallclock timestamps so bursty P2P frames don't drift/stutter."""
    envf = "/config/cameras/%s.env" % name
    logf = "/config/tutk-%s.log" % name
    cmd = (
        # OWLET_PRODUCER=<slug> is the wrapper's identity marker: it sits in
        # this bash's argv (and is exported to its children), so
        # watchdog.stop_all_producers can find EVERY running stream wrapper in
        # the process table without knowing the camera's (possibly renamed or
        # deleted) config name.
        'set -a; OWLET_PRODUCER=%(n)s; [ -f %(e)s ] && . %(e)s; '
        'D="${TMPDIR:-/tmp}"; mkdir -p "$D" 2>/dev/null; '
        'T="$D/owlet-talk-%(n)s"; rm -f "$T"; mkfifo "$T" 2>/dev/null; '
        # Audio FIFO: tutk_client writes the camera's ADTS AAC frames here and
        # the _audio_source producer reads them. Created before python starts so
        # the audio producer finds it immediately.
        #
        # Deliberately UNLIKE the talk FIFO: created only if absent, and never
        # removed (not here, not in the trap). The audio reader can be parked in
        # open() waiting for the first frame, and rm+mkfifo would swap the inode
        # underneath it -- leaving that ffmpeg blocked forever on an unlinked
        # pipe while tutk_client writes to a new one nobody reads, i.e. audio
        # silently dead until the container restarts. A stable path keeps every
        # relaunch pointing at the same pipe. Leftover bytes are not a hazard:
        # the probe only ever writes whole ADTS frames and the demuxer resyncs
        # on the 0xFFFx sync word.
        'A="$D/owlet-audio-%(n)s"; [ -p "$A" ] || mkfifo "$A" 2>/dev/null; '
        'V="$D/owlet-vol-%(n)s"; '
        '[ -n "$OWLET_SPK_VOL" ] && printf "%%s" "$OWLET_SPK_VOL" > "$V"; '
        'C="$D/owlet-audiocmd-%(n)s"; R="$D/owlet-audioresp-%(n)s"; rm -f "$C" "$R"; '
        'mkdir -p /config/vitals 2>/dev/null; '
        'export OWLET_TALK_FIFO="$T" OWLET_AUDIO_FIFO="$A" OWLET_VOL_FILE="$V" '
        'OWLET_AUDIOCMD_FILE="$C" OWLET_AUDIORESP_FILE="$R" '
        'OWLET_CAM_SENSORS="/config/vitals/cam-%(n)s.json"; '
        # $A (audio FIFO) is intentionally NOT cleaned up here -- see above.
        'trap "rm -f $T $V $C $R" EXIT; '
        # Process supervision. go2rtc stops an exec source by signalling ONLY
        # this bash (no process group), so a foreground pipeline here used to
        # leave tutk_client + ffmpeg orphaned, still holding the camera's
        # single P2P slot. The pipeline therefore runs in the background with
        # its two PIDs recorded (F = ffmpeg, P = tutk_client, the job leader),
        # and TERM/INT/HUP forward SIGTERM to both (tutk_client releases the
        # session cleanly), wait up to 8 s, SIGKILL whatever is left, exit.
        # go2rtc sends SIGTERM (killsignal=15) and only SIGKILLs this shell
        # after killtimeout=12 s, so the handler always finishes first.
        # Explicit PIDs, not `kill %1`: bash spins forever in `kill -KILL %1`
        # once one member of the job has already exited (seen in testing).
        'python3 /app/tutk_client.py 2>>%(l)s | '
        'ffmpeg -hide_banner -loglevel warning -fflags +genpts '
        '-use_wallclock_as_timestamps 1 -analyzeduration 5000000 -probesize 5000000 -f h264 -i - '
        '-c:v copy -f rtsp -rtsp_transport tcp {output} & '
        'F=$!; P=$(jobs -p); '
        '_stop() { trap - TERM INT HUP; kill -TERM $P $F 2>/dev/null; '
        'for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; '
        'do { kill -0 $P || kill -0 $F; } 2>/dev/null || break; sleep 0.5; done; '
        'kill -KILL $P $F 2>/dev/null; exit 143; }; '
        'trap _stop TERM INT HUP; wait'
    ) % {"e": envf, "l": logf, "n": slugify(name)}
    # go2rtc parses "#key=value" options after the command (internal/exec);
    # no space before '#', or YAML would read it as a comment.
    return "exec:bash -c '" + cmd + "'" + EXEC_KILL_OPTS


# SIGTERM (15) to the wrapper, SIGKILL only after 12 s (go2rtc 1.9.x
# internal/exec/closer.go: killsignal / killtimeout).
EXEC_KILL_OPTS = "#killsignal=15#killtimeout=12"


def audio_filter() -> str:
    """The ffmpeg `-af` chain for the camera audio (always non-empty).

    1. `aresample=async=1:first_pts=0` -- the audio input is stamped with the
       WALL-CLOCK arrival time of each 128 ms AAC frame, and the camera's P2P
       delivery is bursty, so raw timestamps come out jittery; encoded to Opus
       as-is that reaches the phone as irregular RTP timing and the jitter
       buffer stretches/drops constantly ("choppy", reported 2026-09-23 the
       moment audio first became audible). The async resampler turns the
       bursts into one continuous, regularly-timestamped sample stream, and
       fills a real gap (talk-back pauses the mic, half-duplex) with silence
       instead of letting the timeline skew.
    2. gain: the cam's mic is honest but quiet -- measured with someone
       talking in the room, speech peaked at only -16..-22 dBFS over a
       -45..-55 dBFS floor, far below a comfortable phone level. Default
       +10 dB, then a brickwall limiter so a cry can't clip.
       OWLET_AUDIO_GAIN_DB overrides; 0 drops the gain stage (not the
       resampler)."""
    raw = (os.environ.get("OWLET_AUDIO_GAIN_DB") or "10").strip()
    try:
        gain = float(raw)
    except ValueError:
        gain = 10.0
    chain = ["aresample=async=1:first_pts=0"]
    if gain != 0:
        chain.append("volume=%gdB" % gain)
        chain.append("alimiter=limit=0.95")
    # NB: colons inside filter args are fine; a ": " (colon-space) is not --
    # these land in an unquoted YAML scalar (see _audio_source).
    return "-af " + ",".join(chain) + " "


def _audio_source(name: str) -> str:
    """A SECOND go2rtc producer for the same stream, carrying ONLY the camera's
    audio. go2rtc merges the tracks of every source listed under a stream, so
    `owlet: [_exec_source, _audio_source]` serves one H.264 + one AAC track.

    Why a separate producer instead of a second input on the video ffmpeg: see
    _exec_source. In short, this shape makes audio failure structurally unable
    to touch the picture.

    The camera sends AAC-LC 8 kHz mono in ADTS frames (codec_id 0x0088,
    768-byte frames every 128 ms), which `_audio_probe` writes whole into the
    FIFO, so the input side is a plain `-f aac` (ADTS demuxer) read.

    The output is transcoded to Opus, NOT copied, and that is not optional:
    ffmpeg's RTSP muxer refuses AAC that arrived as ADTS --

        [rtsp] AAC with no global headers is currently not supported.
        Could not write header (incorrect codec parameters ?)

    ADTS carries its config per frame instead of as global extradata, and the
    muxer needs that extradata at write_header time. `-bsf:a aac_adtstoasc`
    cannot rescue it either: the filter only derives extradata once the first
    packet flows, which is after the header is due. Copying ADTS is what made
    the first cut of this producer die on launch (the FIFO opened and closed
    three times, and the stream stayed video-only).

    Opus is also what the consumer actually wants: WebRTC cannot carry AAC at
    all, so something has to produce Opus somewhere. Doing it here costs
    ~nothing (8 kHz mono, resampled to Opus' required 48 kHz) and means Frigate
    needs no ffmpeg wrapper for this camera. Frigate's recorder already runs
    `-c:a aac`, so mp4 recordings still get AAC.

    Lifecycle notes:
      * The FIFO is created by the video exec. We wait for it to appear rather
        than creating it, so there is exactly one owner and no rm/mkfifo race
        that could leave this reader holding a stale inode. The wait is capped
        well inside go2rtc's launch window; the gap is normally milliseconds,
        since mkfifo runs before tutk_client starts.
      * `-i "$A"` blocks in open() until tutk_client writes its first frame,
        which happens moments after the video's first frame (AUDIOSTART is sent
        right after IOTYPE_START), so both producers publish at about the same
        time. A blocked reader still counts as a reader, so tutk_client's
        non-blocking O_WRONLY open succeeds and neither side deadlocks.
      * When the video exec is relaunched it rm's the FIFO, this ffmpeg sees
        EOF and exits, and go2rtc restarts it against the fresh pipe.
      * Wallclock timestamps, same as the video input, so the two producers
        share one arrival-stamped clock and stay in sync."""
    name = slugify(name)   # defense in depth: never interpolate a raw name into bash
    cmd = (
        'D="${TMPDIR:-/tmp}"; A="$D/owlet-audio-%(n)s"; '
        # Wait (up to ~10s) for the video exec's mkfifo. Deliberately no counter
        # variable and no arithmetic: go2rtc expands ${...} when it loads the
        # config (that is how D= resolves to the Termux tmpdir) while leaving bare
        # $VAR alone, so a $i / $((i+1)) here would be substituted away and break
        # the loop. A literal word list is substitution-proof.
        'for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; '
        'do [ -p "$A" ] && break; sleep 0.5; done; '
        # Still no FIFO means the video exec isn't up. Exit non-zero and let
        # go2rtc retry rather than sitting here holding a producer slot open.
        #
        # NB: no ": " (colon-space) anywhere in this command. These sources are
        # emitted as UNQUOTED YAML scalars, so a colon-space makes the YAML
        # parser read the line as a MAPPING instead of a string -- which breaks
        # the stream, and can take neighbouring config with it. Caught in review
        # when the round-trip assertion below started parsing this as a dict.
        '[ -p "$A" ] || { echo "audio %(n)s - FIFO never appeared" >&2; exit 1; }; '
        'exec ffmpeg -hide_banner -loglevel warning -fflags +genpts '
        '-use_wallclock_as_timestamps 1 -f aac -i "$A" '
        + audio_filter() +
        # lowdelay + 48k mono: Opus' RTP clock rate is always 48 kHz.
        '-c:a libopus -b:a 24k -ar 48000 -ac 1 -application lowdelay '
        '-f rtsp -rtsp_transport tcp {output}'
    ) % {"n": name}
    return "exec:bash -c '" + cmd + "'"


def _overlay_source(name: str) -> str:
    """A SEPARATE on-demand stream `<name>_overlay`: pull the already-running
    clean stream from go2rtc and burn the glass HUD (overlay text file, written
    by vitals_poller) into it. Costs nothing until something actually views it;
    the clean `<name>` stream is never touched. Uses h264_nvenc if the runtime
    has it, else libx264 -tune zerolatency."""
    name = slugify(name)  # defense in depth: never interpolate a raw name into bash
    src = "rtsp://127.0.0.1:%s/%s" % (G_RTSP, name)
    cmd = (
        'OV="/config/vitals/overlay-%(n)s.txt"; mkdir -p /config/vitals 2>/dev/null; '
        '[ -f "$OV" ] || printf " " > "$OV"; '
        'VENC="-c:v libx264 -preset veryfast -tune zerolatency -g 60 -pix_fmt yuv420p"; '
        'ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_nvenc && '
        'VENC="-c:v h264_nvenc -preset p2 -tune ll -g 60"; '
        'exec ffmpeg -hide_banner -loglevel warning -fflags nobuffer -flags low_delay '
        '-rtsp_transport tcp -i %(src)s '
        # The filter MUST be double-quoted: `x=(w-tw)/2` contains parentheses,
        # which are bash metacharacters. Unquoted, `bash -c` refused the whole
        # command with "syntax error near unexpected token `('", so this stream
        # never started (go2rtc answered every DESCRIBE with 404). Caught on
        # 2026-09-23 from go2rtc's /api/log. `expansion=none` stops drawtext
        # treating the HUD's "% RH" as a %-expansion ("Stray %" warning per
        # frame); the text never uses %{...} functions.
        '-filter:v "drawtext=fontfile=/app/fonts/DejaVuSans.ttf:textfile=$OV:reload=1'
        ':expansion=none'
        ':fontcolor=white:fontsize=h/26:line_spacing=6:box=1:boxcolor=black@0.40:boxborderw=18'
        ':shadowcolor=black@0.6:shadowx=2:shadowy=2:x=(w-tw)/2:y=h-th-(h/16)" '
        '$VENC -c:a copy -muxdelay 0 -muxpreload 0 -f rtsp -rtsp_transport tcp {output}'
    ) % {"n": name, "src": src}
    return "exec:bash -c '" + cmd + "'"


SECRETS_DIR = os.path.join(CONFIG_DIR, "secrets")
G2_SECRET_PATH = os.path.join(SECRETS_DIR, "go2rtc.json")


def _yaml_str(v: str) -> str:
    """A double-quoted YAML scalar (JSON strings are valid YAML)."""
    return json.dumps(str(v))


def go2rtc_credentials(create: bool = True) -> tuple[str, str]:
    """(username, password) that go2rtc requires on its API and RTSP server
    for every non-loopback client.

    Each half is resolved on its own:
      * password: GO2RTC_PASS if set, else a random one generated once and
        kept in <config>/secrets/go2rtc.json (mode 0600), so Frigate's URL
        stays valid across restarts;
      * username: GO2RTC_USER if set, else the one stored with the generated
        password, else G2_USER_DEFAULT ("owlet").
    So GO2RTC_PASS alone means "owlet" + that password, and GO2RTC_USER alone
    means that user + the stored/generated password. The password is never
    logged; read it from that file (or set the env vars)."""
    u = (os.environ.get("GO2RTC_USER") or "").strip()
    p = (os.environ.get("GO2RTC_PASS") or "").strip()
    if p:
        return u or G2_USER_DEFAULT, p
    try:
        with open(G2_SECRET_PATH) as fh:
            d = json.load(fh)
        if d.get("password"):
            return u or str(d.get("username") or G2_USER_DEFAULT), str(d["password"])
    except (OSError, ValueError):
        pass
    import secrets as _secrets
    u = u or G2_USER_DEFAULT
    p = _secrets.token_urlsafe(18)
    if create:
        try:
            os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)
            os.chmod(SECRETS_DIR, 0o700)
            _atomic_write(G2_SECRET_PATH, json.dumps({"username": u, "password": p}),
                          mode=0o600)
        except OSError:
            pass
    return u, p


def render_go2rtc(cameras: list[dict], candidate: str | None = None,
                  creds: tuple[str, str] | None = None) -> str:
    user, pw = creds if creds is not None else go2rtc_credentials()
    lines = [
        "# generated by owlet-bridge from your saved cameras — do not edit by hand.",
        "log:", "  level: info", "",
        # API on container loopback by default (see G_API_LISTEN); auth for any
        # non-loopback client if GO2RTC_API_LISTEN widens it.
        "api:", f"  listen: {_yaml_str(G_API_LISTEN)}",
        f"  username: {_yaml_str(user)}", f"  password: {_yaml_str(pw)}", "",
        "rtsp:", f"  listen: {_yaml_str(G_RTSP_LISTEN)}",
        f"  username: {_yaml_str(user)}", f"  password: {_yaml_str(pw)}", "",
        "webrtc:", f'  listen: ":{G_WEBRTC}"',
    ]
    # External WebRTC candidate(s) for sub-second from phones/other hosts. Comes
    # from the saved config (UI field, survives restarts) or the
    # OWLET_WEBRTC_CANDIDATE env var. Value is "<host-ip>:<webrtc host port>".
    cand = (candidate if candidate is not None
            else os.environ.get("OWLET_WEBRTC_CANDIDATE", "")).strip()
    if cand:
        # Validate + YAML-quote each entry so a bad free-text value can't brick
        # the whole go2rtc config (which would survive every restart).
        valid = [c.strip() for c in cand.split(",")
                 if c.strip() and _CAND_RE.match(c.strip())]
        if valid:
            lines.append("  candidates:")
            for c in valid:
                lines.append(f'    - "{c}"')
    lines += ["", "streams:"]
    for cam in cameras:
        nm = slugify(cam.get("name") or "")  # never interpolate a raw name
        lines.append(f"  {nm}:")
        lines.append("    - " + _exec_source(nm))
        # Second producer = camera audio only; go2rtc merges it into this stream.
        # Listed after the video source so video is the stream's first track.
        lines.append("    - " + _audio_source(nm))
        # Never-started identical spares: on a go2rtc fan-out wedge the watchdog /
        # stall supervisor alias the camera name to one of these (see
        # go2rtc_swap_to_spare). Cost nothing until used. Spares must be full
        # replacements, audio included, or a swap would silently drop sound.
        for sp in spare_names(nm):
            lines.append(f"  {sp}:")
            lines.append("    - " + _exec_source(nm))
            lines.append("    - " + _audio_source(nm))
        # Optional glass-HUD variant, on-demand (free unless viewed).
        lines.append(f"  {nm}_overlay:")
        lines.append("    - " + _overlay_source(nm))
    return "\n".join(lines) + "\n"


def generate(account: dict, cameras: list[dict]) -> str:
    """Write per-camera env files + the generated go2rtc config. With no cameras
    yet, still emit a single placeholder `owlet` stream so default URLs resolve."""
    render_cams = cameras or [dict(CAMERA_DEFAULTS, name=DEFAULT_CAM_NAME)]
    os.makedirs(CAM_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(CAM_DIR, 0o700)
    except OSError:
        pass
    for cam in render_cams:
        _write_env(os.path.join(CAM_DIR, cam["name"] + ".env"), camera_env(account, cam))
    # legacy primary env, for the baked-in single-cam fallback go2rtc.yaml
    _write_env(ENV_PATH, camera_env(account, render_cams[0]))
    # Candidate from the saved config (boot-safe — the Termux base image drops
    # docker -e vars before the app starts) falling back to the env var.
    cand = (account.get("webrtc_candidate") or "").strip() \
        or os.environ.get("OWLET_WEBRTC_CANDIDATE", "")
    _atomic_write(GEN_PATH, render_go2rtc(render_cams, candidate=cand))
    return GEN_PATH



# --------------------------------------------------------------------------- #
# go2rtc self-heal: spare streams + alias swap
# --------------------------------------------------------------------------- #
# go2rtc 1.9 can wedge a stream's fan-out (2026-09-12: after a Frigate restart it
# stopped reading the exec producer — 2.6 MB unsent on ffmpeg's socket — kept the
# dead producer listed and never relaunched the exec; only a container restart
# helped). Its API refuses to create exec sources dynamically (PUT -> New() ->
# Validate() rejects "dynamic sources"), but PATCH ?name=X&src=<existing stream>
# simply aliases X to that stream's object. So every camera gets SPARE_STREAMS
# identical, never-started spares in the generated config, and on a wedge we point
# the camera's name at the first unused spare: consumers land on a fresh object
# whose exec launches on demand. The wedged object leaks until the next restart.
SPARE_STREAMS = int(os.environ.get("OWLET_SPARE_STREAMS") or "8")


def spare_names(name: str) -> list[str]:
    return [f"{name}_spare{i}" for i in range(1, SPARE_STREAMS + 1)]


def pick_spare(name: str, streams: dict) -> str | None:
    """First spare of `name` that go2rtc has never handed to a consumer
    (its "consumers" is null until first use; [] afterwards)."""
    for sp in spare_names(name):
        info = streams.get(sp)
        if info is not None and info.get("consumers") is None:
            return sp
    return None


def go2rtc_swap_to_spare(name: str, log=None) -> str | None:
    """Alias go2rtc stream `name` to its next unused spare. Returns the spare
    name on success, None if none are left (caller escalates)."""
    import json as _json
    import urllib.request
    say = log or (lambda m: None)
    api = f"http://127.0.0.1:{G_HTTP}/api/streams"
    try:
        streams = _json.load(urllib.request.urlopen(api, timeout=5))
    except Exception as e:  # noqa: BLE001
        say(f"{name}: go2rtc API unreachable ({e})")
        return None
    sp = pick_spare(name, streams)
    if sp is None:
        say(f"{name}: no unused spare go2rtc stream left ({SPARE_STREAMS} configured) — "
            "a container restart is the only reset now")
        return None
    try:
        req = urllib.request.Request(f"{api}?name={name}&src={sp}", method="PATCH")
        r = urllib.request.urlopen(req, timeout=5)
        say(f"{name}: go2rtc stream aliased to fresh spare '{sp}' (HTTP {r.status}); "
            "the wedged object is abandoned")
        return sp
    except Exception as e:  # noqa: BLE001
        say(f"{name}: go2rtc alias swap to '{sp}' failed: {e}")
        return None


def camera_names(cfg: dict | None = None) -> list[str]:
    cfg = cfg or load_config()
    return [c["name"] for c in cfg.get("cameras") or []] or [DEFAULT_CAM_NAME]


def streamable_names(cfg: dict | None = None) -> list[str]:
    """Cameras whose exec can actually connect: a saved UID, or a DSN plus an
    account login it can turn into a key. Unlike camera_names() this does NOT
    fall back to the placeholder `owlet` stream — the keepalive and the watchdog
    use it so a fresh install with no camera yet is left alone instead of
    spawning a failing exec every few seconds / restarting the container."""
    cfg = cfg if cfg is not None else load_config()
    have_login = bool(cfg.get("email") and cfg.get("password"))
    return [c["name"] for c in (cfg.get("cameras") or [])
            if c.get("name") and (c.get("uid") or (c.get("camera_dsn") and have_login))]


# Drag-and-drop lullaby/sound MP3s live here; persisted with the config.
SOUNDS_DIR = os.path.join(CONFIG_DIR, "sounds")


def talk_fifo_path(name: str) -> str:
    """The FIFO tutk_client reads talk/sound audio from — same path the generated
    exec creates (${TMPDIR:-/tmp}/owlet-talk-<name>)."""
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, "owlet-talk-" + name)


def stream_ready(name: str) -> bool:
    """Is this camera's tutk_client session up? The generated exec creates the
    talk FIFO (rm + mkfifo) *before* it starts tutk_client and removes it in its
    EXIT trap, so the FIFO's existence tracks the session exactly. This is the
    signal every control-plane endpoint should gate on.

    It must NOT be the audiocmd/audioresp files: the exec `rm -f`s those at
    every start and nothing recreates them until the first request is written,
    so gating on their existence made the native sound machine, device info,
    LED and live-volume endpoints answer "camera isn't streaming yet" forever
    while the camera was plainly streaming (found 2026-09-23)."""
    return os.path.exists(talk_fifo_path(name))


def audio_fifo_path(name: str) -> str:
    """The FIFO tutk_client writes the camera's received ADTS AAC into, and the
    audio producer reads from — same path the generated exec creates
    (${TMPDIR:-/tmp}/owlet-audio-<name>)."""
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, "owlet-audio-" + name)


def vol_file_path(name: str) -> str:
    """The file the UI writes the desired 0-100 speaker volume into; tutk_client's
    _volume_thread watches it (${TMPDIR:-/tmp}/owlet-vol-<name>)."""
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, "owlet-vol-" + name)


def audiocmd_file_path(name: str) -> str:
    """Native audio-player request file the UI writes JSON commands into."""
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, "owlet-audiocmd-" + name)


def audioresp_file_path(name: str) -> str:
    """Native audio-player response file tutk_client writes the camera reply into."""
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, "owlet-audioresp-" + name)


# Per-camera room-sensor sidecar (temp/humidity/noise/brightness/motion/sound),
# written by tutk_client from the TUTK frame info + GetRealtimeData IOCTL.
VITALS_DIR = os.path.join(CONFIG_DIR, "vitals")


def cam_sensors_path(name: str) -> str:
    return os.path.join(VITALS_DIR, "cam-" + name + ".json")
