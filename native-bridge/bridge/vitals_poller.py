#!/usr/bin/env python3
"""
vitals_poller.py — continuously read Owlet Smart Sock vitals + Owlet Cam room
sensors and fan them out to everything that wants them:

  * /config/vitals/snapshot.json  -> the web UI and the REST API (/api/vitals),
    which is also what your native app polls. Live, no manual "probe" needed.
  * Home Assistant via MQTT discovery (auto-creates entities) — enable by
    setting OWLET_MQTT_HOST (+ optional _PORT/_USER/_PASS/_PREFIX).
  * /config/vitals/overlay-<cam>.txt -> the burned-in video HUD (read by ffmpeg
    drawtext). Written here so it's ready the moment the overlay is switched on.

Two independent sources:

  * Camera room sensors (temperature, humidity, noise, brightness, Wi-Fi
    RSSI) come from the camera's own P2P session: tutk_client reads them from
    the video frame-info and the GET_REALTIME_DATA IOCTL and writes
    /config/vitals/cam-<name>.json. Publishing those needs NO cloud and is ON
    by default (OWLET_SENSOR_PUBLISH=0 turns this process off).
  * Smart Sock vitals come from Owlet's Ayla cloud and require WRITING
    APP_ACTIVE=1 to the sock every cycle. That poller is OFF by default; leave
    it off unless you accept that (OWLET_VITALS_POLL=1 enables it).
"""

from __future__ import annotations

import json
import os
import threading
import time

import config_store as cs

POLL = int(os.environ.get("OWLET_VITALS_INTERVAL") or "15")
SNAP = os.path.join(cs.VITALS_DIR, "snapshot.json")

STATE_PREFIX = "owlet"

# US/Imperial: report temperatures in °F.
TEMP_KEYS = ("temperature", "skin_temperature")


def _f(c):
    try:
        return round(c * 9 / 5 + 32)
    except (TypeError, ValueError):
        return c


def _to_us(sensors: dict) -> dict:
    out = dict(sensors)
    for k in TEMP_KEYS:
        if out.get(k) is not None:
            out[k] = _f(out[k])
    return out


# ---- Home Assistant entity metadata -----------------------------------------
# key -> (friendly, unit, device_class, is_binary)
HA_META = {
    "heart_rate":        ("Heart rate", "bpm", None, False),
    "oxygen":            ("Oxygen", "%", None, False),
    "oxygen_avg":        ("Oxygen (10-min avg)", "%", None, False),
    "skin_temperature":  ("Skin temperature", "°F", "temperature", False),
    "battery":           ("Sock battery", "%", "battery", False),
    "battery_minutes":   ("Battery remaining", "min", "duration", False),
    "signal_strength":   ("Sock signal", "dBm", "signal_strength", False),
    "sleep_state":       ("Sleep state", None, None, False),
    "movement":          ("Movement", None, None, False),
    "temperature":       ("Room temperature", "°F", "temperature", False),
    "humidity":          ("Room humidity", "%", "humidity", False),
    "noise":             ("Room noise", "dB", "sound_pressure", False),
    "brightness":        ("Room brightness", "lx", "illuminance", False),
    "wifi_rssi":         ("Cam WiFi", "dBm", "signal_strength", False),
    "base_station_on":   ("Base station", None, "running", True),
    "charging":          ("Sock charging", None, "battery_charging", True),
    "motion":            ("Motion", None, "motion", True),
    "sound":             ("Sound", None, "sound", True),
}


