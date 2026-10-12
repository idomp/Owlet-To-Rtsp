#!/usr/bin/env python3
"""
webapp.py — the owlet-bridge control panel (wyze-bridge style), now multi-camera.

Everything configurable in the browser:
  - Owlet account credentials + region (shared by every camera)
  - Any number of cameras, each added by its DSN — "Connect" runs the REAL Owlet
    login + camera-key (KMS) fetch and streams every step to a live log pane
  - Each camera becomes its own go2rtc stream at rtsp://host:8554/<name>

Runs on :8088. go2rtc serves the actual video + its own UI on :1984.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request

import config_store as cs

app = Flask(__name__)


def _mb_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default)) * 1024 * 1024
    except ValueError:
        return default * 1024 * 1024


# Owlet APK bundles (.apkm/.xapk) are large, but not unbounded: the APK upload
# is capped at OWLET_UPLOAD_MAX_MB (default 512 MB) and is the largest body any
# endpoint accepts. Sounds / talk clips have their own, much smaller cap.
UPLOAD_MAX = _mb_env("OWLET_UPLOAD_MAX_MB", 512)
SOUND_MAX = _mb_env("OWLET_SOUND_MAX_MB", 50)
app.config["MAX_CONTENT_LENGTH"] = UPLOAD_MAX + 1024 * 1024

# Quiet the per-request access log so go2rtc / TUTK output is visible in
# `docker logs` (the status/findings polling was drowning everything out).
import logging  # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.WARNING)

GO2RTC_API = os.environ.get("GO2RTC_API", "http://127.0.0.1:1984")
TUTK_LIB_DIR = os.environ.get("TUTK_LIB_DIR", "/app/libs/x86_64")
# Host-facing ports for the copy URLs shown in the UI. Default to the in-container
# ports; set these to the mapped host ports (e.g. 18554/1985/18555) when they're
# remapped to coexist with Frigate, so the UI shows reachable URLs.
PUBLIC_HTTP_PORT = os.environ.get("PUBLIC_HTTP_PORT", "1984")
PUBLIC_RTSP_PORT = os.environ.get("PUBLIC_RTSP_PORT", "8554")
PUBLIC_WEBRTC_PORT = os.environ.get("PUBLIC_WEBRTC_PORT", "8555")

# Optional Web UI auth (off by default). Set both to require HTTP Basic auth.
UI_USER = os.environ.get("OWLET_UI_USER", "")
UI_PASS = os.environ.get("OWLET_UI_PASS", "")

LOG: deque[str] = deque(maxlen=6000)
STATE: dict = {"candidates": [], "devices": None, "busy": False, "cam_busy": set()}
# Serialize config writes — the background per-camera diagnose threads and the
# request handlers can otherwise interleave json.dump() and corrupt the file.
_CFG_LOCK = threading.Lock()


def log(msg: str = "") -> None:
    ts = time.strftime("%H:%M:%S")
    for line in str(msg).splitlines() or [""]:
        LOG.append(f"{ts}  {line}")


# --------------------------------------------------------------------------- #
# optional auth + CSRF
# --------------------------------------------------------------------------- #
# UI auth creds come from env (UI_USER/UI_PASS) or the saved config (ui_user/
# ui_pass). Cache the config read for a few seconds so _auth isn't a file read
# on every request.
_AUTH = {"t": 0.0, "user": "", "pass": ""}


def _ui_creds() -> tuple[str, str]:
    if UI_USER and UI_PASS:
        return UI_USER, UI_PASS
    now = time.time()
    if now - _AUTH["t"] > 5:
        try:
            cfg = cs.load_config()
            _AUTH["user"] = cfg.get("ui_user") or ""
            _AUTH["pass"] = cfg.get("ui_pass") or ""
        except Exception:  # noqa: BLE001
            pass
        _AUTH["t"] = now
    return _AUTH["user"], _AUTH["pass"]


@app.before_request
def _auth():
    # CSRF: block cross-site mutating requests. Browsers send Origin/Referer on
    # POST/DELETE; native app clients and curl don't, so they're unaffected.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        src = request.headers.get("Origin") or request.headers.get("Referer")
        if src and urlparse(src).netloc and urlparse(src).netloc != request.host:
            return Response("cross-origin request blocked", 403)
    user, pw = _ui_creds()
    if not (user and pw):
        return None
    a = request.authorization
    if a and hmac.compare_digest(a.username or "", user) \
            and hmac.compare_digest(a.password or "", pw):
        return None
    return Response("auth required", 401, {"WWW-Authenticate": 'Basic realm="owlet-bridge"'})


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def save_config(cfg: dict, regenerate: bool = True) -> bool:
    with _CFG_LOCK:
        ok = cs.save_config(cfg, regenerate=regenerate)
    if ok:
        return True
    log(f"!! CANNOT WRITE {cs.CONFIG_DIR}. The mounted config folder isn't writable "
        "by this container — on Unraid run `chmod -R 777 "
        "/mnt/user/appdata/owlet/config` and restart. Settings won't persist until then.")
    return False


def _mask_pw(v: str) -> str:
    return "********" if v else ""


@app.get("/api/config")
def get_config():
    """Account-level settings only (cameras have their own endpoint)."""
    cfg = cs.load_config()
    out = {k: cfg.get(k, cs.ACCOUNT_DEFAULTS[k]) for k in cs.ACCOUNT_FIELDS}
    out["password"] = _mask_pw(out.get("password"))
    out["ui_pass"] = _mask_pw(out.get("ui_pass"))
    return jsonify(out)


@app.post("/api/config")
def post_config():
    incoming = request.json or {}
    cfg = cs.load_config()
    for k in cs.ACCOUNT_FIELDS:
        if k not in incoming:
            continue
        val = incoming[k]
        if k in ("password", "ui_pass") and val == "********":
            continue
        # let webrtc_candidate / ui_* be explicitly cleared to "" from the UI;
        # other defaulted fields keep their saved value when blank
        if (val is None or val == "") and (cfg.get(k) or "") != "" \
                and k not in ("webrtc_candidate", "ui_user", "ui_pass"):
            continue
        cfg[k] = val
    if not save_config(cfg):
        return jsonify({"ok": False, "error": "config folder not writable — see log"}), 500
    log("account settings saved.")
    restart_go2rtc()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Home Assistant / MQTT settings (UI-configurable)
# --------------------------------------------------------------------------- #
@app.get("/api/mqtt")
def get_mqtt():
    cfg = cs.load_config()
    m = dict(cs.MQTT_DEFAULTS)
    m.update(cfg.get("mqtt") or {})
    m["password"] = _mask_pw(m.get("password"))
    # surface whether env vars are forcing it on, so the UI can explain
    m["env_host"] = bool(os.environ.get("OWLET_MQTT_HOST"))
    return jsonify(m)


@app.post("/api/mqtt")
def post_mqtt():
    incoming = request.json or {}
    cfg = cs.load_config()
    m = dict(cs.MQTT_DEFAULTS)
    m.update(cfg.get("mqtt") or {})
    for k in cs.MQTT_FIELDS:
        if k not in incoming:
            continue
        val = incoming[k]
        if k == "password" and val == "********":
            continue
        m[k] = "1" if (k == "enabled" and val in (True, "1", "on", "true")) else \
            ("" if (k == "enabled") else val)
    cfg["mqtt"] = m
    if not save_config(cfg, regenerate=False):
        return jsonify({"ok": False, "error": "config folder not writable"}), 500
    log("Home Assistant (MQTT) settings saved — applying within a few seconds.")
    return jsonify({"ok": True})


@app.post("/api/mqtt/test")
def test_mqtt():
    incoming = request.json or {}
    cfg = cs.load_config()
    saved = dict(cs.MQTT_DEFAULTS); saved.update(cfg.get("mqtt") or {})
    host = incoming.get("host") or saved.get("host")
    if not host:
        return jsonify({"ok": False, "error": "enter a broker host first"}), 400
    port = int(incoming.get("port") or saved.get("port") or 1883)
    user = incoming.get("user") or saved.get("user") or ""
    pw = saved.get("password") if incoming.get("password") in (None, "********") \
        else incoming.get("password")
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return jsonify({"ok": False, "error": "paho-mqtt not installed in image"}), 500
    try:
        # paho 2.x requires a CallbackAPIVersion; 1.x has no such arg.
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1) \
            if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client()
        if user:
            c.username_pw_set(user, pw or "")
        c.connect(host, port, 5)
        c.disconnect()
        return jsonify({"ok": True, "msg": f"Connected to {host}:{port} ✓"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 502


# --------------------------------------------------------------------------- #
# cameras
# --------------------------------------------------------------------------- #
_STREAMS_CACHE = {"t": 0.0, "data": {}}
_STREAMS_LOCK = threading.Lock()


def _go2rtc_streams() -> dict:
    """Cached ~1s so /api/status + /api/cameras polling (every 5s, plus snapshots)
    don't each make a blocking upstream call and stall the UI during a restart."""
    now = time.time()
    with _STREAMS_LOCK:
        if now - _STREAMS_CACHE["t"] < 1.0:
            return _STREAMS_CACHE["data"]
    try:
        r = requests.get(f"{GO2RTC_API}/api/streams", timeout=2)
        data = r.json() if r.ok else {}
    except Exception:  # noqa: BLE001
        data = {}
    with _STREAMS_LOCK:
        _STREAMS_CACHE["t"] = time.time()
        _STREAMS_CACHE["data"] = data
    return data


