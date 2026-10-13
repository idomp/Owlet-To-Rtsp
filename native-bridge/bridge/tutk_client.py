#!/usr/bin/env python3
"""
tutk_client.py — connect to the Owlet camera over TUTK/Kalay and write raw H.264
to stdout for go2rtc.

The call sequence below is the EXACT one the Owlet Android app uses, recovered by
decompiling `com.owlet.tutk.AndroidTutkSdk` (camera-sdk_release):

    TUTK_SDK_Set_License_Key(<app license key>)
    IOTC_Set_LanSearchPort(63616)
    IOTC_Setup_Session_Alive_Timeout(20)
    TUTK_SDK_Set_Region(3)              # REGION_US
    IOTC_Initialize2(0)
    avInitialize(512)                   # MAX_CAMERAS
    sid = IOTC_Get_SessionID()
    IOTC_Connect_ByUIDEx(uid, sid, St_IOTCConnectInput{authType=0, authKey, timeout=20})
    avClientStart2(sid, "admin", <password>, 20, &servType, 0, &resend)
    avSendIOCtrl(av, 511 /*IOTYPE_USER_IPCAM_START*/, SMsgAVIoctrlAVStream{channel})
    loop: avRecvFrameData2(...) -> stdout

Config comes from the web UI (/config/owlet.env) or env vars:
  OWLET_UID, OWLET_AUTHKEY, OWLET_AV_ACCOUNT, OWLET_AV_PASSWORD,
  OWLET_IOTYPE_START (default 511), OWLET_AV_CHANNEL (default 0),
  OWLET_LICENSE_KEY (defaults to the app key), OWLET_REGION_CODE (default 3)

Needs the TUTK .so libs (libIOTCAPIs.so, libAVAPIs.so, libTUTKGlobalAPIs.so, …)
in TUTK_LIB_DIR.
"""

from __future__ import annotations

import ctypes
import faulthandler
import fcntl
import json
import os
import struct
import sys
import threading
import time
from ctypes import (
    CDLL,
    POINTER,
    Structure,
    byref,
    c_byte,
    c_char,
    c_char_p,
    c_int,
    c_ubyte,
    c_uint, c_void_p,
    create_string_buffer,
)

LIB_DIR = os.environ.get("TUTK_LIB_DIR", "/app/libs/x86_64")

# The license key baked into the Owlet Android app (com.owletcare.owletcare).
# Required by the newer Kalay SDK before any IOTC call, or connect hangs forever.
APP_LICENSE_KEY = (
    "AQAAAGHr2tF3sL8TGR+XirMqZSd8hKY3eBRqKIceLcUSy2okTWYU27qQmwzBORp3tw1yoqiX7l+"
    "yoikFTI+Dzh9M+utHJ/3UBjL8FkYk4kuTSdcE6FtpD3Gidjxnmu2z9TONdpEx15uXvTATqSexOC"
    "GDcldb3xtVXRmH0GoVx9SPKwVPaj7/iYJnPaaURxPzEbEr2Yfd0ckSZoZ8jRH5jxmcJdob"
)

UID = os.environ.get("OWLET_UID", "")
AUTHKEY = os.environ.get("OWLET_AUTHKEY", "")
AV_ACCOUNT = os.environ.get("OWLET_AV_ACCOUNT", "admin").encode()
AV_PASSWORD = os.environ.get("OWLET_AV_PASSWORD", "").encode()
# `or "<default>"` (not the .get default) so an env var present-but-empty — which
# is exactly what an older saved /config/owlet.env contains — falls back cleanly
# instead of crashing on int("").
IOTYPE_START = int(os.environ.get("OWLET_IOTYPE_START") or "511", 0)   # 0x01FF
AV_CHANNEL = int(os.environ.get("OWLET_AV_CHANNEL") or "0")
LICENSE_KEY = os.environ.get("OWLET_LICENSE_KEY") or APP_LICENSE_KEY
REGION_CODE = int(os.environ.get("OWLET_REGION_CODE") or "3")          # 3 = REGION_US
CONNECT_TIMEOUT = int(os.environ.get("OWLET_CONNECT_TIMEOUT") or "20")
# A connect can succeed at the IOTC/AV layer yet deliver NO video when the camera's
# single P2P slot is still held by a prior/racing session. Give up on such a "dud"
# session after this many seconds with no frame, then wait out the camera's session
# hold before reconnecting (otherwise the next connect just races it again).
NO_VIDEO_TIMEOUT = int(os.environ.get("OWLET_NO_VIDEO_TIMEOUT") or "15")
RECONNECT_WAIT = int(os.environ.get("OWLET_RECONNECT_WAIT") or "25")  # > cam's 20s session hold

# A session can ALSO wedge with the main thread blocked — inside avRecvFrameData2
# (the lib never returns) or in the stdout write (ffmpeg stopped draining the
# pipe). Then the NO_VIDEO_TIMEOUT check in the frame loop never runs: audio keeps
# flowing from its own thread, nothing is logged, and only the container watchdog
# notices minutes later. Field data 2026-09-11: 22 stalls in one day, every one of
# them this shape — not a single "no video for 15s" line ever logged. A side thread
# (_stall_supervisor) watches the frame clock instead; past STALL_TIMEOUT it dumps
# every thread's stack to the log (so the wedge is finally diagnosable), calls
# avClientStop() to break a recv blocked in the lib, and if the main thread still
# isn't back after STALL_UNBLOCK_WAIT it hard-exits so go2rtc relaunches the exec.
STALL_TIMEOUT = int(os.environ.get("OWLET_STALL_TIMEOUT") or "30")
STALL_UNBLOCK_WAIT = int(os.environ.get("OWLET_STALL_UNBLOCK_WAIT") or "8")
# AV layer (avClientStartEx, the path the Owlet app uses). security_mode:
# 0=Simple 1=Dtls 2=Auto; auth_type: 0=Password 1=Token 2=Nebula. If
# OWLET_AV_SECURITY_MODE is blank we auto-probe [Auto, Dtls, Simple] because the
# Dream Duo rejects plain avClientStart2 (simple) with a DTLS-class error.
AV_SECURITY_MODE = os.environ.get("OWLET_AV_SECURITY_MODE", "").strip()
AV_AUTH_TYPE = int(os.environ.get("OWLET_AV_AUTH_TYPE") or "0")
AV_SYNC_RECV = int(os.environ.get("OWLET_AV_SYNC_RECV") or "0")

FRAME_BUF = 2 * 1024 * 1024   # 2 MB — fits a 1440p/2K keyframe without truncation
FINFO_BUF = 64
IOTC_ER_ALREADY_INITIALIZED = -3
AV_ER_DATA_NOREADY = -20012
AV_ER_REMOTE_TIMEOUT_DISCONNECT = -20015
AV_ER_SESSION_CLOSE_BY_REMOTE = -20016
AV_ER_IOTC_CHANNEL_IN_USED = -20027   # avServStart2 on a channel already claimed

# TOT-35: of 25 connect cycles logged, 23 died silently at 30-90s (video stops,
# audio keeps flowing, no error) while the 2 that sustained ran 77min/154min.
# Every cycle authenticates and connects cleanly, so the theory is that the dying
# ones land in TUTK relay mode rather than direct P2P. IOTC_Session_Check() turns
# that from a theory into a measurement: 0=P2P 1=Relay 2=LAN (IOTC_SESSION_MODE_FAIL
# on error).
SESSION_MODE_NAMES = {0: "P2P", 1: "Relay", 2: "LAN"}
IOTC_SESSION_MODE_FAIL = 0xFFFFFFFF   # nMode is unsigned; SDK reports failure as -1
# Reject a relay-mode session and retry for direct P2P instead of silently
# accepting it. Bounded so a network that genuinely can't reach P2P (e.g. blocked
# UDP) still eventually gets a stream rather than retrying forever.
#
# Defaults OFF. The relay-vs-P2P theory fits the data (bimodal survival curve,
# clean auth every cycle, audio outliving video) but has never been directly
# observed — IOTC_Session_Check() didn't exist in this codebase until this same
# change added it. Ship logging-only, watch a dozen real connect cycles, confirm
# the 30-90s deaths are actually the relay-mode sessions, THEN flip this on. Do
# not default it to on before that correlation is confirmed (see TOT-35 review).
# 2026-09-12: DEFAULT OFF, and opt-in only. The first production run of this
# check (Owlet Cam, Kalay libs from app 3.35.1) crashed the stream process right
# after every connect: the lib fills a struct far larger than the two-uint
# SessionModeVer below (the garbage "mode" values were ASCII from the UID), so
# the 8-byte ctypes struct was overrun and the process segfaulted — go2rtc
# relaunched it ~60x/min and the Owlet KMS rate-limited the account (HTTP 429).
# When enabled it now passes a 512-byte buffer and only reports the raw bytes.
SESSION_CHECK = os.environ.get("OWLET_SESSION_CHECK", "0") != "0"
REQUIRE_P2P = SESSION_CHECK and os.environ.get("OWLET_REQUIRE_P2P", "0") != "0"
P2P_RETRY_LIMIT = int(os.environ.get("OWLET_P2P_RETRY_LIMIT") or "2")
P2P_RETRY_WAIT = float(os.environ.get("OWLET_P2P_RETRY_WAIT") or "1.5")

# --- camera environmental sensors (temp/humidity/noise/brightness) ------------
# Reverse-engineered from the Owlet app (see docs/owlet-cam-sensors.md). Temp +
# noise + motion + sound are embedded in the EXTENDED frame-info struct the cam
# sends with every frame (offsets below). Humidity + brightness need a separate
# GetRealtimeData IOCTL (req 960 -> resp 961). We publish everything to a JSON
# sidecar the web UI / overlay reads. Disable with OWLET_SENSORS=0.
CAM_SENSORS_PATH = os.environ.get("OWLET_CAM_SENSORS", "")
SENSORS_ENABLED = os.environ.get("OWLET_SENSORS", "1") != "0"
IOTYPE_GET_REALTIME_REQ = 960   # 0x3C0
IOTYPE_GET_REALTIME_RESP = 961  # 0x3C1
# The Owlet app polls GET_REALTIME_DATA every 5s for the production vitals; we
# poll a bit faster (humidity/brightness/wifi only come from this IOCTL). It's a
# cheap mutex-serialized req/resp, so 2s is safe.
CAM_SENSOR_INTERVAL = int(os.environ.get("OWLET_SENSOR_INTERVAL") or "2")
# Temperature rides EVERY video frame in the frame-info struct — the app reads it
# per-frame for the live readout, so it's effectively instant. We publish it on
# this (much shorter) interval rather than the IOCTL interval. ~1s avoids writing
# the sidecar at 25 fps while still feeling live.
try:
    FRAME_SENSOR_INTERVAL = float(os.environ.get("OWLET_FRAME_SENSOR_INTERVAL") or "1")
except ValueError:
    FRAME_SENSOR_INTERVAL = 1.0
# frame-info field offsets (little-endian) in the Owlet extended FRAMEINFO
_FI_FLAGS = 2
_FI_TEMP = 16
_FI_AUDIO_DB = 24
_FI_MIN_LEN = 28

_CAM_SENSORS: dict = {}
_CAM_SENSORS_LOCK = threading.Lock()
_last_frame_sensor = [0.0]   # throttle frame-info writes
# Some cams report temperature in tenths of °C. Default 1 (whole °C — confirmed
# correct on the reference cam). Set OWLET_TEMP_SCALE=10 if your room temp reads
# ~10x too high.
try:
    TEMP_SCALE = float(os.environ.get("OWLET_TEMP_SCALE") or "1") or 1.0
except ValueError:
    TEMP_SCALE = 1.0