class _Mqtt:
    """Wraps a paho MQTT client; (re)connects when the UI/env settings change.

    Every successful (re)connect -- including paho's silent automatic
    reconnects after a network blip -- republishes the retained bridge
    "online" status, every discovery payload sent so far and each device's
    availability, and forces the next state publish. Without that the broker's
    retained last-will "offline" stuck after a reconnect and every Home
    Assistant entity stayed unavailable until the container restarted."""

    def __init__(self, log):
        self.log = log
        self.c = None
        self._announced: set[str] = set()
        self._discovery: dict[str, str] = {}   # config topic -> payload (for replay)
        self._avail: dict[str, str] = {}       # availability topic -> last value
        self._last_state: dict[str, str] = {}  # state topic -> last payload sent
        self._lock = threading.Lock()
        self._key = None          # settings fingerprint we're connected with
        self.prefix = "homeassistant"

    def ensure(self, s: dict):
        """Connect/disconnect/reconnect to match settings dict `s`. Retries each
        poll while disconnected — `self._key` is only committed AFTER a successful
        connect, so a broker that's down at boot gets retried (instead of being
        stuck 'connected' to nothing forever)."""
        key = (s.get("enabled"), s.get("host"), s.get("port"),
               s.get("user"), s.get("password"), s.get("prefix"))
        # already connected with these exact settings -> nothing to do
        if key == self._key and self.c is not None:
            return
        # settings changed -> tear the old connection down
        if key != self._key:
            self._disconnect()
            with self._lock:
                self._announced.clear()
                self._discovery.clear()
                self._avail.clear()
                self._last_state.clear()
            self._key = key
        if not s.get("enabled") or not s.get("host"):
            return
        if self.c is not None:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.log("[vitals] MQTT enabled but paho-mqtt isn't installed")
            return
        try:
            self.prefix = s.get("prefix") or "homeassistant"
            # paho 2.x requires a CallbackAPIVersion; 1.x has no such arg.
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1) \
                if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client()
            if s.get("user"):
                c.username_pw_set(s["user"], s.get("password") or "")
            c.will_set(f"{STATE_PREFIX}/bridge/status", "offline", retain=True)
            c.on_connect = self._on_connect
            c.connect(s["host"], int(s.get("port") or 1883), 60)
            c.loop_start()
            self.c = c
            self.log(f"[vitals] MQTT connecting to {s['host']}:{s.get('port')}")
        except Exception as e:  # noqa: BLE001
            self.log(f"[vitals] MQTT connect failed ({e}); will retry")

    def _on_connect(self, client, _userdata, _flags, rc, *_a):
        """paho callback for EVERY CONNACK (first connect and auto-reconnects)."""
        try:
            rc = int(rc)
        except (TypeError, ValueError):
            rc = getattr(rc, "value", rc)
        if rc != 0:
            self.log(f"[vitals] MQTT broker refused the connection (rc={rc})")
            return
        client.publish(f"{STATE_PREFIX}/bridge/status", "online", retain=True)
        with self._lock:
            replay = list(self._discovery.items()) + list(self._avail.items())
            self._last_state.clear()     # next loop re-sends every state payload
        for topic, payload in replay:
            client.publish(topic, payload, retain=True)
        self.log(f"[vitals] MQTT connected; republished online + {len(replay)} "
                 "discovery/availability message(s)")

    def _disconnect(self):
        if self.c:
            try:
                self.c.loop_stop()
                self.c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.c = None

    @staticmethod
    def _slug(s: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in str(s)).strip("_").lower()

    def _pub(self, topic: str, payload: str, retain: bool = True) -> None:
        self.c.publish(topic, payload, retain=retain)

    def publish_device(self, dev: dict):
        """Discovery + availability + state for one device.

        Only FRESH readings are published (the caller drops stale fields), a
        key HA already knows but that is no longer fresh is sent as null (HA
        shows "unknown", not the old value), the payload carries the
        measurement time, and an unchanged payload is not re-sent. A device
        with no fresh reading at all is marked unavailable."""
        if not self.c:
            return
        dsn = dev.get("dsn") or dev.get("name") or "owlet"
        node = self._slug(dsn)
        kind = dev.get("kind", "device")
        sensors = _to_us(dev.get("sensors") or {})
        state_topic = f"{STATE_PREFIX}/{node}/state"
        avail_topic = f"{STATE_PREFIX}/{node}/availability"
        fresh = {k: v for k, v in sensors.items() if k in HA_META and v is not None}
        avail = "online" if fresh else "offline"
        with self._lock:
            changed = self._avail.get(avail_topic) != avail
            self._avail[avail_topic] = avail
        if changed:
            self._pub(avail_topic, avail)
        if not fresh:
            return
        dev_info = {
            "identifiers": [f"owlet_{node}"],
            "name": f"Owlet {kind.title()} {dev.get('name', dsn)}",
            "manufacturer": "Owlet",
            "model": dev.get("model") or kind,
        }
        for key in fresh:
            friendly, unit, dclass, is_bin = HA_META[key]
            comp = "binary_sensor" if is_bin else "sensor"
            uid = f"owlet_{node}_{key}"
            if uid in self._announced:
                continue
            cfg = {
                "name": friendly,
                "state_topic": state_topic,
                "value_template": "{{ value_json.%s }}" % key,
                "unique_id": uid,
                "object_id": uid,
                "device": dev_info,
                # unavailable if the bridge is gone OR this device's readings are stale
                "availability": [{"topic": f"{STATE_PREFIX}/bridge/status"},
                                 {"topic": avail_topic}],
                "availability_mode": "all",
            }
            if unit:
                cfg["unit_of_measurement"] = unit
            if dclass:
                cfg["device_class"] = dclass
            if is_bin:
                cfg["payload_on"] = 1
                cfg["payload_off"] = 0
            topic = f"{self.prefix}/{comp}/{uid}/config"
            payload = json.dumps(cfg)
            with self._lock:
                self._discovery[topic] = payload
                self._announced.add(uid)
            self._pub(topic, payload)
        # keys announced for this node earlier but not fresh now -> explicit null
        state = {k: None for k in HA_META
                 if f"owlet_{node}_{k}" in self._announced and k not in fresh}
        state.update(fresh)
        ts = dev.get("ts")
        if isinstance(ts, (int, float)):
            state["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        payload = json.dumps(state, sort_keys=True)
        with self._lock:
            if self._last_state.get(state_topic) == payload:
                return               # same reading, same measurement -> not "new"
            self._last_state[state_topic] = payload
        self._pub(state_topic, payload)


def _write_overlays(devices: list[dict], log):
    """Compose the burned-in HUD text per camera (cam env + a paired sock)."""
    cams = [d for d in devices if d.get("kind") == "cam"]
    socks = [d for d in devices if d.get("kind") == "sock"]
    sock = socks[0] if len(socks) == 1 else None  # only auto-pair a lone sock
    for cam in cams:
        s = _to_us(cam.get("sensors") or {})
        parts = []
        if s.get("temperature") is not None:
            parts.append(f"{s['temperature']}°F")
        if s.get("humidity") is not None:
            parts.append(f"{s['humidity']}% RH")
        if sock:
            sv = sock.get("sensors") or {}
            if sv.get("heart_rate") is not None:
                parts.append(f"HR {sv['heart_rate']}")
            if sv.get("oxygen") is not None:
                parts.append(f"O2 {sv['oxygen']}%")
        # No readings -> blank the HUD, don't skip. Skipping left whatever was
        # written last time burned into the picture indefinitely (a cam whose
        # sensors went away kept showing its final values). drawtext wants a
        # non-empty file, so a single space is the "nothing" value -- the same
        # seed the overlay exec writes before ffmpeg starts.
        line = "   ".join(parts) if parts else " "
        try:
            path = os.path.join(cs.VITALS_DIR, "overlay-%s.txt" % cam["name"])
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(line)
            os.replace(tmp, path)
        except OSError:
            pass


# A camera reading older than this is not published as current (the stream
# died, or the camera stopped reporting that field).
STALE_AFTER = int(os.environ.get("OWLET_SENSOR_STALE") or "60")


def _read_cam_sidecars(now: float | None = None) -> list[dict]:
    """Camera room sensors from tutk_client's sidecars, FRESH fields only.

    Uses each field's own measurement time ("field_ts"; older sidecars without
    it fall back to the file-level "ts"). A camera whose sidecar has no fresh
    field is still returned, with empty sensors, so MQTT can mark it
    unavailable and the HUD is blanked instead of showing old numbers."""
    now = time.time() if now is None else now
    out = []
    for name in cs.camera_names():
        try:
            with open(cs.cam_sensors_path(name)) as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        fts = data.get("field_ts") if isinstance(data.get("field_ts"), dict) else {}
        file_ts = data.get("ts")
        sensors, newest = {}, None
        for k, v in data.items():
            if k in ("ts", "field_ts") or v is None:
                continue
            t = fts.get(k, file_ts)
            if not isinstance(t, (int, float)) or now - t > STALE_AFTER:
                continue
            sensors[k] = v
            newest = t if newest is None else max(newest, t)
        out.append({"dsn": name, "name": name, "model": "Owlet Cam",
                    "kind": "cam", "sensors": sensors, "ts": newest,
                    "stale": not sensors})
    return out


def cloud_enabled() -> bool:
    """The Ayla sock poller. OFF unless OWLET_VITALS_POLL=1: every cycle it
    WRITES APP_ACTIVE=1 to each sock through Owlet's cloud and logs in to Ayla.
    Leave it off unless you want sock vitals here and accept that."""
    return os.environ.get("OWLET_VITALS_POLL", "0") == "1"


def publisher_enabled() -> bool:
    """The local publisher (camera room sensors -> snapshot/HUD/MQTT). It talks
    to no cloud; OWLET_SENSOR_PUBLISH=0 turns it off."""
    return os.environ.get("OWLET_SENSOR_PUBLISH", "1") != "0"


class _Cloud:
    """Sock vitals from the Ayla cloud, polled on its own schedule so a cloud
    outage (and its backoff) never delays the local camera publishing."""

    def __init__(self, log):
        self.log = log
        self.api = None
        self.key = None
        self.fails = 0
        self.next_at = 0.0

    def poll(self, cfg: dict, now: float) -> list[dict]:
        if not cloud_enabled() or not (cfg.get("email") and cfg.get("password")):
            self.api = None
            return []
        if now < self.next_at:
            return []
        from owlet_api import OwletAPI, OwletError
        from owlet_vitals import OwletVitals
        key = (cfg.get("region"), cfg.get("email"), cfg.get("password"))
        if key != self.key:          # account changed in the UI -> new session
            self.api, self.key = None, key
        try:
            if self.api is None:
                self.api = OwletAPI(cfg["region"], cfg["email"], cfg["password"])
            devices = OwletVitals(self.api).snapshot()
            self.fails = 0
        except OwletError as e:
            # auth/login failure -> drop the session and re-auth next time
            self.log(f"[vitals] login error: {e}")
            self.api = None
            self.fails += 1
            devices = []
        except Exception as e:  # noqa: BLE001
            # A 401/403 means the Ayla access_token expired -> drop the session
            # and re-auth (raise_for_status() throws requests.HTTPError, not an
            # OwletError). Anything else keeps the warm session and backs off.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403) or " 401 " in f" {e} " or " 403 " in f" {e} ":
                self.log(f"[vitals] auth expired ({status or 'HTTP'}) -> re-authenticating")
                self.api = None
            else:
                self.log(f"[vitals] poll error: {e}")
            self.fails += 1
            devices = []
        # exponential backoff on sustained cloud failure (cap ~5 min)
        self.next_at = now + (POLL if self.fails == 0
                              else min(POLL * (2 ** min(self.fails, 5)), 300))
        return devices


def poll_once(mq: "_Mqtt", cloud: _Cloud, log, now: float | None = None) -> list[dict]:
    """One cycle: (optional) sock cloud poll + local camera sidecars ->
    snapshot.json, HUD overlay text and MQTT. Returns the devices handled."""
    now = time.time() if now is None else now
    cfg = cs.load_config()
    mq.ensure(cs.mqtt_settings(cfg))   # pick up UI changes live
    devices = cloud.poll(cfg, now)
    # merge live cam sensors from tutk_client sidecars (cloud-independent)
    cam_devs = _read_cam_sidecars(now=now)
    seen = {d.get("dsn") for d in devices}
    devices += [c for c in cam_devs if c["dsn"] not in seen]

    # Always refresh the snapshot (even when empty) so /api/vitals can tell
    # "no data yet" from "stale": it carries ts + an ok flag every loop.
    try:
        os.makedirs(cs.VITALS_DIR, mode=0o700, exist_ok=True)
        cs._atomic_write(SNAP, json.dumps({"ts": now, "ok": bool(devices),  # noqa: SLF001
                                           "devices": devices}))
    except OSError as e:
        log(f"[vitals] snapshot write failed: {e}")
    if devices:
        _write_overlays(devices, log)
        for d in devices:
            mq.publish_device(d)
    return devices


def main() -> None:
    def log(m):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), m, flush=True)

    if not publisher_enabled():
        return
    log("[vitals] camera room sensors -> snapshot/HUD/MQTT (local only); sock cloud "
        "poller " + ("ON (writes APP_ACTIVE to the sock)" if cloud_enabled()
                     else "off (OWLET_VITALS_POLL=0)"))
    time.sleep(12)  # let webapp + first stream come up
    mq = _Mqtt(log)
    cloud = _Cloud(log)
    while True:
        poll_once(mq, cloud, log)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