def _stream_status(streams: dict, name: str) -> dict:
    info = streams.get(name) or {}
    prods = info.get("producers") or []
    codec, recv = "", 0
    for p in prods:
        try:
            recv += int(p.get("recv") or 0)
        except (TypeError, ValueError):
            pass
        for m in (p.get("medias") or []):
            ml = str(m).lower()
            if "h265" in ml or "265" in ml:
                codec = "H.265"
            elif "h264" in ml or "264" in ml:
                codec = codec or "H.264"
    return {"stream_up": bool(prods), "codec": codec, "recv": recv}


@app.get("/api/cameras")
def list_cameras():
    cfg = cs.load_config()
    streams = _go2rtc_streams()
    cams = []
    for c in cfg.get("cameras") or []:
        st = _stream_status(streams, c["name"])
        cams.append({
            "name": c["name"],
            "camera_dsn": c.get("camera_dsn", ""),
            "uid": c.get("uid", ""),
            "authkey": c.get("authkey", ""),
            "av_password": _mask_pw(c.get("av_password")),
            "av_security_mode": c.get("av_security_mode", ""),
            "have_key": bool(c.get("uid") and c.get("authkey")),
            "busy": c["name"] in STATE["cam_busy"],
            **st,
        })
    return jsonify({
        "cameras": cams,
        "rtsp_port": PUBLIC_RTSP_PORT, "http_port": PUBLIC_HTTP_PORT,
        "webrtc_port": PUBLIC_WEBRTC_PORT,
        # go2rtc requires these on RTSP for non-loopback clients (Frigate). The
        # password itself is never returned; it lives in GO2RTC_PASS or
        # /config/secrets/go2rtc.json.
        "rtsp_user": cs.go2rtc_credentials()[0],
    })


@app.post("/api/cameras")
def add_camera():
    body = request.json or {}
    dsn = (body.get("camera_dsn") or "").strip()
    name = body.get("name") or dsn or cs.DEFAULT_CAM_NAME
    cfg = cs.load_config()
    used = {c["name"] for c in cfg.get("cameras") or []}
    slug = cs.slugify(name)
    # de-dup the slug against existing cameras
    final = slug
    i = 2
    while final in used:
        final = f"{slug}-{i}"
        i += 1
    cam = dict(cs.CAMERA_DEFAULTS, name=final, camera_dsn=dsn)
    cfg.setdefault("cameras", []).append(cam)
    if not save_config(cfg):
        return jsonify({"ok": False, "error": "config folder not writable — see log"}), 500
    log(f"[{final}] camera added (DSN {dsn or '—'}).")
    if dsn:
        _start_camera_diagnose(final)
    return jsonify({"ok": True, "name": final})


