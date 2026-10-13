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

import collections
import json
import os
import threading
import time

import config_store as cs

POLL = int(os.environ.get("OWLET_VITALS_INTERVAL") or "15")
SNAP = os.path.join(cs.VITALS_DIR, "snapshot.json")
# Every MQTT discovery topic the bridge has published (see docs/mqtt.md).
DISCOVERY_RECORD = os.path.join(cs.VITALS_DIR, "discovery.json")

STATE_PREFIX = "owlet"
# How long a retired discovery entry whose retirement the broker never
# acknowledged stays recorded (and keeps being re-sent) before it is pruned
# from the record with a warning.
RETIRE_KEEP = 7 * 24 * 3600
# While polling, an unacknowledged retirement is re-sent at most this often
# (every broker connect re-sends it as well).
RETIRE_RESEND = 10 * 60


def _retired_at(entry: dict):
    """The entry's retirement time, or None when it is not marked retired."""
    v = entry.get("retired_at")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

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

    def __init__(self, log, record_path: str | None = None):
        self.log = log
        self.c = None
        self._announced: set[str] = set()
        self._discovery: dict[str, str] = {}   # config topic -> payload (for replay)
        self._avail: dict[str, str] = {}       # availability topic -> last value
        self._last_state: dict[str, str] = {}  # state topic -> last payload sent
        self._lock = threading.Lock()
        self._key = None          # settings fingerprint we're connected with
        self.prefix = "homeassistant"
        # Every discovery topic ever published, persisted across restarts, so
        # entities of a source that is no longer running can be retired.
        self.record_path = record_path or DISCOVERY_RECORD
        self._record: dict[str, dict] = self._load_record()
        self._active: set[str] | None = None   # sources publishing this run
        self._connected = False
        # Delivery evidence for retirement messages. _on_publish (paho's
        # network thread) only appends the acknowledged mid to _acks; the
        # rest is applied under self._lock by _apply_acks_locked.
        self._acks: collections.deque = collections.deque()
        self._mid_topic: dict[int, str] = {}   # retirement mid -> its topic
        self._acked_topics: set[str] = set()   # retirement topics with a PUBACK
        self._sent_at: dict[str, float] = {}   # config topic -> last retirement send

    # -- discovery record + retirement -------------------------------------- #
    def _load_record(self) -> dict:
        try:
            with open(self.record_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        topics = data.get("topics") if isinstance(data, dict) else None
        if not isinstance(topics, dict):
            return {}
        return {t: e for t, e in topics.items()
                if isinstance(t, str) and isinstance(e, dict) and e.get("source")}

    def _save_record(self) -> None:
        """Atomic rewrite of the record (caller holds the lock). Entries whose
        retirement the broker acknowledged ("confirmed_at") are dropped first.
        Best effort: a read-only /config only loses the ability to retire
        entities later."""
        for t in [t for t, e in self._record.items() if e.get("confirmed_at") is not None]:
            del self._record[t]
            self._sent_at.pop(t, None)
        try:
            os.makedirs(os.path.dirname(self.record_path) or ".", mode=0o700, exist_ok=True)
            cs._atomic_write(self.record_path,  # noqa: SLF001
                             json.dumps({"version": 1, "topics": self._record},
                                        indent=1, sort_keys=True))
        except OSError as e:
            self.log(f"[vitals] discovery record write failed: {e}")

    def set_active_sources(self, sources, now: float | None = None) -> None:
        """Tell the publisher which sources run this cycle ("cam", "cloud").
        Called every poll cycle: whenever an entity recorded for a source that
        is no longer active is still waiting to be retired, retirement is
        retried here -- not only when the set changes, so a pass that could not
        run (no connection yet, a failed publish) is picked up on the next
        cycle instead of waiting for the next reconnect. A retirement that was
        sent but not yet acknowledged is only re-sent once per RETIRE_RESEND
        from here."""
        sources = set(sources)
        with self._lock:
            self._active = sources
            pending = self._retire_pending_locked()
        if pending:
            self.retire_inactive(now=now, resend=False)

    def _retire_pending_locked(self) -> bool:
        """True while retirement has work to do (caller holds the lock): a
        recorded entity of an inactive source not yet retired, or a retired
        entry that still waits for its acknowledgement (or for pruning)."""
        if self._active is None:
            return False
        return any(e.get("source") not in self._active or _retired_at(e) is not None
                   for e in self._record.values())

    def _live_topics_locked(self) -> set[str]:
        """State/availability topics an active, unretired entity still uses;
        those are never blanked or set offline by a retirement."""
        return {x for e in self._record.values()
                if e.get("source") in self._active and _retired_at(e) is None
                for x in (e.get("state"), e.get("availability")) if x}

    @staticmethod
    def _retire_extras(e: dict, live: set[str]) -> list[tuple[str, str]]:
        """The state clear + "offline" an entry's retirement sends besides the
        empty discovery payload (topics still in use are left alone)."""
        return [(x, p) for x, p in ((e.get("state"), ""), (e.get("availability"), "offline"))
                if x and x not in live]

    def _apply_acks_locked(self, now: float) -> bool:
        """Turn the PUBACKs queued by _on_publish into delivery evidence and
        mark every retired entry whose retirement messages have ALL been
        acknowledged "confirmed_at" (dropped on the next record write).
        Caller holds the lock. Returns True if an entry was confirmed."""
        while True:
            try:
                mid = self._acks.popleft()
            except IndexError:
                break
            topic = self._mid_topic.pop(mid, None)
            if topic is not None:
                self._acked_topics.add(topic)
        live = self._live_topics_locked()
        confirmed = 0
        for topic, e in list(self._record.items()):
            if _retired_at(e) is None or e.get("confirmed_at") is not None:
                continue
            need = {topic} | {x for x, _p in self._retire_extras(e, live)}
            if need <= self._acked_topics:
                self._record[topic] = dict(e, confirmed_at=now)
                confirmed += 1
        if confirmed:
            self.log(f"[vitals] broker acknowledged the retirement of {confirmed} MQTT "
                     f"entit{'y' if confirmed == 1 else 'ies'}; dropped from the record")
        return bool(confirmed)

    def retire_inactive(self, now: float | None = None, resend: bool = True) -> int:
        """Remove every recorded entity whose source is not active: an EMPTY
        retained payload on its discovery topic (that is how Home Assistant
        deletes an MQTT-discovered entity), the retained state cleared, and
        "offline" on its availability topic.

        The publishes use QoS 1 but are not waited on (this runs on paho's
        network thread from _on_connect). Once all three are accepted the
        entry is marked "retired_at"; it is "confirmed_at" (and dropped from
        the record) only when the broker has acknowledged every one of them
        (PUBACK, seen through _on_publish). Until then it is re-sent on every
        connect (resend=True) and, during polling (resend=False), at most once
        per RETIRE_RESEND. An entry still unconfirmed after RETIRE_KEEP (7
        days) is pruned with a warning naming its topic. An entry with any of
        its three publishes refused stays unmarked and is retried on every
        connect and poll cycle. Needs a live connection (acknowledgements and
        pruning do not). Returns how many entries were newly marked retired."""
        now = time.time() if now is None else now
        newly = 0
        with self._lock:
            if self._active is None:
                return 0
            dirty = self._apply_acks_locked(now)
            expired = [t for t, e in self._record.items()
                       if _retired_at(e) is not None and e.get("confirmed_at") is None
                       and now - _retired_at(e) >= RETIRE_KEEP]
            for t in expired:
                del self._record[t]
                self._sent_at.pop(t, None)
            dirty = dirty or bool(expired)
            if self.c is not None and self._connected:
                live = self._live_topics_locked()
                # unmarked entries every pass; marked, unconfirmed ones on a
                # connect or once their last send is RETIRE_RESEND old
                gone = {t: e for t, e in self._record.items()
                        if e.get("source") not in self._active
                        and e.get("confirmed_at") is None
                        and (resend or _retired_at(e) is None
                             or now - self._sent_at.get(t, 0.0) >= RETIRE_RESEND)}
                sent: dict[str, bool] = {}
                for topic, e in sorted(gone.items()):
                    ok = self._try_pub(topic, "")
                    extras = self._retire_extras(e, live)
                    for extra, payload in extras:
                        if extra not in sent:
                            sent[extra] = self._try_pub(extra, payload)
                            self._avail.pop(extra, None)
                            self._last_state.pop(extra, None)
                        ok = ok and sent[extra]
                    if _retired_at(e) is not None:
                        self._sent_at[topic] = now     # re-send of a marked entry
                        continue
                    if not ok:
                        continue                       # refused -> stays unmarked, retried
                    # acknowledgements older than this pass prove nothing
                    # about it (the queued PUBACKs of this pass come later)
                    self._acked_topics.difference_update({topic} | {x for x, _p in extras})
                    self._record[topic] = dict(e, retired_at=now)
                    self._sent_at[topic] = now
                    self._discovery.pop(topic, None)
                    if e.get("uid"):
                        self._announced.discard(e["uid"])
                    newly += 1
                    dirty = True
            if dirty:
                self._save_record()
        for t in expired:
            self.log(f"[vitals] WARNING: the broker never acknowledged the retirement of "
                     f"MQTT discovery topic {t} within 7 days; dropped it from the record. "
                     "If Home Assistant still shows that entity, clear the retained topic "
                     "by hand (see docs/mqtt.md).")
        if newly:
            self.log(f"[vitals] retired {newly} MQTT entit{'y' if newly == 1 else 'ies'} "
                     "of disabled source(s) (empty retained discovery payload; re-sent "
                     "until the broker acknowledges it)")
        return newly

    def _try_pub(self, topic: str, payload: str) -> bool:
        """QoS 1 retained publish, not waited on; False if paho refused it.
        Caller holds the lock. The message id is remembered so its PUBACK
        (_on_publish) can be matched to the topic; a newer send on the same
        topic replaces the older id."""
        try:
            info = self.c.publish(topic, payload, qos=1, retain=True)
        except Exception:  # noqa: BLE001
            return False
        rc = getattr(info, "rc", 0)
        if rc:
            return False
        mid = getattr(info, "mid", None)
        if isinstance(mid, int) and not isinstance(mid, bool):
            for old in [m for m, t in self._mid_topic.items() if t == topic]:
                del self._mid_topic[old]
            self._mid_topic[mid] = topic
        return True

    def _on_publish(self, _client, _userdata, mid, *_a):
        """paho callback (network thread) once a publish completes -- for QoS 1
        that is the broker's PUBACK. It only queues the message id and never
        takes self._lock: paho can hold its own message lock around this call
        while another thread holds self._lock inside publish() waiting for
        that same paho lock. The ids are matched on the next retirement pass
        (every poll cycle while one is pending, and every connect)."""
        self._acks.append(mid)

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
                # message ids and acknowledgements belong to the old client
                # (and possibly another broker)
                self._acks.clear()
                self._mid_topic.clear()
                self._acked_topics.clear()
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
            c.on_disconnect = self._on_disconnect
            c.on_publish = self._on_publish
            c.connect(s["host"], int(s.get("port") or 1883), 60)
            # Assigned BEFORE the network loop starts: a fast CONNACK runs
            # _on_connect on paho's thread, and its retirement pass needs
            # self.c (it returned early when the client was set afterwards).
            self.c = c
            try:
                c.loop_start()
            except Exception:
                self.c = None
                raise
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
        self._connected = True
        client.publish(f"{STATE_PREFIX}/bridge/status", "online", retain=True)
        # The replay is published while HOLDING the lock the publisher uses for
        # its availability cache + publish. Publishing a snapshot after
        # releasing it let the publisher flip a device to "offline" in between;
        # the replay then put the old "online" back on the wire, and since the
        # cache already said "offline" the correction was never sent.
        with self._lock:
            replay = list(self._discovery.items()) + list(self._avail.items())
            self._last_state.clear()     # next loop re-sends every state payload
            for topic, payload in replay:
                client.publish(topic, payload, retain=True)
        self.log(f"[vitals] MQTT connected; republished online + {len(replay)} "
                 "discovery/availability message(s)")
        self.retire_inactive()

    def _on_disconnect(self, *_a):
        self._connected = False

    def _disconnect(self):
        self._connected = False
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
        # which publisher this device came from (poll_once tags it): "cam" =
        # the local camera sidecars, "cloud" = the Ayla sock poller
        source = dev.get("source") or ("cam" if kind == "cam" else "cloud")
        sensors = _to_us(dev.get("sensors") or {})
        state_topic = f"{STATE_PREFIX}/{node}/state"
        avail_topic = f"{STATE_PREFIX}/{node}/availability"
        fresh = {k: v for k, v in sensors.items() if k in HA_META and v is not None}
        avail = "online" if fresh else "offline"
        # cache update and publish are one atomic step w.r.t. the reconnect
        # replay, so the wire can never end up disagreeing with the cache
        with self._lock:
            if self._avail.get(avail_topic) != avail:
                self._avail[avail_topic] = avail
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
            entry = {"source": source, "uid": uid, "node": node,
                     "state": state_topic, "availability": avail_topic}
            with self._lock:
                self._discovery[topic] = payload
                self._announced.add(uid)
                self._pub(topic, payload)
                if self._record.get(topic) != entry:
                    self._record[topic] = entry
                    self._save_record()
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


def active_sources() -> set[str]:
    """The sources whose MQTT entities this run keeps alive: the local camera
    sidecars always (this process only runs when the publisher is enabled),
    the Ayla sock poller only when OWLET_VITALS_POLL=1."""
    return {"cam", "cloud"} if cloud_enabled() else {"cam"}


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
    # before ensure(): the on-connect handler retires entities of sources that
    # are not in this set (e.g. sock entities once the cloud poller is off)
    mq.set_active_sources(active_sources())
    mq.ensure(cs.mqtt_settings(cfg))   # pick up UI changes live
    devices = cloud.poll(cfg, now)
    for d in devices:
        d.setdefault("source", "cloud")
    # merge live cam sensors from tutk_client sidecars (cloud-independent)
    cam_devs = _read_cam_sidecars(now=now)
    for c in cam_devs:
        c["source"] = "cam"
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