def _gate_realtime(temp, humidity):
    """Drop the camera's not-available sentinels from a GET_REALTIME_DATA reply.

    A cam without (or not yet reporting) the room sensors answers 0xFF (255) in
    both the temperature and humidity fields. Published raw, that became a
    255 °C room -> "491°F   255% RH" on the HUD and in the MQTT/Home Assistant
    entities (seen live 2026-09-23). Same sanity window the frame-info path
    already applies for temperature (the app's isValidCelsiusTemperature), and
    0..100 for humidity. Returns (temp, humidity) with junk replaced by None."""
    t = temp
    if t is not None:
        scaled = t / TEMP_SCALE if TEMP_SCALE else t
        if not (-20 <= scaled <= 60):
            t = None
    h = humidity
    if h is not None and not (0 <= h <= 100):
        h = None
    return t, h


def _publish_cam_sensors(_clear=(), **fields) -> None:
    """Merge new readings into the sidecar JSON (atomic write).

    Every field carries its own measurement time in "field_ts", so a reader can
    tell a fresh temperature from a humidity value last seen an hour ago (the
    old single "ts" made every stale field look as new as the latest one).
    Fields named in `_clear` were reported as not-available by the camera and
    are removed instead of keeping their last good value."""
    if not (CAM_SENSORS_PATH and SENSORS_ENABLED):
        return
    if fields.get("temperature") is not None and TEMP_SCALE != 1.0:
        fields["temperature"] = round(fields["temperature"] / TEMP_SCALE, 1)
    now = time.time()
    with _CAM_SENSORS_LOCK:
        fts = _CAM_SENSORS.setdefault("field_ts", {})
        for k in _clear:
            if fields.get(k) is None:
                _CAM_SENSORS.pop(k, None)
                fts.pop(k, None)
        for k, v in fields.items():
            if v is not None:
                _CAM_SENSORS[k] = v
                fts[k] = now
        _CAM_SENSORS["ts"] = now
        try:
            tmp = CAM_SENSORS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(_CAM_SENSORS, f)
            os.replace(tmp, CAM_SENSORS_PATH)
        except OSError:
            pass


def _parse_frame_sensors(info: bytes, n: int) -> None:
    """Publish the live temperature that rides in every video frame's frame-info.

    Temperature is the one field the Owlet app trusts from frame-info — it reads
    getTemperature() per frame for the instant on-screen readout (e1/c temp@0x10).
    The frame also carries audio_db@0x18 but the app parses-and-discards it, so we
    leave noise/humidity/brightness to the authoritative GET_REALTIME_DATA poll.
    Throttled to FRAME_SENSOR_INTERVAL (~1s) so we don't write the sidecar at 25fps.
    """
    if not (CAM_SENSORS_PATH and SENSORS_ENABLED) or n < _FI_MIN_LEN:
        return
    now = time.time()
    if now - _last_frame_sensor[0] < FRAME_SENSOR_INTERVAL:
        return
    _last_frame_sensor[0] = now
    temp = int.from_bytes(info[_FI_TEMP:_FI_TEMP + 4], "little", signed=True)
    # Gate obviously-bad frames (the app validates with isValidCelsiusTemperature);
    # a raw room temp of -20..60 °C is sane, anything else is a junk frame.
    scaled = temp / TEMP_SCALE if TEMP_SCALE else temp
    if -20 <= scaled <= 60:
        _publish_cam_sensors(temperature=temp)


def _realtime_thread(av, av_idx, stop_evt):
    """Poll the cam's GetRealtimeData IOCTL for humidity + brightness.

    Sends req 960 (4 zero bytes), reads IOCtrl frames until resp 961, parses the
    little-endian struct {temp i32@0, humidity i32@4, noise i32@8, brightness
    i32@12, wifi_rssi i8@16}. Fully guarded — any error just skips this round so
    it can never disturb the video pipeline.
    """
    if not (CAM_SENSORS_PATH and SENSORS_ENABLED):
        return
    if not hasattr(av, "avRecvIOCtrl"):
        return
    io_type = c_int(0)
    rbuf = create_string_buffer(64)
    payload = b"\x00\x00\x00\x00"
    while not stop_evt.is_set():
        # While talk is being set up / active, do NOT touch avRecvIOCtrl — the
        # talk handshake reads the camera's SPEAKERSTART ack (0x600b6) off the same
        # channel queue, and avRecvIOCtrl pops one message per call. If we drain
        # here we can steal the ack, making talk silently start the server too
        # early. (Mirrors how _audio_probe parks during talk.)
        if _TALKING.is_set():
            stop_evt.wait(0.2)
            continue
        try:
            with _AV_IO:
                av.avSendIOCtrl(av_idx, IOTYPE_GET_REALTIME_REQ, payload, len(payload))
            # drain IOCtrl responses for up to ~1s looking for 961
            deadline = time.time() + 1.0
            while time.time() < deadline and not stop_evt.is_set():
                with _AV_IO:
                    rc = av.avRecvIOCtrl(av_idx, byref(io_type), rbuf, 64, 500)
                if rc < 0:
                    break
                if io_type.value == IOTYPE_GET_REALTIME_RESP and rc >= 16:
                    b = rbuf.raw
                    temp, hum = _gate_realtime(
                        int.from_bytes(b[0:4], "little", signed=True),
                        int.from_bytes(b[4:8], "little", signed=True))
                    _publish_cam_sensors(
                        # 0xFF sentinel -> the cam has no reading: clear it
                        _clear=("temperature", "humidity"),
                        temperature=temp,
                        humidity=hum,
                        noise=int.from_bytes(b[8:12], "little", signed=True),
                        brightness=int.from_bytes(b[12:16], "little", signed=True),
                        # wifi_rssi is a 4-byte LE int at offset 16 (confirmed
                        # from the Owlet app: a0.c([B],0x10) reads a signed int32).
                        wifi_rssi=(int.from_bytes(b[16:20], "little", signed=True)
                                   if rc >= 20 else None),
                    )
                    break
        except Exception:  # noqa: BLE001
            pass
        stop_evt.wait(CAM_SENSOR_INTERVAL)