@app.post("/api/cameras/<name>")
def update_camera(name):
    body = request.json or {}
    cfg = cs.load_config()
    cam = cs.find_camera(cfg, name)
    if not cam:
        return jsonify({"ok": False, "error": "no such camera"}), 404
    for k in ("camera_dsn", "uid", "authkey", "av_password", "av_security_mode", "name"):
        if k not in body:
            continue
        val = body[k]
        if k == "av_password" and val == "********":
            continue
        if k == "name":
            val = cs.slugify(val, cam["name"])
        cam[k] = val
    if not save_config(cfg):
        return jsonify({"ok": False, "error": "config folder not writable — see log"}), 500
    log(f"[{cam['name']}] settings saved.")
    restart_go2rtc()
    return jsonify({"ok": True, "name": cam["name"]})


@app.delete("/api/cameras/<name>")
def delete_camera(name):
    cfg = cs.load_config()
    before = len(cfg.get("cameras") or [])
    cfg["cameras"] = [c for c in cfg.get("cameras") or [] if c.get("name") != name]
    if len(cfg["cameras"]) == before:
        return jsonify({"ok": False, "error": "no such camera"}), 404
    if not save_config(cfg):
        return jsonify({"ok": False, "error": "config folder not writable — see log"}), 500
    # tidy up the camera's generated env + log (best effort)
    for p in (os.path.join(cs.CAM_DIR, f"{name}.env"),
              os.path.join(cs.CONFIG_DIR, f"tutk-{name}.log")):
        try:
            os.remove(p)
        except OSError:
            pass
    log(f"[{name}] camera removed.")
    restart_go2rtc()
    return jsonify({"ok": True})


_BUSY_LOCK = threading.Lock()


def _start_camera_diagnose(name: str):
    # Atomic test-and-set so two near-simultaneous requests can't both spawn a
    # login worker for the same camera (each would fight for the single session).
    with _BUSY_LOCK:
        if name in STATE["cam_busy"]:
            return
        STATE["cam_busy"].add(name)
    threading.Thread(target=_camera_diagnose_worker, args=(name,), daemon=True).start()


def _camera_diagnose_worker(name: str):
    try:
        from owlet_api import OwletAPI
        cfg = cs.load_config()
        cam = cs.find_camera(cfg, name)
        if not cam:
            return
        if not cfg.get("email") or not cfg.get("password"):
            log(f"[{name}] enter your Owlet email + password (account card) first.")
            return
        dsn = (cam.get("camera_dsn") or "").strip()
        if not dsn:
            log(f"[{name}] set this camera's DSN first.")
            return
        log(f"[{name}] === connecting & fetching camera key ===")
        api = OwletAPI(cfg["region"], cfg["email"], cfg["password"], log=log)
        try:
            creds = api.camera_credentials(dsn)
        except Exception as e:  # noqa: BLE001
            log(f"[{name}] could not fetch camera key: {e}")
            return
        # re-load in case the user edited meanwhile, then write our creds
        cfg = cs.load_config()
        cam = cs.find_camera(cfg, name)
        if not cam:
            return
        cam["uid"] = creds["uid"]
        cam["authkey"] = creds.get("authkey") or ""
        if creds.get("av_password"):
            cam["av_password"] = creds["av_password"]
        if save_config(cfg):
            log(f"[{name}] camera key saved (uid={creds['uid']}); (re)starting stream.")
            restart_go2rtc()
    finally:
        STATE["cam_busy"].discard(name)


@app.post("/api/cameras/<name>/diagnose")
def diagnose_camera(name):
    cfg = cs.load_config()
    if not cs.find_camera(cfg, name):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    _start_camera_diagnose(name)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# account login test (lists Ayla devices — handy for confirming credentials)
# --------------------------------------------------------------------------- #
def _diagnose_worker(cfg: dict):
    STATE["busy"] = True
    try:
        from owlet_api import OwletAPI, OwletError
        if not cfg.get("email") or not cfg.get("password"):
            log("!! enter your Owlet email + password first.")
            return
        api = OwletAPI(cfg["region"], cfg["email"], cfg["password"], log=log)
        try:
            res = api.diagnose()
            STATE["candidates"] = res["candidates"]
            STATE["devices"] = res["devices"]
            log("[login] OK — credentials accepted. Add your camera(s) by DSN below.")
        except OwletError as e:
            log(f"!! login failed: {e}")
        except Exception as e:  # noqa: BLE001
            log(f"!! unexpected error: {e}")
    finally:
        STATE["busy"] = False


@app.post("/api/diagnose")
def diagnose():
    if STATE["busy"]:
        return jsonify({"error": "already running"}), 409
    cfg = cs.load_config()
    incoming = request.json or {}
    for k in ("region", "email", "password"):
        if incoming.get(k) and incoming[k] != "********":
            cfg[k] = incoming[k]
    save_config(cfg)
    LOG.clear()
    log("=== Owlet login test ===")
    threading.Thread(target=_diagnose_worker, args=(cfg,), daemon=True).start()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Owlet sensors / vitals (Smart Sock + Cam room sensors)
# --------------------------------------------------------------------------- #
VITALS_DIR = os.path.join(cs.CONFIG_DIR, "vitals")
VITALS_CACHE = os.path.join(VITALS_DIR, "snapshot.json")


def _vitals_worker(cfg: dict):
    STATE["busy"] = True
    try:
        from owlet_api import OwletAPI, OwletError
        from owlet_vitals import OwletVitals, unit_for
        if not cfg.get("email") or not cfg.get("password"):
            log("!! enter your Owlet email + password first.")
            return
        api = OwletAPI(cfg["region"], cfg["email"], cfg["password"], log=log)
        vit = OwletVitals(api, log=log)
        try:
            snap = vit.snapshot()
        except OwletError as e:
            log(f"!! login failed: {e}")
            return
        except Exception as e:  # noqa: BLE001
            log(f"!! vitals error: {e}")
            return
        log(f"[vitals] {len(snap)} Ayla device(s) on the account")
        for d in snap:
            log(f"\n  ── {d['kind'].upper()}  dsn={d['dsn']}  model={d['model']}  ({d['name']})")
            if d["sensors"]:
                pretty = ", ".join(
                    f"{k}={v}{unit_for(k)}" for k, v in d["sensors"].items() if v is not None)
                log(f"     sensors: {pretty or '(all null — sock may be asleep)'}")
            else:
                log("     sensors: none recognized")
            log(f"     all properties ({len(d['raw_props'])}):")
            for name in d["raw_props"]:
                val = d["raw_props"][name]
                sval = str(val)
                if len(sval) > 120:
                    sval = sval[:117] + "…"
                log(f"        {name} = {sval}")
        try:
            os.makedirs(VITALS_DIR, exist_ok=True)
            with open(VITALS_CACHE, "w") as f:
                import json as _json
                _json.dump({"ts": time.time(), "devices": snap}, f)
        except Exception as e:  # noqa: BLE001
            log(f"[vitals] cache write failed: {e}")
        log("\n[vitals] done — copy the property names above and we'll map them.")
    finally:
        STATE["busy"] = False


@app.post("/api/vitals/discover")
def vitals_discover():
    if STATE["busy"]:
        return jsonify({"error": "already running"}), 409
    cfg = cs.load_config()
    if not cfg.get("email") or not cfg.get("password"):
        return jsonify({"ok": False, "error": "no account configured"}), 400
    LOG.clear()
    log("=== Owlet sensor discovery ===")
    threading.Thread(target=_vitals_worker, args=(cfg,), daemon=True).start()
    return jsonify({"ok": True})


CAM_SENSOR_KEYS = ("temperature", "humidity", "noise", "brightness",
                   "motion", "sound", "wifi_rssi")


def _cam_sensor_devices() -> list[dict]:
    """Live room sensors per camera, written by tutk_client from the TUTK
    stream (temp/noise/motion/sound from frame info; humidity/brightness from
    the GetRealtimeData IOCTL)."""
    import json as _json
    out = []
    cfg = cs.load_config()
    for name in cs.camera_names(cfg):
        try:
            with open(cs.cam_sensors_path(name)) as f:
                data = _json.load(f)
        except (FileNotFoundError, ValueError):
            continue
        sensors = {k: data[k] for k in CAM_SENSOR_KEYS if k in data}
        if not sensors:
            continue
        out.append({"dsn": name, "name": name, "model": "Owlet Cam",
                    "kind": "cam", "sensors": sensors, "ts": data.get("ts")})
    return out


# Temperatures are stored raw (°C); the API serves US/Imperial (°F) so the web
# UI and the native app both get °F from one place. ?units=metric for raw °C.
_TEMP_KEYS = ("temperature", "skin_temperature")


def _convert_units(devices: list[dict], units: str) -> None:
    if units == "metric":
        return
    for d in devices:
        s = d.get("sensors") or {}
        for k in _TEMP_KEYS:
            if s.get(k) is not None:
                try:
                    s[k] = round(s[k] * 9 / 5 + 32)
                except (TypeError, ValueError):
                    pass


@app.get("/api/vitals")
def vitals_latest():
    """Live sock vitals + cam room sensors for the web UI and the native app.

    Shape: {ts, units, devices:[{dsn,name,kind,model,ts,sensors:{...}}]}.
    Temperatures are °F by default (US); pass ?units=metric for °C.
    """
    import json as _json
    units = (request.args.get("units") or "us").lower()
    payload = {"ts": None, "devices": []}
    try:
        with open(VITALS_CACHE) as f:
            payload = _json.load(f)
    except (FileNotFoundError, ValueError):
        pass
    except Exception as e:  # noqa: BLE001
        payload = {"ts": None, "devices": [], "error": str(e)}
    # Merge live cam room-sensors (these update continuously off the stream).
    cams = _cam_sensor_devices()
    if cams:
        existing = {d.get("dsn") for d in payload.get("devices", [])}
        payload.setdefault("devices", [])
        payload["devices"] += [c for c in cams if c["dsn"] not in existing]
    _convert_units(payload.get("devices", []), units)
    payload["units"] = "metric" if units == "metric" else "us"
    return jsonify(payload)


@app.get("/api/logs")
def logs():
    def gen():
        last = 0
        while True:
            cur = list(LOG)
            for ln in cur[last:]:
                yield f"data: {ln}\n\n"
            last = len(cur)
            time.sleep(0.4)
    return Response(gen(), mimetype="text/event-stream")


# --------------------------------------------------------------------------- #
# LAN camera discovery (Kalay UDP 63616)
# --------------------------------------------------------------------------- #
DISCOVERY_PORT = 63616
DISCOVERY_PROBES = [
    bytes.fromhex("f1411388"),
    bytes.fromhex("f1300000"),
    b"\x00\x00\x00\x00",
    b"\x01\x00\x00\x00",
    b"TUTK_SEARCH",
]
UID_TOKEN = re.compile(rb"[A-Z0-9]{6,}-?[A-Z0-9]{4,}-?[A-Z0-9]{0,8}")


def _discover_worker(target: str):
    LOG.clear()
    log("=== LAN camera discovery (UDP 63616) ===")
    log("NOTE: this only works if the container is on the same L2 network as the")
    log("camera — run it with `--network host` for broadcast, or give the camera IP.")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)

    dests = []
    if target:
        dests.append(target.strip())
        log(f"probing {target.strip()} directly …")
    dests.append("255.255.255.255")
    log("broadcasting to 255.255.255.255 …")

    for d in dests:
        for p in DISCOVERY_PROBES:
            try:
                s.sendto(p, (d, DISCOVERY_PORT))
            except OSError as e:
                log(f"  send to {d} failed: {e}")

    found: dict[str, list[str]] = {}
    end = time.time() + 8
    while time.time() < end:
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        toks = [t.decode(errors="ignore") for t in UID_TOKEN.findall(data)
                if 12 <= len(t) <= 24]
        log(f"  ◀ response from {addr[0]}:{addr[1]} ({len(data)} bytes) "
            f"hex={data[:32].hex()}")
        if toks:
            log(f"     UID candidate(s): {toks}")
            found.setdefault(addr[0], [])
            for t in toks:
                if t not in found[addr[0]]:
                    found[addr[0]].append(t)
    s.close()

    cands = []
    for ip, uids in found.items():
        for u in uids:
            cands.append({"field": f"LAN {ip} <uid>", "value": u})
    if cands:
        STATE["candidates"] = cands + STATE.get("candidates", [])
        log(f"\n[done] {len(cands)} UID candidate(s) on the LAN.")
    else:
        log("\n[done] No camera responded on UDP 63616.")