def _vol_units(pct: int) -> int:
    """Map a 0-100 percent to the camera's 0-5 device units, exactly like the
    Owlet app's i1.i.a(): units = (pct*5 + 50) // 100, clamped to 0..5."""
    pct = max(0, min(100, pct))
    return max(0, min(5, (pct * 5 + 50) // 100))


def _spk_vol_payload(pct: int) -> bytes:
    """8-byte SET_SPK_VOL payload: int32 LE device-unit (0-5) at offset 0, rest 0."""
    p = bytearray(8)
    struct.pack_into("<i", p, 0, _vol_units(pct))
    return bytes(p)


def _volume_thread(av, av_idx, stop_evt):
    """Watch VOL_FILE for a 0-100 percent and push SET_SPK_VOL to the camera when
    it changes; apply OWLET_SPK_VOL once at start. Fully guarded — any error just
    skips a round so it can never disturb the video pipeline."""
    last = None

    def _apply(pct: int):
        nonlocal last
        try:
            payload = _spk_vol_payload(pct)
            with _AV_IO:
                rc = av.avSendIOCtrl(av_idx, IOTYPE_SET_SPK_VOL,
                                     c_char_p(payload), len(payload))
            log(f"[vol] speaker volume -> {pct}% (units={_vol_units(pct)}) rc={rc}")
            last = pct
        except Exception as e:  # noqa: BLE001
            log(f"[vol] set failed: {e}")

    if SPK_VOL_INITIAL >= 0:
        _apply(SPK_VOL_INITIAL)
    while not stop_evt.is_set():
        if VOL_FILE:
            try:
                with open(VOL_FILE) as f:
                    pct = int(f.read().strip())
            except (OSError, ValueError):
                pct = None
            if pct is not None and pct != last:
                _apply(pct)
        stop_evt.wait(1.0)


def _json_ioctl(av, av_idx, obj, want_resp=True, timeout=2.5):
    """Send one audio-player JSON command via IOCTL 0x60105 (plain UTF-8 JSON, no
    framing — exactly as the Owlet app). When want_resp, drain avRecvIOCtrl for the
    matching 0x60105 reply and return the decoded dict; else just send. The whole
    send+drain is under _AV_IO so it can't interleave with the sensor poller."""
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    io_type = c_int(0)
    rbuf = create_string_buffer(4096)
    with _AV_IO:
        rc = av.avSendIOCtrl(av_idx, IOTYPE_OWLET_JSON, c_char_p(body), len(body))
        if rc < 0:
            return {"error": f"send rc={rc}"}
        if not want_resp:
            return {"ok": True}
        if not hasattr(av, "avRecvIOCtrl"):
            return {"ok": True, "note": "no avRecvIOCtrl in lib"}
        deadline = time.time() + timeout
        acc = b""
        while time.time() < deadline:
            rc = av.avRecvIOCtrl(av_idx, byref(io_type), rbuf, 4096, 500)
            if rc in (AV_ER_REMOTE_TIMEOUT_DISCONNECT, AV_ER_SESSION_CLOSE_BY_REMOTE):
                return {"error": f"session closed rc={rc}"}
            if rc < 0:
                continue  # timeout / not-ready this round — keep waiting
            if io_type.value == IOTYPE_OWLET_JSON and rc > 0:
                acc += rbuf.raw[:rc]
                try:
                    return json.loads(acc.decode("utf-8", "ignore"))
                except ValueError:
                    continue  # camera may deliver the JSON in pieces
        return {"error": "no response (timeout)"}


def _raw_ioctl(av, av_idx, req_io, payload: bytes, resp_io=None, timeout=2.0):
    """Send a binary IOCTL on the client channel; if resp_io is given, drain
    avRecvIOCtrl for that response io_type and return its raw bytes (else None).
    Whole transaction under _AV_IO so it can't interleave with other drainers."""
    io_type = c_int(0)
    rbuf = create_string_buffer(1024)
    with _AV_IO:
        rc = av.avSendIOCtrl(av_idx, req_io, c_char_p(payload), len(payload))
        if rc < 0:
            return None if resp_io is None else {"error": f"send rc={rc}"}
        if resp_io is None:
            return b""
        if not hasattr(av, "avRecvIOCtrl"):
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            rc = av.avRecvIOCtrl(av_idx, byref(io_type), rbuf, 1024, 500)
            if rc in (AV_ER_REMOTE_TIMEOUT_DISCONNECT, AV_ER_SESSION_CLOSE_BY_REMOTE):
                return None
            if rc < 0:
                continue
            if io_type.value == resp_io and rc > 0:
                return rbuf.raw[:rc]
    return None


def _handle_audio_cmd(av, av_idx, req: dict) -> dict:
    """Translate a small UI request into the app's wire commands (audio player +
    camera controls — all client-channel IOCTLs)."""
    action = req.get("action")
    # --- camera controls -----------------------------------------------------
    if action == "led":
        on = bool(req.get("on"))
        payload = bytes([1, 1, 1, 0]) if on else bytes([0, 0, 0, 0])
        _raw_ioctl(av, av_idx, IOTYPE_SET_LED, payload)
        return {"ok": True, "led": "on" if on else "off"}
    if action == "devinfo":
        b = _raw_ioctl(av, av_idx, IOTYPE_DEVINFO_REQ, b"\x00\x00\x00\x00",
                       resp_io=IOTYPE_DEVINFO_RESP, timeout=3.0)
        if not isinstance(b, (bytes, bytearray)):
            return {"error": "no devinfo response"}
        def _ascii(off, ln):
            return b[off:off + ln].split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
        info = {"model": _ascii(0, 16), "vendor": _ascii(16, 16)}
        if len(b) >= 0x24:
            v = int.from_bytes(b[0x20:0x24], "little", signed=False)
            info["version"] = ".".join(str((v >> s) & 0xFF) for s in (24, 16, 8, 0))
        if len(b) > 0x38:
            info["firmware"] = _ascii(0x38, 16)
        return {"ok": True, **info}
    if action == "getvol":
        b = _raw_ioctl(av, av_idx, IOTYPE_GET_SPK_MIC_VOL, b"\x00\x00\x00\x00",
                       resp_io=IOTYPE_GET_SPK_MIC_VOL_RESP, timeout=3.0)
        if not isinstance(b, (bytes, bytearray)) or len(b) < 4:
            return {"error": "no volume response"}
        units = int.from_bytes(b[0:4], "little", signed=True)
        pct = max(0, min(100, (units * 100 + 2) // 5))   # app's i1.i.b()
        return {"ok": True, "units": units, "percent": pct}
    # --- native audio player -------------------------------------------------
    wire = _player_wire(action, req)
    if wire is None:
        return {"error": f"unknown action {action!r}"}
    if action in ("sources", "state"):
        return _json_ioctl(av, av_idx, wire[0])
    for body in wire:
        _json_ioctl(av, av_idx, body, want_resp=False)
    return {"ok": True, "action": action}


def _player_wire(action: str, req: dict):
    """The exact JSON bodies the Owlet app sends for a native-player action, in
    send order; None for an unknown action.

    Envelope, from the app's camera-sdk (kotlinx.serialization with
    `classDiscriminator = "cmd"` over the sealed class TutkJsonCommand): every
    message is FLAT -- `{"cmd": "<name>", ...fields...}`. The first cut of this
    code sent `{"<name>": {...}}` instead, which the camera rejects with
    `{"cmd":"unknown","error_code":5,"reason":"Invalid cmd field"}` (seen live
    on a CAM3 / fw 0.4.6.6, 2026-09-23). Per-command fields, from the DTOs:

      audio_player_sources / _state / _reset : no fields (object serializers)
      audio_player_queue     {"queue": {"items": [{"uuid": ...}, ...]}}
      audio_player_set       {"player": {"queue": {"repeat": bool, "timeout_ms": int}}}
      audio_player_transport {"player": {"action": "play|pause|stop|next|prev"}}

    Replies are flat too: {"cmd", "result", "error_code"} plus "items" (sources)
    or "player" (state)."""
    def cmd(name, **fields):
        return {"cmd": name, **fields}
    if action == "sources":
        return [cmd("audio_player_sources")]
    if action == "state":
        return [cmd("audio_player_state")]
    if action == "reset":
        return [cmd("audio_player_reset")]
    if action in ("play", "pause", "stop", "next", "prev"):
        out = []
        uuids = req.get("uuids") or []
        if uuids:
            out.append(cmd("audio_player_queue",
                           queue={"items": [{"uuid": u} for u in uuids]}))
        if req.get("repeat") is not None or req.get("timeout_ms") is not None:
            q = {}
            if req.get("repeat") is not None:
                q["repeat"] = bool(req["repeat"])
            if req.get("timeout_ms") is not None:
                q["timeout_ms"] = int(req["timeout_ms"])
            out.append(cmd("audio_player_set", player={"queue": q}))
        out.append(cmd("audio_player_transport", player={"action": action}))
        return out
    return None


def _audioplayer_thread(av, av_idx, stop_evt):
    """Watch AUDIOCMD_FILE for a JSON request, run it on the camera's native audio
    player, write the reply (with the request's id) to AUDIORESP_FILE. Inert until
    a request appears, so it sends no IOCTL traffic by default."""
    if not AUDIOCMD_FILE or not hasattr(av, "avSendIOCtrl"):
        return
    last_mtime = 0.0
    while not stop_evt.is_set():
        try:
            mtime = os.path.getmtime(AUDIOCMD_FILE)
        except OSError:
            mtime = 0.0
        if mtime and mtime != last_mtime:
            last_mtime = mtime
            # Don't run a command (which drains avRecvIOCtrl) while talk is being
            # set up — it could steal the SPEAKERSTART ack. Talk clips are short.
            while _TALKING.is_set() and not stop_evt.is_set():
                stop_evt.wait(0.1)
            try:
                with open(AUDIOCMD_FILE) as f:
                    req = json.load(f)
            except (OSError, ValueError):
                req = None
            if isinstance(req, dict):
                log(f"[lullaby] cmd: {req.get('action')}")
                try:
                    resp = _handle_audio_cmd(av, av_idx, req)
                except Exception as e:  # noqa: BLE001
                    resp = {"error": str(e)}
                resp["id"] = req.get("id")
                if AUDIORESP_FILE:
                    try:
                        tmp = AUDIORESP_FILE + ".tmp"
                        with open(tmp, "w") as f:
                            json.dump(resp, f)
                        os.replace(tmp, AUDIORESP_FILE)
                    except OSError:
                        pass
        stop_evt.wait(0.4)


# --- audio probe (step 1: capture + report the camera's audio format) ---------
# The Owlet cam carries audio on the same Kalay AV channel as video, but its codec
# / sample rate are undocumented. This probe reads the audio channel READ-ONLY (it
# never touches the video pipeline) and logs the exact codec_id + flags so we can
# mux it correctly. Disable with OWLET_AUDIO=0.
AUDIO_ENABLED = os.environ.get("OWLET_AUDIO", "1") != "0"
# Standard TUTK "start audio" IOCTL (IOTYPE_USER_IPCAM_AUDIOSTART = 0x300 = 768).
IOTYPE_AUDIOSTART = int(os.environ.get("OWLET_IOTYPE_AUDIOSTART") or "768")
AUDIO_BUF = 16 * 1024
# Best-effort decode tables for the log (raw values are logged too, so we don't
# rely on these being right — they just annotate the report).
_AUDIO_CODECS = {
    0x86: "AAC", 0x87: "AAC", 0x88: "AAC", 0x89: "PCM(s16)", 0x8a: "ADPCM",
    0x8b: "G711 u-law", 0x8c: "G711 a-law", 0x8d: "G726", 0x8e: "Speex",
    0x8f: "MP3", 0x140: "AAC", 0x141: "AAC",
}
_AUDIO_RATES = {0: 8000, 1: 11025, 2: 12000, 3: 16000, 4: 22050,
                5: 24000, 6: 32000, 7: 44100, 8: 48000}


def _guess_audio(codec_id: int, flags: int) -> str:
    codec = _AUDIO_CODECS.get(codec_id, f"unknown(0x{codec_id:04x})")
    rate = _AUDIO_RATES.get((flags >> 2) & 0x0f, "?")
    ch = "stereo" if (flags & 0x01) else "mono"
    bits = 16 if (flags & 0x02) else 8
    return f"codec≈{codec}, ~{rate}Hz, {ch}, {bits}-bit  (guess from flags — verify)"


# --- two-way talk / sound playback: send audio TO the camera's speaker ---------
# Recovered from the Owlet app: it sends IOTYPE_USER_IPCAM_SPEAKERSTART (848), then
# pushes AAC-LC 8kHz mono frames via avSendAudioData. We read AAC (ADTS) from
# OWLET_TALK_FIFO (written by the web UI's "hold to talk" / "play sound"), strip
# the ADTS header to raw AAC access units, and send them; SPEAKERSTOP (849) on idle.
IOTYPE_SPEAKERSTART = int(os.environ.get("OWLET_IOTYPE_SPEAKERSTART") or "848")
IOTYPE_SPEAKERSTOP = int(os.environ.get("OWLET_IOTYPE_SPEAKERSTOP") or "849")
# The camera's ack to SPEAKERSTART (the app waits for it before opening the AV
# server). 0x600b6 = 393398, from the app (z0/o0.getResponseType).
IOTYPE_SPEAKERSTART_RESP = int(os.environ.get("OWLET_IOTYPE_SPEAKERSTART_RESP") or "393398")
# Talk-back transport. The Owlet app does NOT push speaker audio on the video
# client channel — it opens an AV SERVER on a *free* channel of the live session
# (avServStart2) and sends audio there; the camera connects back to receive. This
# is why sending on the client channel was silently dropped. Set OWLET_TALK_LEGACY=1
# to force the old client-channel avSendAudioData path.
TALK_LEGACY = (os.environ.get("OWLET_TALK_LEGACY", "") or "").lower() in (
    "1", "true", "yes", "on")
try:
    # seconds avServStart2 waits to accept the camera's inbound connection. The app
    # uses 2; we allow a touch more headroom for slower P2P links (it returns
    # immediately once the camera connects, so a larger value costs nothing on success).
    AVSERV_TIMEOUT = int(os.environ.get("OWLET_AVSERV_TIMEOUT") or "4")
except ValueError:
    AVSERV_TIMEOUT = 4
# Speaker volume IOCTL (IOTYPE_USER_IPCAM_SET_SPK_VOL_REQ = 0x60092 = 393362, from
# the Owlet app). Payload: 8 bytes, a little-endian int32 "device unit" 0-5 at
# offset 0. The app maps a 0-100% UI value to units via (pct*5 + 50) // 100.
IOTYPE_SET_SPK_VOL = int(os.environ.get("OWLET_IOTYPE_SET_SPK_VOL") or "393362")
# Extra camera controls (all client-channel request/response IOCTLs, from the app):
IOTYPE_GET_SPK_MIC_VOL = 0x60096       # req; resp 0x60097 carries spk units i32@0
IOTYPE_GET_SPK_MIC_VOL_RESP = 0x60097
IOTYPE_SET_LED = 0x40082               # status indicator light; ON=[1,1,1,0] OFF=[0,0,0,0]
IOTYPE_SET_LED_RESP = 0x40083
IOTYPE_DEVINFO_REQ = 0x330             # model/vendor/firmware; resp 0x331
IOTYPE_DEVINFO_RESP = 0x331
# Live volume control: the web UI writes the desired 0-100 percent into this file;
# _volume_thread watches it and pushes SET_SPK_VOL when it changes. OWLET_SPK_VOL
# is an optional initial percent applied at connect (so a saved setting persists).
VOL_FILE = os.environ.get("OWLET_VOL_FILE", "")
try:
    SPK_VOL_INITIAL = int(os.environ.get("OWLET_SPK_VOL") or "-1")
except ValueError:
    SPK_VOL_INITIAL = -1

# Native audio player (camera's built-in soothing-sounds/lullabies). Driven by the
# Owlet app's JSON-over-IOCTL protocol: IOTYPE_USER_IPCAM_OWLET_JSON_MSG_REQ =
# 0x60105 = 393477, request AND response share that io_type, payload is plain
# UTF-8 JSON with no binary framing. The web UI drops a small JSON request into
# AUDIOCMD_FILE; _audioplayer_thread runs it and writes the camera's reply to
# AUDIORESP_FILE. The camera self-reports its track UUIDs (audio_player_sources),
# so no cloud is needed. No IOCTL is sent unless the user triggers an action, so
# this is inert by default.
IOTYPE_OWLET_JSON = int(os.environ.get("OWLET_IOTYPE_JSON_MSG") or "393477")
AUDIOCMD_FILE = os.environ.get("OWLET_AUDIOCMD_FILE", "")
AUDIORESP_FILE = os.environ.get("OWLET_AUDIORESP_FILE", "")
# If the camera closes the AV session when it receives SPEAKERSTART (848), set
# OWLET_SKIP_SPEAKERSTART=1. Some cameras open the speaker channel bi-directionally
# from AUDIOSTART alone and reject a separate SPEAKERSTART IOCTL.
SKIP_SPEAKERSTART = os.environ.get("OWLET_SKIP_SPEAKERSTART", "0") != "0"
TALK_FIFO = os.environ.get("OWLET_TALK_FIFO", "")
# Speaker frame format. Defaults match what the cam streams to us (AAC, 8kHz mono);
# The Owlet app hardcodes AAC-LC 0x88 / flags 0x02 for the speaker send and does
# NOT probe for the talk path, so these are fixed (env override kept for diagnostics
# only — the receive probe must never drive the SEND frameinfo).
SPEAKER_CODEC_ID = int(os.environ.get("OWLET_SPEAKER_CODEC_ID") or "0x88", 0)
SPEAKER_FLAGS = int(os.environ.get("OWLET_SPEAKER_FLAGS") or "0x02", 0)
_PROBED_AUDIO: dict = {"codec_id": None, "flags": None}

# Serializes every avSendIOCtrl / avRecvIOCtrl / avSendAudioData call. ctypes
# releases the GIL, so the talk thread, the realtime-sensor poller and the
# startup IOCTLs would otherwise run truly parallel inside the C SDK on one
# av_idx — a known segfault/queue-corruption source. (Frame/audio RECV stay on
# their own threads, unlocked.)
_AV_IO = threading.Lock()

# Half-duplex coordination. The Owlet camera has ONE P2P session and the TUTK
# lib is NOT safe doing simultaneous audio RECV (the probe) and audio SEND (talk)
# on the same channel — doing both corrupts/kills the session (video drops and
# avSendAudioData can hang the process, needing a container restart). So while the
# talk thread is actively sending speaker audio, the audio-receive probe PAUSES
# (exactly how the Owlet app behaves: it stops listening while you hold talk).
_TALKING = threading.Event()


def _talk_frameinfo() -> bytes:
    """16-byte audio frame header, byte-for-byte matching the Owlet app's
    e1.d.parseContent(short codec, byte flags, byte, byte, int ts):
        [0:2]  codec_id   little-endian uint16   (0x0088 = AAC) — PINNED
        [2]    flags       (0x02)                                — PINNED
        [3]    0
        [4]    0
        [5:12] 0
        [12:16] timestamp  little-endian uint32   = wall-clock ms, per frame

    Two app-exact details that were wrong before and caused 'accepted but SILENT':
    - timestamp is the 32-bit-truncated ABSOLUTE wall-clock ms (the app uses
      (int)System.currentTimeMillis()), recomputed every frame — NOT a 0-based
      per-clip counter. A jitter buffer keyed to wall-clock rejects frames whose
      timestamps reset to ~0 each time talk starts.
    - codec/flags are HARDCODED to 0x88/0x02 for the speaker send (the app does
      not probe for the talk path: d1/f is constructed with 0x88, flags 0x02).
      We must NOT let the receive-side audio probe override them."""
    fi = bytearray(16)
    struct.pack_into("<H", fi, 0, SPEAKER_CODEC_ID & 0xFFFF)          # codec_id @0
    fi[2] = SPEAKER_FLAGS & 0xFF                                      # flags    @2
    struct.pack_into("<I", fi, 12, int(time.time() * 1000) & 0xFFFFFFFF)  # ts @12
    return bytes(fi)


def _send_adts(av, av_idx, buf: bytes, stats: list, stop_evt=None) -> bytes:
    """Parse ADTS AAC frames from buf, strip the ADTS header, and send each as a
    raw AAC-LC access unit via avSendAudioData — exactly what the app does (its
    MediaCodec output is sent as bare AUs, never with ADTS). Returns leftover
    bytes (an incomplete trailing frame). stats=[sent, rejected, dead_rc]."""
    i, n = 0, len(buf)
    while n - i >= 7:
        # Bail immediately between frames if shutdown is requested — avClientStop
        # is called before this, but avSendAudioData can still hang on a dead
        # channel inside the C lib.  Checking here lets us exit after the current
        # in-flight C call returns rather than queuing more frames onto a dead av_idx.
        if stop_evt is not None and stop_evt.is_set():
            return b""
        if buf[i] != 0xFF or (buf[i + 1] & 0xF0) != 0xF0:   # ADTS sync 0xFFFx
            i += 1
            continue
        frame_len = ((buf[i + 3] & 0x03) << 11) | (buf[i + 4] << 3) | (buf[i + 5] >> 5)
        if frame_len < 7:
            i += 1
            continue
        if n - i < frame_len:
            break  # frame not fully buffered yet
        hdr = 7 if (buf[i + 1] & 0x01) else 9   # protection_absent -> 7-byte header
        raw = buf[i + hdr:i + frame_len]        # raw AAC access unit (no ADTS)
        fi = _talk_frameinfo()
        with _AV_IO:
            rc = av.avSendAudioData(av_idx, c_char_p(raw), len(raw), c_char_p(fi), len(fi))
        if rc is not None and rc < 0:
            stats[1] += 1
            # Record a session-close so the talk loop can stop immediately
            # instead of hammering a dead channel (which can hang the process).
            if rc in (AV_ER_REMOTE_TIMEOUT_DISCONNECT, AV_ER_SESSION_CLOSE_BY_REMOTE) \
                    and len(stats) > 2:
                stats[2] = rc
                return buf[i + frame_len:]
        else:
            stats[0] += 1
        i += frame_len
    return buf[i:]


def _ctrl_speaker(av, av_idx, ioctl, chan):
    """Send a speaker control IOCTL (SPEAKERSTART/STOP) on the video CLIENT channel,
    carrying the target speaker channel as a little-endian int32 in 8 bytes — the
    exact payload the app builds (e1.d$b.parseContent(channel))."""
    p = bytearray(8)
    struct.pack_into("<i", p, 0, int(chan))
    with _AV_IO:
        return av.avSendIOCtrl(av_idx, ioctl, c_char_p(bytes(p)), 8)


def _safe(fn):
    """Run a teardown call, swallowing any error (best-effort cleanup)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _await_ioctl_resp(av, av_idx, want, timeout):
    """Briefly drain avRecvIOCtrl on the client channel for a specific response
    io_type (e.g. the SPEAKERSTART ack). Best-effort; returns True if seen."""
    if not hasattr(av, "avRecvIOCtrl"):
        return False
    io_type = c_int(0)
    rbuf = create_string_buffer(1024)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _AV_IO:
            rc = av.avRecvIOCtrl(av_idx, byref(io_type), rbuf, 1024, 300)
        if rc < 0:
            continue
        if io_type.value == want:
            return True
    return False


def _talk_thread(av: CDLL, iotc: CDLL, session: int, av_idx: int,
                 stop_evt: threading.Event) -> None:
    """Play whatever AAC arrives on the talk FIFO out the camera's speaker.

    Talk-back uses the AV-SERVER model the Owlet app uses: get a free channel on
    the live session, tell the camera (SPEAKERSTART carrying that channel), open an
    avServStart2 server on it, and push audio there. Sending on the video client
    channel (the old behavior, OWLET_TALK_LEGACY=1) is silently dropped by the cam."""
    if not TALK_FIFO:
        log("[talk] OWLET_TALK_FIFO not set — talk/speaker disabled")
        return
    if not hasattr(av, "avSendAudioData"):
        log("[talk] avSendAudioData not found in TUTK lib — talk/speaker disabled "
            "(older lib build; two-way audio requires a lib with avSendAudioData)")
        return
    server_ok = (not TALK_LEGACY
                 and hasattr(av, "avServStart2")
                 and hasattr(iotc, "IOTC_Session_Get_Free_Channel"))
    if not server_ok and not TALK_LEGACY:
        log("[talk] lib lacks avServStart2/IOTC_Session_Get_Free_Channel — "
            "falling back to legacy client-channel send (may be silent)")
    log(f"[talk] transport = {'AV-server (app-style)' if server_ok else 'legacy client-channel'}")
    try:
        # O_RDWR so the FIFO never EOFs / blocks when no writer is connected.
        fd = os.open(TALK_FIFO, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:  # noqa: BLE001
        log(f"[talk] cannot open {TALK_FIFO}: {e}")
        return
    log(f"[talk] FIFO open (fd={fd}) — ready to receive audio on {TALK_FIFO}")
    buf = b""
    speaking = False
    send_idx = -1            # the av index to push audio on (server idx or av_idx)
    spk_chan = -1            # the speaker channel we opened (server model)
    spk_serv_idx = -1        # the avServStart2 return — needed for avServStop teardown
    last_audio = 0.0
    stats = [0, 0, 0]        # [sent, rejected, dead_rc] avSendAudioData results
    last_alive = time.monotonic()

    def _open_speaker():
        """Start the speaker channel; return (send_idx, chan) or (-1, -1).

        Order matches the app EXACTLY: free channel -> SPEAKERSTART(chan) ->
        WAIT for the camera's 0x600b6 ack (the camera only connects back to our
        server channel after this) -> avServStart2. avServStart2 is GATED on the
        ack: starting the server before the camera is listening = silent audio."""
        nonlocal spk_chan, spk_serv_idx
        if not server_ok:
            # legacy: just SPEAKERSTART on the client channel and send there
            rc = 0 if SKIP_SPEAKERSTART else _ctrl_speaker(
                av, av_idx, IOTYPE_SPEAKERSTART, AV_CHANNEL)
            log(f"[talk] (legacy) SPEAKERSTART rc={rc}")
            if rc in (AV_ER_REMOTE_TIMEOUT_DISCONNECT, AV_ER_SESSION_CLOSE_BY_REMOTE):
                return -1, -1
            return av_idx, AV_CHANNEL
        # AV-server model (matches the app). CRITICAL ORDERING: the camera connects
        # back to our server channel immediately after it ACKs SPEAKERSTART, so the
        # server must be LISTENING first. We send SPEAKERSTART and then call
        # avServStart2 right away (it blocks up to AVSERV_TIMEOUT accepting the
        # camera's connection). Waiting for the 0x600b6 ack before avServStart2 let
        # the camera grab the channel first -> avServStart2 returned -20027
        # (AV_ER_IOTC_CHANNEL_IN_USED). We do NOT wait for the ack.
        with _AV_IO:
            chan = iotc.IOTC_Session_Get_Free_Channel(session)
        if chan < 0:
            log(f"[talk] IOTC_Session_Get_Free_Channel rc={chan} — no free channel")
            return -1, -1
        for attempt in (1, 2):
            rc = _ctrl_speaker(av, av_idx, IOTYPE_SPEAKERSTART, chan)
            log(f"[talk] SPEAKERSTART(chan={chan}) rc={rc} (attempt {attempt})")
            if rc in (AV_ER_REMOTE_TIMEOUT_DISCONNECT, AV_ER_SESSION_CLOSE_BY_REMOTE):
                log("[talk] SPEAKERSTART closed the session — aborting talk")
                break
            # start the server NOW (no ack wait) — it accepts the camera's inbound
            # connection within AVSERV_TIMEOUT seconds.
            sidx = av.avServStart2(session, b"", b"", AVSERV_TIMEOUT, 0, chan)
            log(f"[talk] avServStart2(chan={chan}) -> serv_idx={sidx}")
            if sidx >= 0:
                spk_chan = chan
                spk_serv_idx = sidx
                return sidx, chan
            if sidx == AV_ER_IOTC_CHANNEL_IN_USED and attempt == 1:
                # the camera grabbed the channel before our server was up — close
                # that inbound channel and retry once so the server can own it.
                log("[talk] channel busy (-20027) — releasing + retrying")
                with _AV_IO:
                    _safe(lambda: iotc.IOTC_Session_Channel_OFF(session, chan))
                time.sleep(0.25)
                continue
            break
        with _AV_IO:
            _safe(lambda: iotc.IOTC_Session_Channel_OFF(session, chan))
        return -1, -1

    def _close_speaker():
        """Tear down the speaker. For a started server the app uses
        avServStop(serverIdx) (NOT avServExit) then SPEAKERSTOP then Channel_OFF."""
        nonlocal spk_chan, spk_serv_idx
        if server_ok and spk_chan >= 0:
            if spk_serv_idx >= 0 and hasattr(av, "avServStop"):
                with _AV_IO:
                    _safe(lambda: av.avServStop(spk_serv_idx))
            _safe(lambda: _ctrl_speaker(av, av_idx, IOTYPE_SPEAKERSTOP, spk_chan))
            with _AV_IO:
                _safe(lambda: iotc.IOTC_Session_Channel_OFF(session, spk_chan))
            spk_chan = -1
            spk_serv_idx = -1
        elif not server_ok:
            _safe(lambda: _ctrl_speaker(av, av_idx, IOTYPE_SPEAKERSTOP, AV_CHANNEL))

    try:
        while not stop_evt.is_set():
            if time.monotonic() - last_alive > 30:
                log(f"[talk] alive, listening on FIFO (speaking={speaking})")
                last_alive = time.monotonic()
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                chunk = b""
            if chunk:
                log(f"[talk] read {len(chunk)} bytes from FIFO")
                if not speaking:
                    # Pause the audio-RECV probe while talking (matches the app;
                    # also avoids any client/server channel contention on setup).
                    _TALKING.set()
                    time.sleep(0.06)
                    send_idx, _ = _open_speaker()
                    if send_idx < 0:
                        log("[talk] could not open speaker — dropping this clip")
                        _TALKING.clear()
                        buf = b""
                        # drain the FIFO so we don't immediately retry on stale data
                        try:
                            while os.read(fd, 8192):
                                pass
                        except BlockingIOError:
                            pass
                        continue
                    log(f"[talk] speaker ON (send_idx={send_idx} "
                        f"codec=0x{SPEAKER_CODEC_ID:04x} flags=0x{SPEAKER_FLAGS:02x})")
                    speaking = True
                    stats[0] = stats[1] = stats[2] = 0
                buf = _send_adts(av, send_idx, buf + chunk, stats, stop_evt=stop_evt)
                last_audio = time.monotonic()
                if stats[2]:
                    log(f"[talk] camera closed the channel mid-send (rc={stats[2]}); "
                        f"stopping talk")
                    _close_speaker()
                    speaking = False
                    _TALKING.clear()
                    continue
            else:
                if speaking and time.monotonic() - last_audio > 0.8:
                    _close_speaker()
                    speaking = False
                    buf = b""
                    _TALKING.clear()
                    log(f"[talk] speaker off (idle) — sent {stats[0]} frames, "
                        f"{stats[1]} rejected")
                time.sleep(0.02)
    finally:
        _TALKING.clear()
        # Tear the speaker channel down unless the whole session is already being
        # stopped (avServExit/avSendIOCtrl on a stopped session can hang).
        if speaking and not stop_evt.is_set():
            try:
                _close_speaker()
            except Exception:  # noqa: BLE001
                pass
        try:
            os.close(fd)
        except Exception:  # noqa: BLE001
            pass


def _audio_probe(av: CDLL, av_idx: int, stop_evt: threading.Event) -> None:
    """Capture the audio channel and, if OWLET_AUDIO_FIFO is set, write its raw
    bytes to that FIFO so ffmpeg can mux the audio next to the video. Read-only
    w.r.t. the video pipeline (never blocks the picture). Runs in its own thread."""
    if not hasattr(av, "avRecvAudioData"):
        log("[audio] avRecvAudioData missing from this lib build — no audio capture")
        return
    fifo_path = os.environ.get("OWLET_AUDIO_FIFO", "")
    fifo_fd = -1
    abuf = create_string_buffer(AUDIO_BUF)
    ainfo = create_string_buffer(FINFO_BUF)
    aidx = c_uint(0)
    frames = 0
    reported = False
    warned = False
    last_log = time.time()
    started = time.time()
    try:
        while not stop_evt.is_set():
            # Half-duplex: while the talk thread is pushing speaker audio, STOP
            # receiving audio. Simultaneous send+recv on the cam's single AV
            # channel corrupts the session (drops video / wedges the process).
            if _TALKING.is_set():
                time.sleep(0.05)
                continue
            try:
                rc = av.avRecvAudioData(av_idx, abuf, AUDIO_BUF, ainfo, FINFO_BUF, byref(aidx))
            except Exception as e:  # noqa: BLE001
                log(f"[audio] avRecvAudioData error: {e}")
                return
            if rc > 0:
                frames += 1
                if not reported:
                    info = ainfo.raw[:16]
                    codec_id = int.from_bytes(info[0:2], "little")
                    flags = info[2]
                    # feed the real format into the talk/speaker path
                    _PROBED_AUDIO["codec_id"] = codec_id
                    _PROBED_AUDIO["flags"] = flags
                    log("[audio] ===== AUDIO DETECTED =====")
                    log(f"[audio] codec_id=0x{codec_id:04x} flags=0x{flags:02x} "
                        f"frame_size={rc}B info={info.hex()}")
                    log(f"[audio] first frame bytes: {abuf.raw[:min(rc, 24)].hex()}")
                    log(f"[audio] best guess: {_guess_audio(codec_id, flags)}")
                    reported = True
                # Open the FIFO non-blocking (retry until ffmpeg opens the read end),
                # then switch it to blocking so each frame is written WHOLE — keeping
                # the AAC/ADTS framing intact (a partial write would corrupt it).
                if fifo_fd < 0 and fifo_path:
                    try:
                        fifo_fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                        fl = fcntl.fcntl(fifo_fd, fcntl.F_GETFL)
                        fcntl.fcntl(fifo_fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
                        log(f"[audio] muxing audio -> {fifo_path}")
                    except OSError:
                        fifo_fd = -1  # no reader yet; try again on the next frame
                if fifo_fd >= 0:
                    try:
                        os.write(fifo_fd, abuf.raw[:rc])
                    except (BrokenPipeError, OSError):
                        try:
                            os.close(fifo_fd)
                        except Exception:  # noqa: BLE001
                            pass
                        fifo_fd = -1  # ffmpeg went away; reopen when it's back
                if time.time() - last_log > 30:
                    log(f"[audio] {frames} audio frames forwarded")
                    last_log = time.time()
            elif rc == AV_ER_DATA_NOREADY:
                if not reported and not warned and time.time() - started > 12:
                    log("[audio] no audio 12s after AUDIOSTART — the cam may use a "
                        "different audio-start IOCTL, or audio is muted. Video unaffected.")
                    warned = True
                time.sleep(0.02)
            else:
                time.sleep(0.03)
    finally:
        if fifo_fd >= 0:
            try:
                os.close(fifo_fd)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# St_IOTCConnectInput — the struct IOTC_Connect_ByUIDEx() takes as arg 3.
#
# This layout is NOT a guess: it was read straight out of the Owlet x86_64
# libIOTCAPIs.so by disassembling both the real IOTC_Connect_ByUIDEx() and its
# JNI wrapper. The exact offsets / sizes the binary enforces:
#
#   0x00 int  structSize         -- MUST equal 0xA0 (160); the C function does
#                                   `cmp [rdx],0xA0; jne -> return -46` first.
#   0x04 int  authenticationType -- must be 0 for the password path.
#   0x08 char authKey[8]         -- the JNI checks strlen(authKey)==8 exactly
#                                   (the Owlet KMS AuthKey is 8 chars).
#   0x10 char deviceRegion[132]  -- left empty.
#   0x94 int  timeout            -- seconds.
#   0x98 int  dataTransmitMode   -- 0 (validated <=2, !=1).
#   0x9c byte lanModeDisable
#   0x9d byte p2pModeDisable
#   total sizeof == 0xA0 (160).
# --------------------------------------------------------------------------- #
class St_IOTCConnectInput(Structure):
    _fields_ = [
        ("structSize", c_int),          # 0x00  must be 160
        ("authenticationType", c_int),  # 0x04
        ("authKey", c_char * 8),        # 0x08  exactly 8 chars
        ("deviceRegion", c_char * 132), # 0x10
        ("timeout", c_int),             # 0x94
        ("dataTransmitMode", c_int),    # 0x98
        ("lanModeDisable", c_byte),     # 0x9c
        ("p2pModeDisable", c_byte),     # 0x9d
        ("_pad", c_byte * 2),           # 0x9e -> 0xA0
    ]


# --------------------------------------------------------------------------- #
# SessionModeVer — the struct IOTC_Session_Check() fills in. Standard TUTK
# IOTCAPIs.h layout: two unsigned ints, no padding.
#   nMode:     0 = P2P, 1 = Relay, 2 = LAN, 0xFFFFFFFF = check failed
#   nApiLevel: remote device's API level (informational only)
# --------------------------------------------------------------------------- #
class SessionModeVer(Structure):
    _fields_ = [
        ("nMode", c_uint),
        ("nApiLevel", c_uint),
    ]


# --------------------------------------------------------------------------- #
# St_AVClientStartInConfig / St_AVClientStartOutConfig — args to avClientStartEx,
# the AV-layer login the Owlet app uses (com.owlet.tutk.AndroidTutkSdk.startClient).
# Layout read straight out of the Owlet libAVAPIs.so (real avClientStartEx + its
# JNI wrapper). Both carry a leading structSize the C side validates:
#   IN  must be 0x40 (64);  OUT must be 0x20 (32).
# Field offsets (IN): 0x04 session, 0x08 channel(byte), 0x0c timeout, 0x10 account*,
#   0x18 password*, 0x20 resend, 0x24 security_mode, 0x28 auth_type,
#   0x2c sync_recv_data, 0x30 dtls_cipher_suites*.
# --------------------------------------------------------------------------- #
class St_AVClientStartInConfig(Structure):
    _fields_ = [
        ("structSize", c_int),                  # 0x00 = 64
        ("iotc_session_id", c_int),             # 0x04
        ("iotc_channel_id", c_byte),            # 0x08 (byte)
        ("_p08", c_byte * 3),                   # 0x09
        ("timeout_sec", c_int),                 # 0x0c
        ("account_or_identity", c_char_p),      # 0x10
        ("password_or_token", c_char_p),        # 0x18
        ("resend", c_int),                      # 0x20
        ("security_mode", c_int),               # 0x24
        ("auth_type", c_int),                   # 0x28
        ("sync_recv_data", c_int),              # 0x2c
        ("dtls_cipher_suites", c_char_p),       # 0x30
        ("_r38", c_int),                        # 0x38
        ("_r3c", c_int),                        # 0x3c  -> 0x40
    ]


class St_AVClientStartOutConfig(Structure):
    # 32 bytes; the SDK only reads structSize (==0x20) on the way in and writes
    # negotiated results (server_type, two_way_streaming, …) we don't need.
    _fields_ = [
        ("structSize", c_int),                  # 0x00 = 32
        ("_rest", c_int * 7),                   # 0x04 -> 0x20
    ]


def log(*a):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(ts, "[tutk]", *a, file=sys.stderr, flush=True)


def _set_sig(lib, name, restype, argtypes):
    """Set restype/argtypes if the symbol exists (prevents 64-bit ptr truncation)."""
    fn = getattr(lib, name, None)
    if fn is not None:
        fn.restype = restype
        fn.argtypes = argtypes
    return fn


def load() -> tuple[CDLL, CDLL, CDLL | None]:
    iotc_p = os.path.join(LIB_DIR, "libIOTCAPIs.so")
    av_p = os.path.join(LIB_DIR, "libAVAPIs.so")
    for p in (iotc_p, av_p):
        if not os.path.exists(p):
            log(f"MISSING {p} — copy the Owlet x86_64 TUTK libs into {LIB_DIR}/")
            sys.exit(2)
    # Preload the ThroughTek dependency libs (globally) so IOTC/AV resolve their
    # symbols. crypto/ssl/curl first, then globals/tunnel/RDT.
    for dep in (
        "libcrypto.so", "libcrypto.so.1.1", "libssl.so", "libssl.so.1.1",
        "libcurl.so", "libTUTKGlobalAPIs.so", "libP2PTunnelAPIs.so", "libRDTAPIs.so",
    ):
        dp = os.path.join(LIB_DIR, dep)
        if os.path.exists(dp):
            try:
                CDLL(dp, mode=ctypes.RTLD_GLOBAL)
                log(f"preloaded {dep}")
            except OSError as e:
                log(f"preload {dep} failed: {e}")
    tutk = None
    tp = os.path.join(LIB_DIR, "libTUTKGlobalAPIs.so")
    if os.path.exists(tp):
        try:
            tutk = CDLL(tp, mode=ctypes.RTLD_GLOBAL)
        except OSError as e:
            log(f"load libTUTKGlobalAPIs failed: {e}")
    return (CDLL(iotc_p, mode=ctypes.RTLD_GLOBAL),
            CDLL(av_p, mode=ctypes.RTLD_GLOBAL),
            tutk)


def smsg_av_stream(channel: int) -> bytes:
    # struct SMsgAVIoctrlAVStream { unsigned int channel; unsigned char reserved[4]; }
    return struct.pack("<I4s", channel, b"\x00\x00\x00\x00")


def _session_mode(iotc: CDLL, session: int) -> int:
    """IOTC_Session_Check(): measure whether this session landed in direct P2P
    (0), relay (1) or LAN (2) mode. Logged unconditionally (TOT-35 wants this in
    the container log for every cycle, not just the ones we act on) so the ops
    audit can correlate mode against how long the session survived. Returns
    IOTC_SESSION_MODE_FAIL if the call is unavailable or errors — callers must
    not enforce on that value, only log it."""
    if not SESSION_CHECK:
        return IOTC_SESSION_MODE_FAIL   # opt-in only (OWLET_SESSION_CHECK=1), see above
    if not hasattr(iotc, "IOTC_Session_Check"):
        log("IOTC_Session_Check not exported by this lib build — mode unknown")
        return IOTC_SESSION_MODE_FAIL
    # The struct this lib build fills is NOT the two-uint SessionModeVer (it
    # overran it and crashed the process in production). Hand it a buffer big
    # enough for any known st_SInfo/st_SInfoEx layout and report the raw head so
    # the real layout can be worked out from logs; the first byte is nMode in the
    # Ex layouts (uint8), which is all the enforcement path looks at.
    buf = create_string_buffer(512)
    rc = iotc.IOTC_Session_Check(session, buf)
    if rc < 0:
        log(f"IOTC_Session_Check -> rc={rc} (mode unknown)")
        return IOTC_SESSION_MODE_FAIL
    head = buf.raw[:32]
    mode = head[0]
    log(f"IOTC_Session_Check -> rc={rc} mode={SESSION_MODE_NAMES.get(mode, f'unknown({mode})')} "
        f"raw[0:32]={head.hex()}")
    return mode


def stream_once(uid: str, sec_mode: int) -> int:
    iotc, av, tutk = load()

    # ---- bind signatures (critical on x86_64 — default int return truncates ptrs)
    _set_sig(iotc, "IOTC_Set_LanSearchPort", c_int, [c_int])
    _set_sig(iotc, "IOTC_Setup_Session_Alive_Timeout", c_int, [c_uint])
    _set_sig(iotc, "IOTC_Initialize2", c_int, [c_uint])
    _set_sig(iotc, "IOTC_Get_SessionID", c_int, [])
    _set_sig(iotc, "IOTC_Connect_ByUIDEx", c_int,
             [c_char_p, c_int, POINTER(St_IOTCConnectInput)])
    _set_sig(iotc, "IOTC_Connect_ByUID_Parallel", c_int, [c_char_p, c_int])
    _set_sig(iotc, "IOTC_Session_Close", c_int, [c_int])
    _set_sig(iotc, "IOTC_Session_Check", c_int, [c_int, c_void_p])
    _set_sig(iotc, "IOTC_DeInitialize", c_int, [])
    _set_sig(av, "avInitialize", c_int, [c_int])
    _set_sig(av, "avDeInitialize", c_int, [])
    _set_sig(av, "avClientStart2", c_int,
             [c_int, c_char_p, c_char_p, c_uint, POINTER(c_uint), c_ubyte,
              POINTER(c_uint)])
    _set_sig(av, "avClientStartEx", c_int,
             [POINTER(St_AVClientStartInConfig), POINTER(St_AVClientStartOutConfig)])
    _set_sig(av, "avSendIOCtrl", c_int, [c_int, c_uint, c_char_p, c_int])
    if hasattr(av, "avRecvIOCtrl"):
        _set_sig(av, "avRecvIOCtrl", c_int,
                 [c_int, POINTER(c_int), c_char_p, c_int, c_int])
    _set_sig(av, "avClientStop", c_int, [c_int])
    _set_sig(av, "avRecvFrameData2", c_int,
             [c_int, c_char_p, c_int, POINTER(c_int), POINTER(c_int),
              c_char_p, c_int, POINTER(c_int), POINTER(c_int)])
    _set_sig(av, "avRecvAudioData", c_int,
             [c_int, c_char_p, c_int, c_char_p, c_int, POINTER(c_uint)])
    _set_sig(av, "avSendAudioData", c_int,
             [c_int, c_char_p, c_int, c_char_p, c_int])
    # Two-way talk uses the AV-SERVER model (the app does the same): open a server
    # on a free channel of the live session and push audio there, NOT on the video
    # client channel. Bind the calls the talk path needs (guarded — older libs may
    # lack some, in which case talk falls back to the legacy client-channel send).
    _set_sig(iotc, "IOTC_Session_Get_Free_Channel", c_int, [c_int])
    _set_sig(iotc, "IOTC_Session_Channel_OFF", c_int, [c_int, c_int])
    _set_sig(av, "avServStart2", c_int,
             [c_int, c_char_p, c_char_p, c_int, c_int, c_int])
    _set_sig(av, "avServExit", c_int, [c_int, c_int])
    _set_sig(av, "avServStop", c_int, [c_int])
    if tutk is not None:
        _set_sig(tutk, "TUTK_SDK_Set_License_Key", c_int, [c_char_p])
        _set_sig(tutk, "TUTK_SDK_Set_Region", c_int, [c_int])

    # ---- license + region the newer Kalay SDK requires BEFORE init -----------
    if tutk is not None and hasattr(tutk, "TUTK_SDK_Set_License_Key"):
        rc = tutk.TUTK_SDK_Set_License_Key(LICENSE_KEY.encode())
        log(f"TUTK_SDK_Set_License_Key rc={rc}")
    else:
        log("WARNING: libTUTKGlobalAPIs/TUTK_SDK_Set_License_Key unavailable")

    if hasattr(iotc, "IOTC_Set_LanSearchPort"):
        log(f"IOTC_Set_LanSearchPort(63616) rc={iotc.IOTC_Set_LanSearchPort(63616)}")
    if hasattr(iotc, "IOTC_Setup_Session_Alive_Timeout"):
        log("IOTC_Setup_Session_Alive_Timeout(20) rc="
            f"{iotc.IOTC_Setup_Session_Alive_Timeout(20)}")
    if tutk is not None and hasattr(tutk, "TUTK_SDK_Set_Region"):
        log(f"TUTK_SDK_Set_Region({REGION_CODE}) rc={tutk.TUTK_SDK_Set_Region(REGION_CODE)}")

    rc = iotc.IOTC_Initialize2(0)
    log(f"IOTC_Initialize2 rc={rc}")
    if rc < 0 and rc != IOTC_ER_ALREADY_INITIALIZED:
        return 3
    av.avInitialize(512)

    session = -1
    av_idx = -1
    audio_stop = threading.Event()
    loop_done = threading.Event()                  # stops the stall supervisor
    vclock = {"video_at": time.time(), "frames": 0}  # shared with it
    audio_thr = None
    talk_thr = None
    sensors_thr = None
    vol_thr = None
    player_thr = None
    try:
        log(f"connecting UID={uid[:4]}…{uid[-2:]} authKey={'yes' if AUTHKEY else 'no'} …")
        ak = AUTHKEY.encode()
        if AUTHKEY and len(ak) != 8:
            log(f"WARNING: authKey is {len(ak)} chars; the SDK requires exactly 8 "
                "(IOTC_Connect_ByUIDEx will reject it with -46)")

        # TOT-35: 23 of 25 logged connect cycles authenticated cleanly, connected,
        # then died silently at 30-90s (video stops, audio keeps flowing, no
        # error) — theory is those sessions landed in TUTK relay mode instead of
        # direct P2P. Measure every session with IOTC_Session_Check() and, if
        # enforcement is on, tear down + reconnect a relay session for a bounded
        # number of attempts before accepting whatever we get (a network that
        # genuinely can't reach P2P must still eventually get a stream).
        session = -1
        mode = IOTC_SESSION_MODE_FAIL
        max_attempts = P2P_RETRY_LIMIT + 1
        for attempt in range(1, max_attempts + 1):
            sid = iotc.IOTC_Get_SessionID()
            log(f"IOTC_Get_SessionID -> {sid}")
            if sid < 0:
                return 3

            inp = St_IOTCConnectInput()
            inp.structSize = 160               # 0xA0 — required size/version guard
            inp.authenticationType = 0
            inp.authKey = ak[:8]
            inp.timeout = CONNECT_TIMEOUT
            session = iotc.IOTC_Connect_ByUIDEx(uid.encode(), sid, byref(inp))
            log(f"IOTC_Connect_ByUIDEx -> {session}")
            if session < 0 and hasattr(iotc, "IOTC_Connect_ByUID_Parallel"):
                # Fallback for cams that don't require the authKey.
                session = iotc.IOTC_Connect_ByUID_Parallel(uid.encode(), sid)
                log(f"IOTC_Connect_ByUID_Parallel -> {session}")
            if session < 0:
                log(f"connect failed: {session}")
                return 4

            mode = _session_mode(iotc, session)
            if mode == 1 and REQUIRE_P2P and attempt < max_attempts:
                log(f"session landed in Relay mode (attempt {attempt}/{max_attempts}) "
                    "— tearing down and reconnecting for direct P2P")
                closing = session
                _safe(lambda: iotc.IOTC_Session_Close(closing))
                session = -1
                time.sleep(P2P_RETRY_WAIT)
                continue
            if mode == 1 and not REQUIRE_P2P:
                log("session landed in Relay mode — enforcement is off "
                    "(OWLET_REQUIRE_P2P=0), accepting it and logging only")
            elif mode == 1:
                log(f"still Relay mode after {max_attempts} attempt(s) — accepting it "
                    "rather than retrying forever")
            break

        log(f"IOTC session={session} mode={SESSION_MODE_NAMES.get(mode, 'unknown')}")

        av_idx = av_client_start(av, session, sec_mode)
        if av_idx < 0:
            return 5

        payload = smsg_av_stream(AV_CHANNEL)
        av.avSendIOCtrl(av_idx, IOTYPE_START, c_char_p(payload), len(payload))
        log(f"sent IOTYPE_START={IOTYPE_START} payload={payload.hex()}")

        # Kick off audio capture alongside the video loop. It reports the codec and,
        # when the exec gave us an OWLET_AUDIO_FIFO, feeds AAC into it for muxing.
        # Always run when a FIFO is present so ffmpeg's audio input never starves.
        if AUDIO_ENABLED or os.environ.get("OWLET_AUDIO_FIFO"):
            try:
                ap = smsg_av_stream(AV_CHANNEL)
                av.avSendIOCtrl(av_idx, IOTYPE_AUDIOSTART, c_char_p(ap), len(ap))
                log(f"sent IOTYPE_AUDIOSTART={IOTYPE_AUDIOSTART}")
                audio_thr = threading.Thread(
                    target=_audio_probe, args=(av, av_idx, audio_stop), daemon=True)
                audio_thr.start()
            except Exception as e:  # noqa: BLE001
                log(f"[audio] probe start failed: {e}")

        # Talk / sound-playback: play AAC arriving on the talk FIFO out the speaker.
        has_send = hasattr(av, "avSendAudioData")
        log(f"[talk] avSendAudioData={'found' if has_send else 'NOT FOUND in lib'} "
            f"TALK_FIFO={TALK_FIFO!r}")
        if TALK_FIFO:
            try:
                talk_thr = threading.Thread(
                    target=_talk_thread, args=(av, iotc, session, av_idx, audio_stop),
                    daemon=True)
                talk_thr.start()
                log(f"[talk] thread started — listening on {TALK_FIFO}")
            except Exception as e:  # noqa: BLE001
                log(f"[talk] start failed: {e}")

        # Speaker volume: watch the volume control file and push SET_SPK_VOL
        # (0x60092) to the camera when it changes. Opt-in — only runs if a volume
        # file or an initial OWLET_SPK_VOL is configured, so default behavior is
        # unchanged. Sends a benign config IOCTL (not a channel op), serialized by
        # _AV_IO like every other IOCTL.
        if (VOL_FILE or SPK_VOL_INITIAL >= 0) and hasattr(av, "avSendIOCtrl"):
            try:
                vol_thr = threading.Thread(
                    target=_volume_thread, args=(av, av_idx, audio_stop), daemon=True)
                vol_thr.start()
                log(f"[vol] speaker-volume control active (file={VOL_FILE!r})")
            except Exception as e:  # noqa: BLE001
                log(f"[vol] start failed: {e}")

        # Native audio player (camera built-in lullabies). Inert until the UI
        # drops a command in AUDIOCMD_FILE — sends no IOCTL traffic by default.
        if AUDIOCMD_FILE and hasattr(av, "avSendIOCtrl"):
            try:
                player_thr = threading.Thread(
                    target=_audioplayer_thread, args=(av, av_idx, audio_stop),
                    daemon=True)
                player_thr.start()
                log(f"[lullaby] native audio-player control active "
                    f"(file={AUDIOCMD_FILE!r})")
            except Exception as e:  # noqa: BLE001
                log(f"[lullaby] start failed: {e}")

        # Room sensors: poll the GetRealtimeData IOCTL for humidity + brightness
        # (temp/noise/motion/sound already ride the frame info).
        if CAM_SENSORS_PATH and SENSORS_ENABLED:
            try:
                sensors_thr = threading.Thread(
                    target=_realtime_thread, args=(av, av_idx, audio_stop), daemon=True)
                sensors_thr.start()
                log(f"[sensors] publishing room sensors to {CAM_SENSORS_PATH}")
            except Exception as e:  # noqa: BLE001
                log(f"[sensors] start failed: {e}")

        buf = create_string_buffer(FRAME_BUF)
        finfo = create_string_buffer(FINFO_BUF)
        actual = c_int(0); expected = c_int(0); finfo_len = c_int(0); frmno = c_int(0)
        out = sys.stdout.buffer
        frames = 0
        last_log = time.time()
        last_data = time.time()
        vclock["video_at"] = last_data
        threading.Thread(target=_stall_supervisor, name="stall-supervisor",
                         args=(av, av_idx, vclock, loop_done), daemon=True).start()
        while True:
            rc = av.avRecvFrameData2(av_idx, buf, FRAME_BUF,
                                     byref(actual), byref(expected),
                                     finfo, FINFO_BUF, byref(finfo_len), byref(frmno))
            if rc >= 0 and actual.value > 0:
                # zero-copy: write a view of just the frame bytes, not a full
                # 2 MB .raw copy of the whole buffer every frame (matters at 25fps).
                out.write(memoryview(buf)[:actual.value]); out.flush()
                frames += 1
                if not _SESSION_VIDEO[0]:
                    _on_first_frame()
                last_data = time.time()
                vclock["video_at"] = last_data
                vclock["frames"] = frames
                # temp/noise/motion/sound ride in the extended frame-info struct
                _parse_frame_sensors(finfo.raw, finfo_len.value)
                if time.time() - last_log > 15:
                    log(f"{frames} frames forwarded"); last_log = time.time()
            elif rc == AV_ER_DATA_NOREADY:
                # No frame yet. If it's been silent too long this session is a dud
                # (or has silently stalled) — bail so main() waits out the camera's
                # session hold and reconnects, instead of hanging here forever (which
                # used to require a manual "Connect & Diagnose" in the UI).
                if time.time() - last_data > NO_VIDEO_TIMEOUT:
                    log(f"no video for {NO_VIDEO_TIMEOUT}s (frames={frames}) — dud/"
                        "stalled session; reconnecting after the camera releases it")
                    return 6
                time.sleep(0.01)
            elif rc in (AV_ER_REMOTE_TIMEOUT_DISCONNECT, AV_ER_SESSION_CLOSE_BY_REMOTE):
                log(f"stream ended rc={rc}"); break
            else:
                if time.time() - last_data > NO_VIDEO_TIMEOUT:
                    log(f"no video for {NO_VIDEO_TIMEOUT}s — session stalled; reconnecting")
                    return 6
                time.sleep(0.02)
        return 0
    finally:
        loop_done.set()   # the stall supervisor must not act during teardown
        # Signal the worker threads, then avClientStop FIRST — that unblocks any
        # thread parked inside avRecvAudioData/avRecvIOCtrl (which have their own
        # timeouts but can sit longer than the join). Joining BEFORE the stop
        # risked deInitialize() running while a worker was still in the C lib
        # (use-after-free / segfault on reconnect).
        audio_stop.set()
        if av_idx >= 0:
            try:
                av.avClientStop(av_idx)
            except Exception:  # noqa: BLE001
                pass
        for thr in (audio_thr, talk_thr, sensors_thr, vol_thr, player_thr):
            if thr is not None:
                thr.join(timeout=3)
                if thr.is_alive():
                    log(f"[warn] {thr.name!r} still alive after 3s — possible thread "
                        "leak (avSendAudioData/avSendIOCtrl may be hung in C lib)")
        # Always tear down so the next attempt's IOTC_Initialize2 doesn't return
        # -3 (ALREADY_INITIALIZED) and avClientStart doesn't see a stale channel.
        if session >= 0:
            iotc.IOTC_Session_Close(session)
        av.avDeInitialize()
        iotc.IOTC_DeInitialize()


def _main_wchan() -> str:
    """Kernel wait channel of the main thread — tells a blocked pipe write
    (pipe_write) from a blocked socket/futex wait inside the TUTK lib."""
    try:
        tid = threading.main_thread().native_id or os.getpid()
        with open(f"/proc/self/task/{tid}/wchan") as fh:
            return fh.read().strip() or "0"
    except Exception:  # noqa: BLE001
        return "?"


def _cam_name() -> str:
    """This stream's camera name, recovered from the talk FIFO path the exec sets
    (${TMPDIR}/owlet-talk-<name>)."""
    base = os.path.basename(os.environ.get("OWLET_TALK_FIFO", ""))
    return base[len("owlet-talk-"):] if base.startswith("owlet-talk-") else ""


def _stdout_readers() -> list:
    """PIDs whose stdin is the pipe we write video into — i.e. the ffmpeg go2rtc
    spawned next to us. Found by pipe inode so we never guess by name."""
    try:
        ino = os.fstat(1).st_ino
    except Exception:  # noqa: BLE001
        return []
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            if os.readlink(f"/proc/{d}/fd/0") == f"pipe:[{ino}]":
                out.append(int(d))
        except Exception:  # noqa: BLE001
            continue
    return out


def _reset_go2rtc_stream(name: str) -> None:
    """Point this camera's go2rtc stream name at a fresh, never-used spare stream
    object (PATCH alias — see config_store.go2rtc_swap_to_spare). Evidence
    2026-09-12 10:07: on a stall the main thread was in pipe_write, ffmpeg was
    stuck in send() with 2.6 MB unsent, and go2rtc had stopped reading its producer
    (fan-out wedged after a Frigate restart). Killing our processes did not help —
    go2rtc kept the dead producer listed and never relaunched the exec — only a
    container restart did. go2rtc's API refuses to create exec sources (PUT ->
    400), but aliasing to a config-defined spare gives every new consumer a fresh
    object whose exec launches on demand (verified on an isolated go2rtc 1.9.4)."""
    if not name:
        return
    try:
        import config_store as cs
        cs.go2rtc_swap_to_spare(name, log=lambda m: log("[stall] " + m))
    except Exception as e:  # noqa: BLE001
        log(f"[stall] go2rtc spare swap failed: {e}")


def _kill_downstream(pids: list) -> None:
    """SIGTERM then SIGKILL the ffmpeg reading our stdout, so its socket to go2rtc
    closes and the old producer is really gone."""
    import signal as _sig
    for sig, wait in ((_sig.SIGTERM, 2.0), (_sig.SIGKILL, 0.0)):
        alive = []
        for p in pids:
            try:
                os.kill(p, sig)
                alive.append(p)
            except ProcessLookupError:
                pass
            except Exception as e:  # noqa: BLE001
                log(f"[stall] kill {p}: {e}")
        if not alive:
            return
        end = time.time() + wait
        while time.time() < end and any(os.path.exists(f"/proc/{p}") for p in alive):
            time.sleep(0.1)
        pids = [p for p in alive if os.path.exists(f"/proc/{p}")]
        if not pids:
            return
    log(f"[stall] downstream ffmpeg still present after SIGKILL: {pids}")


def _stall_supervisor(av: CDLL, av_idx: int, st: dict, done: threading.Event) -> None:
    """Watch the video-frame clock from a side thread (see STALL_TIMEOUT).

    stream_once()'s own NO_VIDEO_TIMEOUT only works while the main thread keeps
    returning from avRecvFrameData2 / the stdout write. When either blocks this
    is the only thing that notices. Ladder:
      1. log it, with the main thread's wait channel + every thread's Python
         stack (the evidence TOT-35 was missing);
      2. avClientStop() — unblocks a recv parked in the lib; the frame loop then
         sees an error, hits its own timeout and reconnects CLEANLY (session
         released, fresh KMS key, normal RECONNECT_WAIT);
      3. if the main thread still hasn't come back after STALL_UNBLOCK_WAIT (e.g.
         it's stuck in the pipe write), os._exit so go2rtc relaunches the exec.
         The camera drops the abandoned slot after its 20s alive timeout, which
         the relaunched process's dud-session handling already waits out."""
    limit = max(STALL_TIMEOUT, NO_VIDEO_TIMEOUT + 10)
    while not done.wait(1.0):
        idle = time.time() - st["video_at"]
        if idle < limit:
            continue
        log(f"[stall] no video forwarded for {int(idle)}s (frames={st['frames']}) and "
            f"the frame loop never hit its own {NO_VIDEO_TIMEOUT}s timeout — main "
            f"thread is blocked (wchan={_main_wchan()}); dumping thread stacks")
        try:
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        log("[stall] avClientStop() to break the blocked receive …")
        try:
            rc = av.avClientStop(av_idx)
            log(f"[stall] avClientStop -> {rc}")
        except Exception as e:  # noqa: BLE001
            log(f"[stall] avClientStop raised: {e}")
        if done.wait(STALL_UNBLOCK_WAIT):
            log("[stall] frame loop is back — reconnecting normally")
            return
        log(f"[stall] still blocked {STALL_UNBLOCK_WAIT}s after avClientStop — replacing "
            "the go2rtc stream, killing the downstream ffmpeg, then hard exit so the "
            "stream relaunches with a fresh key")
        # Order matters: give go2rtc a fresh Stream object first (new consumers land
        # on it), then close the old producer's socket by killing its ffmpeg, then
        # die ourselves. The old wrapper exits on its own once both pipe ends are gone.
        readers = _stdout_readers()
        _reset_go2rtc_stream(_cam_name())
        if readers:
            log(f"[stall] killing downstream ffmpeg {readers} (it stopped draining our pipe)")
            _kill_downstream(readers)
        else:
            log("[stall] no downstream ffmpeg found on our stdout pipe")
        try:
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(6)


SEC_NAMES = {0: "Simple", 1: "Dtls", 2: "Auto"}


def av_client_start(av: CDLL, session: int, mode: int) -> int:
    """AV-layer login the Owlet app uses: avClientStartEx with the given DTLS
    security mode. Falls back to the legacy scalar avClientStart2 if Ex is
    missing from the lib."""
    if hasattr(av, "avClientStartEx"):
        cin = St_AVClientStartInConfig()
        cin.structSize = 64
        cin.iotc_session_id = session
        cin.iotc_channel_id = AV_CHANNEL
        cin.timeout_sec = CONNECT_TIMEOUT
        cin.account_or_identity = AV_ACCOUNT
        cin.password_or_token = AV_PASSWORD
        cin.resend = 1
        cin.security_mode = mode
        cin.auth_type = AV_AUTH_TYPE
        cin.sync_recv_data = AV_SYNC_RECV
        cin.dtls_cipher_suites = None
        cout = St_AVClientStartOutConfig()
        cout.structSize = 32
        av_idx = av.avClientStartEx(byref(cin), byref(cout))
        log(f"avClientStartEx(security={SEC_NAMES.get(mode, mode)}, "
            f"auth={AV_AUTH_TYPE}) -> {av_idx}")
        if av_idx >= 0:
            log(f"AV channel={av_idx}")
        return av_idx

    # Legacy fallback (no Ex export).
    serv = c_uint(0); resend = c_uint(1)
    av_idx = av.avClientStart2(session, c_char_p(AV_ACCOUNT), c_char_p(AV_PASSWORD),
                              c_uint(CONNECT_TIMEOUT), byref(serv), c_ubyte(AV_CHANNEL),
                              byref(resend))
    log(f"avClientStart2 -> {av_idx}")
    if av_idx >= 0:
        log(f"AV channel={av_idx} servType={serv.value}")
    return av_idx


# Login-storm brake (see login_guard.py): at most ONE automatic Firebase
# password login per connection attempt, never after a connection that
# delivered video, and only when the account-wide guard allows it
# (exponential backoff, hard cap OWLET_LOGIN_MAX_ATTEMPTS). The guard state is
# shared by every stream process and survives relaunches, so a long-lived
# process can refresh a rotated key once the backoff has elapsed without
# being able to storm the account.
_HAD_VIDEO = [False]           # this process has forwarded at least one frame
_SESSION_VIDEO = [False]       # the current stream_once session has (reset by main)
_SUCCESS_RECORDED = [False]    # record_success() done for this session (reset by main)
_GUARD_ERR_LOGGED = [False]    # a record_success() failure was already logged
_KEY_REFRESHED = [False]       # resolve_creds() fetched a NEW key (taken over by main)
_SESSION_FRESH_KEY = [False]   # the current session uses that freshly fetched key


def _on_first_frame() -> None:
    """First forwarded frame of a session. If this session runs on a key that
    an automatic refresh fetched for this very connection attempt, that login
    evidently worked, so reset the account-wide guard's escalation, once per
    SESSION: a long-lived process may refresh a rotated key on a later
    connection attempt (each refresh counts against the guard's cap), and the
    video that follows must reset it again, or a few successful rotations
    would exhaust the cap for good. Video from the key the process already
    held says nothing about the cloud login (the failure that triggered it
    was transient) and leaves the guard alone; so does another camera's video.
    Called from the frame loop, so it must never raise; a failure is logged
    once per process."""
    _SESSION_VIDEO[0] = True
    _HAD_VIDEO[0] = True
    if _SUCCESS_RECORDED[0] or not _SESSION_FRESH_KEY[0]:
        return
    _SUCCESS_RECORDED[0] = True
    try:
        import login_guard
        login_guard.record_success()
    except Exception as e:  # noqa: BLE001
        if not _GUARD_ERR_LOGGED[0]:
            _GUARD_ERR_LOGGED[0] = True
            log(f"could not reset the login guard after video ({e}); continuing")


def _refresh_allowed() -> bool:
    return os.environ.get("OWLET_REFRESH_KMS", "1") != "0"


def resolve_creds(refresh: bool = True) -> str:
    """Return the camera UID, optionally refreshing UID/AuthKey/AV-password
    from the Owlet KMS first. Makes at most ONE cloud login per call, and
    main() calls it at most once per connection attempt.

    The saved key is the default. A cloud refresh (a full Firebase *password*
    login + KMS fetch) happens only when ALL of these hold:
      * `refresh` (main() asks only at a start with no saved UID, or after a
        connection attempt that produced no video and failed in a way a
        rotated key could explain);
      * OWLET_REFRESH_KMS is not "0" and DSN + account credentials exist;
      * login_guard allows it (account-wide exponential backoff + hard cap,
        shared by every stream process and surviving relaunches).
    A fetched key is persisted to the config (config_store.persist_camera_key),
    so later starts and reconnects reuse it instead of logging in again.
    """
    global AUTHKEY, AV_PASSWORD, UID
    dsn = os.environ.get("OWLET_CAMERA_DSN", "").strip()
    email = os.environ.get("OWLET_EMAIL")
    pw = os.environ.get("OWLET_PASSWORD")
    region = os.environ.get("OWLET_REGION", "world")
    want = bool(refresh and dsn and email and pw)
    if want and not _refresh_allowed():
        log("KMS refresh skipped (refresh disabled: OWLET_REFRESH_KMS=0); using the saved key")
        want = False
    if want:
        try:
            import login_guard
            ok, why = login_guard.try_acquire()
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"login guard error: {e}"
        if not ok:
            log(f"KMS refresh skipped ({why}); using the saved key")
            want = False
    if want:
        try:
            from owlet_api import OwletAPI
            api = OwletAPI(region, email, pw, log=lambda m: log(m))
            creds = api.camera_credentials(dsn)
            if creds.get("uid"):
                held = (UID, AUTHKEY, AV_PASSWORD)
                if creds.get("authkey"):
                    AUTHKEY = creds["authkey"]
                if creds.get("av_password"):
                    AV_PASSWORD = creds["av_password"].encode()
                log("refreshed camera key from KMS")
                UID = creds["uid"]   # later fallbacks reuse the fresh key
                _persist_key(creds)
                # Only a key that differs from the one we held can make the
                # next session's video evidence that this login was needed
                # and worked; an identical key means the failure was transient.
                _KEY_REFRESHED[0] = (UID, AUTHKEY, AV_PASSWORD) != held
                return creds["uid"]
        except Exception as e:  # noqa: BLE001
            log(f"KMS refresh failed ({e}); falling back to the saved key")
    if UID:
        return UID
    log("No saved camera UID and no cloud refresh allowed right now. Set the Camera "
        "DSN + account login in the UI and use 'Connect / refresh key'.")
    time.sleep(30)   # don't let go2rtc relaunch us in a tight loop
    sys.exit(1)


def _persist_key(creds: dict) -> None:
    """Save a fetched key so restarts/reconnects reuse it; if the config asks
    for it (OWLET_FORGET_ACCOUNT), the account credentials are blanked then and
    this process stops being able to log in at all. A persistence failure is
    logged as an error; the stream carries on with the in-memory key."""
    name = os.environ.get("OWLET_CAMERA_NAME", "")
    if not name:
        return
    try:
        import config_store as cs
        cs.persist_camera_key(name, creds)
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: could not persist the refreshed key ({e}); continuing with the "
            "in-memory key, the next restart will use the old saved key")
        return
    try:
        cfg = cs.load_config()
        if not (cfg.get("email") and cfg.get("password")):
            os.environ.pop("OWLET_EMAIL", None)
            os.environ.pop("OWLET_PASSWORD", None)
            log("account credentials removed from config (OWLET_FORGET_ACCOUNT) — "
                "no further cloud logins")
    except Exception as e:  # noqa: BLE001
        log(f"could not re-read the config after saving the key: {e}")


_SHUTTING_DOWN = {"flag": False, "logged": False}


def _on_sigterm(*_a) -> None:
    """First SIGTERM: mark shutting down and raise SystemExit so stream_once()'s
    finally releases the camera session cleanly. Any later SIGTERM is ignored
    (logged once): raising again would interrupt that very teardown and leave
    the camera holding a dead session for ~20 s."""
    if _SHUTTING_DOWN["flag"]:
        if not _SHUTTING_DOWN["logged"]:
            _SHUTTING_DOWN["logged"] = True
            log("SIGTERM again during shutdown — ignored, session teardown continues")
        return
    _SHUTTING_DOWN["flag"] = True
    sys.exit(0)


def main() -> None:
    # On SIGTERM (go2rtc stopping the source / container shutdown) raise SystemExit
    # so stream_once()'s finally releases the camera session cleanly — otherwise the
    # cam holds the single P2P slot and the next connect is refused.
    import signal
    signal.signal(signal.SIGTERM, _on_sigterm)
    # Saved key first; log in at start only if there is no saved UID at all.
    uid = resolve_creds(refresh=not UID)
    last_kms = time.monotonic()
    # Security mode to try. If pinned via env, use only that; otherwise cycle
    # [Auto, Dtls, Simple] across reconnects — each on a FRESH session so a
    # failed DTLS handshake on one mode can't poison the next attempt.
    modes = [int(AV_SECURITY_MODE)] if AV_SECURITY_MODE != "" else [2, 1, 0]
    i = 0
    while True:
        mode = modes[i % len(modes)]
        backoff = 5
        try:
            _SESSION_VIDEO[0] = False
            _SUCCESS_RECORDED[0] = False   # at most one record_success per session
            # A key fetched by the refresh of the previous attempt counts for
            # this session only; any later session runs on a held key.
            _SESSION_FRESH_KEY[0], _KEY_REFRESHED[0] = _KEY_REFRESHED[0], False
            rc = stream_once(uid, mode)
            if rc == 5:
                # AV login failed for this security mode — advance to the next.
                i += 1
                log(f"AV login failed (security={SEC_NAMES.get(mode, mode)}); "
                    f"trying {SEC_NAMES.get(modes[i % len(modes)])} next")
            elif rc == 6:
                # Connected but got no video — the camera's single session was still
                # held. Wait past its ~20s session-alive-timeout so it fully releases;
                # reconnecting sooner just races it and gets another empty session.
                backoff = RECONNECT_WAIT
                log(f"no-video session — waiting {RECONNECT_WAIT}s for the camera to "
                    "release its session before reconnecting")
            elif rc != 0:
                log(f"exit rc={rc}; retry in 5s")
            # If this session ran on a freshly fetched key,
            # login_guard.record_success() already ran at its first forwarded
            # frame (_on_first_frame), so a SIGTERM or broken pipe after video
            # still resets the guard's escalation.
            #
            # At most one refresh per connection attempt, and only when that
            # attempt (made with the key we hold) produced no video AND failed
            # at connect (4) or AV login (5), which a rotated key could
            # explain. A no-video session (6) is the camera's slot still being
            # held; a new key can't fix that. resolve_creds() then applies
            # OWLET_REFRESH_KMS and the account-wide backoff/cap.
            if rc in (4, 5):
                if _SESSION_VIDEO[0]:
                    log("KMS refresh skipped (previous session had video; the key works)")
                elif time.monotonic() - last_kms >= 15:
                    uid = resolve_creds()
                    last_kms = time.monotonic()
                else:
                    log("KMS refresh skipped (last key check under 15s ago)")
        except SystemExit:
            raise
        except BrokenPipeError:
            # Our stdout consumer (ffmpeg) died — e.g. go2rtc recycled the stream.
            # Don't sit here writing into a dead pipe (the old behaviour looped
            # forever, so go2rtc never restarted the exec and Frigate got nothing
            # for hours). Exit so go2rtc relaunches the whole pipeline with a fresh
            # ffmpeg. Back off briefly first: stream_once's finally just released
            # the camera's single P2P slot, and this pause lets it settle so the
            # relaunched process doesn't immediately reconnect into a still-held
            # slot and churn the camera.
            log("downstream ffmpeg closed the pipe — backing off, then exiting for "
                "a clean go2rtc restart")
            time.sleep(5)
            return
        except Exception as e:  # noqa: BLE001
            log(f"error: {e}; retry in 5s")
        time.sleep(backoff)


if __name__ == "__main__":
    main()