@app.post("/api/discover")
def discover():
    if STATE["busy"]:
        return jsonify({"error": "busy"}), 409
    target = (request.json or {}).get("ip", "") if request.is_json else ""

    def run():
        STATE["busy"] = True
        try:
            _discover_worker(target)
        finally:
            STATE["busy"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/findings")
def findings():
    return jsonify({"candidates": STATE["candidates"], "devices": STATE["devices"]})


# --------------------------------------------------------------------------- #
# status / snapshots / stream control
# --------------------------------------------------------------------------- #
def _have_libs() -> bool:
    try:
        from lib_extract import have_all
        return have_all(TUTK_LIB_DIR)
    except Exception:  # noqa: BLE001
        return os.path.exists(os.path.join(TUTK_LIB_DIR, "libIOTCAPIs.so"))


def _watchdog_state():
    """Live self-heal status written by watchdog.py: per-camera liveness,
    stage-1 (stream) restart counts and the container-restart cooldown.
    None until the watchdog's first probe (or if OWLET_WATCHDOG=0)."""
    try:
        with open(os.path.join(cs.CONFIG_DIR, "vitals", "watchdog.json")) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/status")
def status():
    cfg = cs.load_config()
    cameras = cfg.get("cameras") or []
    streams = _go2rtc_streams()
    with_key = sum(1 for c in cameras if c.get("uid") and c.get("authkey"))
    live = sum(1 for c in cameras if _stream_status(streams, c["name"])["stream_up"])
    return jsonify({
        "have_login": bool(cfg.get("email") and cfg.get("password")),
        "have_libs": _have_libs(),
        "config_writable": os.access(cs.CONFIG_DIR, os.W_OK),
        "busy": STATE["busy"],
        "cameras": len(cameras),
        "cameras_with_key": with_key,
        "streams_live": live,
        "rtsp_port": PUBLIC_RTSP_PORT, "http_port": PUBLIC_HTTP_PORT,
        "webrtc_port": PUBLIC_WEBRTC_PORT,
        "watchdog": _watchdog_state(),
    })


def _snapshot(name: str) -> Response:
    if name.endswith("_overlay"):
        base = name[:-len("_overlay")]
    else:
        base = name
    if not _known_camera(base):
        return Response(status=404)
    try:
        r = requests.get(f"{GO2RTC_API}/api/frame.jpeg", params={"src": name}, timeout=6)
        return Response(r.content, status=r.status_code,
                        mimetype=r.headers.get("Content-Type", "image/jpeg"))
    except Exception:  # noqa: BLE001
        return Response(status=502)


@app.get("/img/<name>.jpg")
def img(name):
    return _snapshot(name)


@app.get("/snapshot/<name>.jpg")
def snapshot(name):
    return _snapshot(name)


@app.get("/api/frame.jpeg")
def frame_proxy():
    """Back-compat snapshot of the primary camera."""
    name = cs.camera_names()[0]
    return _snapshot(name)


@app.post("/api/extract_libs")
def extract_libs_ep():
    from lib_extract import provision
    search = [os.environ.get("OWLET_APK_DIR"), cs.CONFIG_DIR, "/config", "/app/libs", "/apk"]
    ok, msg = provision(TUTK_LIB_DIR, search, log=log)
    log(f"[libs] {msg}")
    return jsonify({"ok": ok, "message": msg})


# Uploaded APKs land ONLY here, under a generated name — never in the /config
# root, where a chosen filename could overwrite owlet.yaml or go2rtc.gen.yaml.
UPLOAD_DIR = os.path.join(cs.CONFIG_DIR, "uploads")
# .apkm is kept because the README's documented source (APKMirror) ships it.
APK_EXTS = (".apk", ".apkm", ".xapk", ".apks")
ZIP_MAGIC = b"PK\x03\x04"


def _apk_ext(filename: str) -> str | None:
    """The upload's lower-case extension if it is an allowed APK container and
    the client-supplied name carries no path component; else None."""
    if not filename or any(c in filename for c in ("/", "\\", "\0")) or ".." in filename:
        return None
    ext = os.path.splitext(filename.strip().lower())[1]
    return ext if ext in APK_EXTS else None


def _save_capped(stream, dest: str, limit: int) -> int:
    """Copy `stream` into a new 0600 file at `dest`, refusing more than `limit`
    bytes (raises ValueError and removes the partial file)."""
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError("too large")
                out.write(chunk)
    except BaseException:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    return total


@app.post("/api/upload_apk")
def upload_apk():
    """Accept an uploaded Owlet .apk/.apkm/.xapk/.apks and extract the TUTK libs.

    Checked by extension AND content (zip magic + a readable zip directory),
    capped at OWLET_UPLOAD_MAX_MB, saved under a generated name in
    /config/uploads only."""
    import secrets as _secrets
    import zipfile
    from lib_extract import provision
    if request.content_length is not None and request.content_length > UPLOAD_MAX + 1024 * 1024:
        return jsonify({"ok": False, "message": "upload too large"}), 413
    f = request.files.get("apk")
    if not f or not f.filename:
        return jsonify({"ok": False, "message": "no file received"}), 400
    ext = _apk_ext(f.filename)
    if not ext:
        return jsonify({"ok": False,
                        "message": "only .apk / .apkm / .xapk / .apks files are accepted"}), 400
    head = f.stream.read(4)
    if head != ZIP_MAGIC:
        return jsonify({"ok": False, "message": "not an APK (no zip header)"}), 400
    try:
        os.makedirs(UPLOAD_DIR, mode=0o700, exist_ok=True)
        dest = os.path.join(UPLOAD_DIR, f"owlet-{int(time.time())}-{_secrets.token_hex(4)}{ext}")
        size = _save_capped(_Prefixed(head, f.stream), dest, UPLOAD_MAX)
    except ValueError:
        return jsonify({"ok": False, "message": "upload too large"}), 413
    except OSError as e:  # noqa: BLE001
        log(f"[upload] cannot save: {e}")
        return jsonify({"ok": False,
                        "message": f"cannot save upload ({e.strerror}) — is /config writable?"}), 500
    if not zipfile.is_zipfile(dest):
        os.remove(dest)
        return jsonify({"ok": False, "message": "not an APK (corrupt zip)"}), 400
    # keep only the newest upload so repeated uploads can't fill appdata
    for old in os.listdir(UPLOAD_DIR):
        p = os.path.join(UPLOAD_DIR, old)
        if p != dest:
            try:
                os.remove(p)
            except OSError:
                pass
    log(f"[upload] received {os.path.basename(dest)} ({size // 1024} KB); extracting libraries…")
    ok, msg = provision(TUTK_LIB_DIR, [UPLOAD_DIR], log=log)
    log(f"[libs] {msg}")
    return jsonify({"ok": ok, "message": msg})


class _Prefixed:
    """A read()-able stream that yields `head` first, then `rest`."""

    def __init__(self, head: bytes, rest):
        self.head, self.rest = head, rest

    def read(self, n: int = -1) -> bytes:
        if self.head:
            out, self.head = self.head, b""
            return out
        return self.rest.read(n)


def _gen_hash():
    try:
        with open(cs.GEN_PATH, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except OSError:
        return None


# Hash of the go2rtc config we last (re)started for. Seeded with the boot config
# so a save that doesn't change the streams (e.g. a UI-password edit) doesn't
# needlessly bounce every camera's single P2P session.
_LAST_GEN = {"h": _gen_hash()}


def restart_go2rtc(force: bool = False):
    h = _gen_hash()
    if not force and h is not None and h == _LAST_GEN["h"]:
        return  # generated config unchanged — leave the live sessions alone
    _LAST_GEN["h"] = h
    try:
        requests.post(f"{GO2RTC_API}/api/restart", timeout=4)
        log("requested go2rtc restart.")
    except Exception as e:  # noqa: BLE001
        log(f"restart error: {e}")


@app.post("/api/stream/restart")
def restart_stream():
    restart_go2rtc(force=True)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# talk-back + sound playback (send audio TO the camera speaker)
# --------------------------------------------------------------------------- #
PLAY_PROCS: dict = {}          # camera -> the current talk/play ffmpeg process
_PLAY_LOCK = threading.Lock()
SOUND_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


def _known_camera(camera: str) -> bool:
    try:
        return camera in cs.camera_names()
    except Exception:  # noqa: BLE001
        return False


def _ffmpeg_to_speaker(camera: str, input_args: list):
    """Transcode an audio source to AAC-LC mono ADTS and stream it (paced to real
    time with -re) into the camera's talk FIFO; tutk_client plays it out the
    speaker. Any previous playback for that camera is interrupted. Returns
    (ok, message, proc). Rate is OWLET_TALK_RATE (default 8000, the cam's mic
    rate) — set to match the camera's probed speaker rate if no audio plays."""
    fifo = cs.talk_fifo_path(camera)
    log(f"[talk] FIFO={fifo} exists={os.path.exists(fifo)}")
    if not cs.stream_ready(camera):
        return False, "camera isn't streaming yet — start its stream first", None
    # Quick check: can we open the FIFO for writing without blocking?
    # If tutk_client's _talk_thread is NOT running (or FIFO has no reader),
    # O_WRONLY|O_NONBLOCK fails with ENXIO. Log this clearly.
    import errno as _errno
    try:
        _test_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(_test_fd)
        log(f"[talk] FIFO has a reader (tutk_client talk thread is alive)")
    except OSError as _e:
        if _e.errno == _errno.ENXIO:
            log(f"[talk] FIFO has NO reader — tutk_client talk thread may not be running "
                f"(check the tutk log for '[talk] FIFO open' or avSendAudioData errors)")
        else:
            log(f"[talk] FIFO open check: {_e}")
    rate = os.environ.get("OWLET_TALK_RATE", "8000")
    # -y is CRITICAL: the output path is a pre-existing FIFO. Without -y ffmpeg
    # sees an "existing file", prompts "Overwrite? [y/N]", gets no stdin, and
    # exits writing ZERO bytes — so no audio ever reaches the camera speaker.
    cmd = (["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"] + input_args
           + ["-ac", "1", "-ar", str(rate), "-c:a", "aac", "-b:a", "24k", "-f", "adts", fifo])
    log(f"[talk] cmd: {' '.join(cmd)}")
    with _PLAY_LOCK:
        old = PLAY_PROCS.get(camera)
        if old and old.poll() is None:
            try:
                old.terminate()
                old.wait(timeout=2)           # one writer at a time on the FIFO
            except Exception:  # noqa: BLE001
                try:
                    old.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as e:  # noqa: BLE001
            return False, f"ffmpeg failed: {e}", None
        PLAY_PROCS[camera] = proc

    # Stream ffmpeg's stderr into the webapp log so errors are visible.
    def _drain_ffmpeg_stderr():
        try:
            for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    log(f"[ffmpeg/talk] {line}")
        except Exception:  # noqa: BLE001
            pass
        rc = proc.wait()
        log(f"[talk] ffmpeg exited rc={rc}")
    threading.Thread(target=_drain_ffmpeg_stderr, daemon=True).start()

    return True, "playing", proc


@app.get("/api/sounds")
def list_sounds():
    try:
        files = sorted(f for f in os.listdir(cs.SOUNDS_DIR)
                       if f.lower().endswith(SOUND_EXTS))
    except OSError:
        files = []
    return jsonify({"sounds": files})


@app.post("/api/sounds")
def upload_sound():
    from werkzeug.utils import secure_filename
    if (request.content_length or 0) > SOUND_MAX:
        return jsonify({"ok": False, "error": "file too large"}), 413
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "no file received"}), 400
    name = secure_filename(f.filename)
    if not name.lower().endswith(SOUND_EXTS):
        return jsonify({"ok": False, "error": "audio files only (mp3/wav/m4a/aac/ogg)"}), 400
    try:
        os.makedirs(cs.SOUNDS_DIR, exist_ok=True)
        f.save(os.path.join(cs.SOUNDS_DIR, name))
    except OSError as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"can't save ({e.strerror}) — is /config writable?"}), 500
    log(f"[sounds] uploaded {name}")
    return jsonify({"ok": True, "name": name})


@app.delete("/api/sounds/<path:fname>")
def delete_sound(fname):
    from werkzeug.utils import secure_filename
    try:
        os.remove(os.path.join(cs.SOUNDS_DIR, secure_filename(fname)))
    except OSError:
        pass
    return jsonify({"ok": True})


def _schedule_stop(camera, proc, seconds: float):
    """Stop THIS playback process after `seconds` (the streamed-audio sleep timer).
    The camera-side timer_ms only applies to the app's native player, so for
    streamed mp3/lullaby we enforce the timer here by terminating this exact proc."""
    def _stop():
        try:
            if proc.poll() is None:
                proc.terminate()
                log(f"[play] {camera}: sleep timer elapsed — stopped playback")
        except Exception:  # noqa: BLE001
            pass
    t = threading.Timer(seconds, _stop)
    t.daemon = True
    t.start()


@app.post("/api/play/<camera>")
def play_sound(camera):
    from werkzeug.utils import secure_filename
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    body = request.json or {}
    fname = secure_filename(body.get("file", ""))
    path = os.path.join(cs.SOUNDS_DIR, fname)
    if not fname or not os.path.exists(path):
        return jsonify({"ok": False, "error": "sound not found"}), 404
    # loop=True repeats the track (lullaby/white-noise); timer_min auto-stops it.
    input_args = ["-re"]
    if body.get("loop"):
        input_args = ["-stream_loop", "-1", "-re"]
    input_args += ["-i", path]
    ok, msg, proc = _ffmpeg_to_speaker(camera, input_args)
    try:
        timer_min = float(body.get("timer_min") or 0)
    except (TypeError, ValueError):
        timer_min = 0
    if ok and proc and timer_min > 0:
        _schedule_stop(camera, proc, timer_min * 60)
    extra = (" [loop]" if body.get("loop") else "") + \
            (f" [{timer_min:g}min timer]" if timer_min > 0 else "")
    log(f"[play] {camera}: {fname} -> {msg}{extra}")
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)


@app.post("/api/talk/<camera>")
def talk(camera):
    """Play a recorded mic clip out the camera speaker (hold-to-talk)."""
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    if (request.content_length or 0) > SOUND_MAX:
        return jsonify({"ok": False, "error": "clip too large"}), 413
    f = request.files.get("audio")
    if not f:
        return jsonify({"ok": False, "error": "no audio received"}), 400
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".dat")
    f.save(tmp.name)
    tmp.close()
    ok, msg, proc = _ffmpeg_to_speaker(camera, ["-re", "-i", tmp.name])

    # clean the temp once THIS clip's ffmpeg is done with it (bind to the exact
    # proc — a later talk/play must not delete this clip out from under it).
    def _cleanup():
        if proc:
            try:
                proc.wait()
            except Exception:  # noqa: BLE001
                pass
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    threading.Thread(target=_cleanup, daemon=True).start()
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)


@app.post("/api/talk/<camera>/stop")
def talk_stop(camera):
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    with _PLAY_LOCK:
        p = PLAY_PROCS.get(camera)
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
    return jsonify({"ok": True})


@app.get("/api/talk/<camera>/fifo_status")
def talk_fifo_status(camera):
    """Diagnostic: report whether tutk_client's talk FIFO is open for reading."""
    import errno as _errno
    fifo = cs.talk_fifo_path(camera)
    exists = os.path.exists(fifo)
    if not exists:
        return jsonify({"fifo": fifo, "exists": False,
                        "reader": False, "error": "FIFO not found (camera not streaming?)"}), 404
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        return jsonify({"fifo": fifo, "exists": True, "reader": True,
                        "message": "FIFO has a reader — tutk_client talk thread is alive"})
    except OSError as e:
        if e.errno == _errno.ENXIO:
            return jsonify({"fifo": fifo, "exists": True, "reader": False,
                            "error": "FIFO has NO reader — tutk_client talk thread not running "
                                     "(check log for avSendAudioData or FIFO open errors)"}), 500
        return jsonify({"fifo": fifo, "exists": True, "reader": None,
                        "error": str(e)}), 500


# --------------------------------------------------------------------------- #
# native audio player (camera built-in lullabies / soothing sounds)
# --------------------------------------------------------------------------- #
_LULLABY_SEQ = [0]
_LULLABY_LOCK = threading.Lock()


def _lullaby_rpc(camera: str, req: dict, want_resp: bool, timeout: float = 4.0):
    """Drop a JSON command in the camera's audiocmd file; if want_resp, wait for
    tutk_client to write the matching reply to the audioresp file."""
    import json as _json
    cmd_path = cs.audiocmd_file_path(camera)
    resp_path = cs.audioresp_file_path(camera)
    # Gate on the session, not on cmd_path: the exec deletes cmd_path at every
    # start and it only comes into being when we write the first request below.
    if not cs.stream_ready(camera):
        return False, {"error": "camera isn't streaming yet — start its stream first"}
    with _LULLABY_LOCK:
        _LULLABY_SEQ[0] += 1
        rid = _LULLABY_SEQ[0]
    req = dict(req, id=rid)
    try:
        with open(cmd_path, "w") as f:
            _json.dump(req, f)
    except OSError as e:  # noqa: BLE001
        return False, {"error": f"cannot write command: {e}"}
    if not want_resp:
        return True, {"ok": True}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(resp_path) as f:
                resp = _json.load(f)
            if resp.get("id") == rid:
                return True, resp
        except (OSError, ValueError):
            pass
        time.sleep(0.15)
    return False, {"error": "no response from camera (timeout)"}


@app.get("/api/lullaby/<camera>/tracks")
def lullaby_tracks(camera):
    """List the camera's built-in audio tracks (UUID + duration). The camera
    self-reports these — no cloud needed."""
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    # The camera's audio_player_sources reply is intermittent — the JSON can
    # arrive in fragments and the first query after connect often comes back with
    # an empty items list. Retry a few times until the camera returns its tracks
    # so the app's sound machine isn't randomly empty.
    ok, resp, items = False, {}, []
    for _ in range(5):
        ok, resp = _lullaby_rpc(camera, {"action": "sources"}, want_resp=True)
        if ok:
            items = resp.get("items") or []
            if items:
                break
        time.sleep(0.4)
    if not ok:
        return jsonify({"ok": False, **resp}), 502
    return jsonify({"ok": True, "items": items, "raw": resp})


@app.get("/api/lullaby/<camera>/state")
def lullaby_state(camera):
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    ok, resp = _lullaby_rpc(camera, {"action": "state"}, want_resp=True)
    if not ok:
        return jsonify({"ok": False, **resp}), 502
    return jsonify({"ok": True, "state": resp})


@app.post("/api/lullaby/<camera>/play")
def lullaby_play(camera):
    """Play built-in tracks by UUID, with optional repeat + camera-side sleep
    timer (timeout_ms)."""
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    body = request.json or {}
    uuids = body.get("uuids") or ([body["uuid"]] if body.get("uuid") else [])
    req = {"action": "play", "uuids": uuids}
    if body.get("repeat") is not None:
        req["repeat"] = bool(body["repeat"])
    if body.get("timeout_ms") is not None:
        try:
            req["timeout_ms"] = int(body["timeout_ms"])
        except (TypeError, ValueError):
            pass
    ok, resp = _lullaby_rpc(camera, req, want_resp=False)
    log(f"[lullaby] {camera}: play {len(uuids)} track(s) -> {ok}")
    return jsonify({"ok": ok, **resp}), (200 if ok else 502)


@app.post("/api/lullaby/<camera>/<action>")
def lullaby_transport(camera, action):
    """stop / pause / next / prev / reset for the native player."""
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    if action not in ("stop", "pause", "next", "prev", "reset"):
        return jsonify({"ok": False, "error": "bad action"}), 400
    ok, resp = _lullaby_rpc(camera, {"action": action}, want_resp=False)
    log(f"[lullaby] {camera}: {action} -> {ok}")
    return jsonify({"ok": ok, **resp}), (200 if ok else 502)


@app.post("/api/led/<camera>")
def set_led(camera):
    """Turn the camera's status indicator light on/off (the app's SET_LED_STATUS)."""
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    on = bool((request.json or {}).get("on"))
    ok, resp = _lullaby_rpc(camera, {"action": "led", "on": on}, want_resp=False)
    log(f"[led] {camera}: {'on' if on else 'off'} -> {ok}")
    return jsonify({"ok": ok, **resp}), (200 if ok else 502)


@app.get("/api/info/<camera>")
def camera_info(camera):
    """Camera model / vendor / firmware (the app's DEVINFO)."""
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    ok, resp = _lullaby_rpc(camera, {"action": "devinfo"}, want_resp=True)
    if not ok:
        return jsonify({"ok": False, **resp}), 502
    return jsonify({"ok": True, **resp})


@app.get("/api/volume/<camera>")
def get_volume(camera):
    """Current speaker volume (0-100). Prefers the camera's actual value (live
    GET_SPK_MIC_VOL), then the live control file, then the saved setting."""
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    # best-effort live readback from the camera
    if os.path.exists(cs.audiocmd_file_path(camera)):
        ok, resp = _lullaby_rpc(camera, {"action": "getvol"}, want_resp=True, timeout=3.5)
        if ok and resp.get("percent") is not None:
            return jsonify({"ok": True, "percent": resp["percent"], "source": "camera"})
    pct = None
    try:
        with open(cs.vol_file_path(camera)) as f:
            pct = int(f.read().strip())
    except (OSError, ValueError):
        cam = cs.find_camera(cs.load_config(), camera) or {}
        try:
            pct = int(str(cam.get("spk_vol")).strip())
        except (TypeError, ValueError):
            pct = None
    return jsonify({"ok": True, "percent": pct, "source": "saved"})


@app.post("/api/volume/<camera>")
def set_volume(camera):
    """Set speaker volume 0-100. Writes the live control file (tutk_client pushes
    SET_SPK_VOL to the camera within ~1s) and persists it as the camera's default."""
    if not _known_camera(camera):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    try:
        pct = int((request.json or {}).get("percent"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "percent must be an integer 0-100"}), 400
    pct = max(0, min(100, pct))
    # live: write the control file the volume thread watches
    try:
        with open(cs.vol_file_path(camera), "w") as f:
            f.write(str(pct))
    except OSError as e:  # noqa: BLE001
        log(f"[vol] cannot write control file: {e}")
    # persist as the camera's default (applied on next (re)connect too)
    cfg = cs.load_config()
    cam = cs.find_camera(cfg, camera)
    if cam is not None:
        cam["spk_vol"] = str(pct)
        save_config(cfg, regenerate=True)
    log(f"[vol] {camera}: speaker volume set to {pct}%")
    return jsonify({"ok": True, "percent": pct})


@app.get("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    # Seed defaults + generate an initial go2rtc config only when nothing exists
    # yet — never overwrite an existing config (it may hold camera creds).
    if not os.path.exists(cs.CONFIG_PATH):
        if not save_config(cs.load_config()):
            log("starting the UI anyway so you can fix the config-folder permission.")
    # Serve on HTTPS so browsers allow microphone access (getUserMedia requires
    # a secure context — HTTPS or localhost — on modern browsers).
    # The cert is generated by start-bionic.sh; if it's missing we try the
    # werkzeug 'adhoc' mode (needs cryptography pkg) then fall back to HTTP.
    cert = "/config/ssl/cert.pem"
    key  = "/config/ssl/key.pem"
    if os.path.isfile(cert) and os.path.isfile(key):
        ssl_ctx = (cert, key)
        log("TLS cert found — control panel starting on HTTPS")
    else:
        try:
            # werkzeug 'adhoc' works when the `cryptography` package is installed.
            # Test it eagerly so we don't get a late import error at run time.
            from cryptography import x509 as _x509  # noqa: F401
            ssl_ctx = "adhoc"
            log("TLS: cryptography pkg available — using adhoc cert (HTTPS)")
        except ImportError:
            ssl_ctx = None
            log("WARNING: no TLS cert (/config/ssl/cert.pem) and `cryptography` "
                "pip package not installed — mic blocked (needs https or localhost). "
                "Use Chrome flag chrome://flags/#unsafely-treat-insecure-origin-as-secure "
                "to whitelist http://[your-ip]:8088 as a workaround.")
    app.run(host="0.0.0.0", port=int(os.environ.get("UI_PORT", "8088")),
            threaded=True, ssl_context=ssl_ctx)
