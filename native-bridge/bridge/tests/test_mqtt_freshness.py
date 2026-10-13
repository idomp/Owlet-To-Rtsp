"""MQTT availability after reconnect + room-sensor freshness. Stdlib-only:
paho is replaced by a fake module, no broker is contacted.

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import collections.abc
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config_store as cs  # noqa: E402
import tutk_client as tc  # noqa: E402
import vitals_poller as vp  # noqa: E402


class FakeInfo:
    """paho's MQTTMessageInfo: rc, mid and is_published(), which turns true
    once the broker acknowledged the message (a QoS 0 message counts as
    published once written). Tests flip it through FakeClient.ack()."""

    def __init__(self, mid, topic, qos):
        self.rc = 0
        self.mid = mid
        self.topic = topic
        self.published = not qos

    def is_published(self):
        return self.published


class FakeClient:
    def __init__(self, *a, **k):
        self.pubs = []
        self.on_connect = None
        self.on_publish = None
        self.will = None
        self._next_mid = 0
        self.inflight = []      # FakeInfo of QoS 1 publishes not yet acknowledged
        self.calls = []         # lifecycle calls in order

    def username_pw_set(self, u, p):
        pass

    def will_set(self, topic, payload, retain=False):
        self.will = (topic, payload, retain)

    def connect(self, host, port, keepalive):
        self.calls.append("connect")

    def loop_start(self):
        self.calls.append("loop_start")

    def loop_stop(self):
        self.calls.append("loop_stop")

    def disconnect(self):
        self.calls.append("disconnect")

    def publish(self, topic, payload=None, qos=0, retain=False):
        """paho's signature: qos and retain may come as keywords in any order.
        Returns an MQTTMessageInfo-like object (FakeInfo)."""
        self.pubs.append((topic, payload, retain))
        self.qos.append((topic, qos))
        self._next_mid += 1
        info = FakeInfo(self._next_mid, topic, qos)
        if qos:
            self.inflight.append(info)
        elif self.on_publish:                  # paho reports QoS 0 sends too
            self.on_publish(self, None, info.mid)
        return info

    @property
    def qos(self):
        return self.__dict__.setdefault("_qos", [])

    # helpers
    def topics(self):
        return [t for t, _p, _r in self.pubs]

    def ack(self, topics=None):
        """Simulate the broker's PUBACK: paho marks the MQTTMessageInfo of
        every acknowledged QoS 1 message published (all in flight, or only
        those on `topics`)."""
        done = [i for i in self.inflight if topics is None or i.topic in topics]
        self.inflight = [i for i in self.inflight if i not in done]
        for info in done:
            info.published = True


class SyncConnectClient(FakeClient):
    """A broker that answers at once: the CONNACK callback runs inside
    loop_start(), before ensure() has returned."""

    def loop_start(self):
        if self.on_connect:
            self.on_connect(self, None, {}, 0)


def _fake_paho(client_cls=FakeClient):
    client_mod = types.ModuleType("paho.mqtt.client")
    client_mod.Client = client_cls
    paho = types.ModuleType("paho")
    mqtt = types.ModuleType("paho.mqtt")
    paho.mqtt = mqtt
    mqtt.client = client_mod
    return {"paho": paho, "paho.mqtt": mqtt, "paho.mqtt.client": client_mod}


SETTINGS = {"enabled": True, "host": "10.0.0.5", "port": "1883", "user": "",
            "password": "", "prefix": "homeassistant"}


class MqttReconnectTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(sys.modules, _fake_paho())
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.record = os.path.join(tmp.name, "discovery.json")
        self.m = vp._Mqtt(lambda *_: None, record_path=self.record)
        self.m.ensure(SETTINGS)
        self.c = self.m.c
        self.assertIsNotNone(self.c.on_connect, "must register an on_connect handler")

    def cam(self, **sensors):
        return {"dsn": "nursery", "name": "nursery", "kind": "cam", "model": "Owlet Cam",
                "sensors": sensors, "ts": 1_700_000_000.0}

    def test_online_published_on_connack(self):
        self.c.on_connect(self.c, None, {}, 0)
        self.assertIn(("owlet/bridge/status", "online", True), self.c.pubs)
        self.assertEqual(self.c.will, ("owlet/bridge/status", "offline", True))

    def test_refused_connack_does_not_claim_online(self):
        self.c.on_connect(self.c, None, {}, 5)
        self.assertNotIn("owlet/bridge/status", self.c.topics())

    def test_reconnect_republishes_online_discovery_and_state(self):
        self.c.on_connect(self.c, None, {}, 0)
        self.m.publish_device(self.cam(temperature=22, humidity=45))
        disc = [t for t in self.c.topics() if t.endswith("/config")]
        self.assertEqual(len(disc), 2)
        self.c.pubs.clear()
        # broker blip: paho reconnects silently and fires on_connect again
        self.c.on_connect(self.c, None, {}, 0)
        topics = self.c.topics()
        self.assertIn("owlet/bridge/status", topics)
        for t in disc:
            self.assertIn(t, topics)
        self.assertIn("owlet/nursery/availability", topics)
        # and the next poll re-sends the state even though it is unchanged
        self.c.pubs.clear()
        self.m.publish_device(self.cam(temperature=22, humidity=45))
        self.assertIn("owlet/nursery/state", self.c.topics())

    def test_replay_racing_an_offline_flip_leaves_wire_equal_to_cache(self):
        """The publisher marks the camera offline WHILE the reconnect replay is
        in progress. Whatever the interleaving, the last availability message
        on the wire must match the cache (the old code replayed a stale
        "online" after the publisher's "offline": wire online, cache offline)."""
        import threading
        avail = "owlet/nursery/availability"
        self.m.publish_device(self.cam(temperature=22))      # cache + wire: online
        flip = {"thread": None}
        real_publish = self.c.publish

        def publish(topic, payload=None, qos=0, retain=False):
            if topic == avail and flip["thread"] is None:
                # the poll thread runs mid-replay and flips the camera offline
                t = threading.Thread(target=self.m.publish_device, args=(self.cam(),))
                flip["thread"] = t
                t.start()
                t.join(0.3)     # blocks on the lock with the fix; finishes without it
            return real_publish(topic, payload, qos=qos, retain=retain)

        self.c.publish = publish
        self.c.on_connect(self.c, None, {}, 0)
        flip["thread"].join(5)
        self.assertFalse(flip["thread"].is_alive())
        wire = [p for t, p, _r in self.c.pubs if t == avail]
        self.assertEqual(self.m._avail[avail], "offline")
        self.assertEqual(wire[-1], self.m._avail[avail])

    def test_unchanged_reading_not_republished(self):
        self.m.publish_device(self.cam(temperature=22))
        self.c.pubs.clear()
        self.m.publish_device(self.cam(temperature=22))
        self.assertNotIn("owlet/nursery/state", self.c.topics())

    def test_state_carries_measurement_time(self):
        self.m.publish_device(self.cam(temperature=22))
        st = [json.loads(p) for t, p, _r in self.c.pubs if t == "owlet/nursery/state"][0]
        self.assertEqual(st["measured_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(st["temperature"], 72)   # °F, as before

    def test_no_fresh_readings_marks_device_unavailable_and_sends_no_state(self):
        self.m.publish_device(self.cam(temperature=22))
        self.c.pubs.clear()
        self.m.publish_device(self.cam())
        self.assertIn(("owlet/nursery/availability", "offline", True), self.c.pubs)
        self.assertNotIn("owlet/nursery/state", self.c.topics())

    def test_field_that_went_stale_is_sent_as_null(self):
        self.m.publish_device(self.cam(temperature=22, humidity=45))
        self.c.pubs.clear()
        self.m.publish_device(self.cam(temperature=23))
        st = [json.loads(p) for t, p, _r in self.c.pubs if t == "owlet/nursery/state"][0]
        self.assertIsNone(st["humidity"])

    def test_discovery_uses_device_availability(self):
        self.m.publish_device(self.cam(temperature=22))
        cfg = [json.loads(p) for t, p, _r in self.c.pubs if t.endswith("/config")][0]
        self.assertEqual(cfg["availability_mode"], "all")
        self.assertEqual({a["topic"] for a in cfg["availability"]},
                         {"owlet/bridge/status", "owlet/nursery/availability"})


class DiscoveryRecordTests(unittest.TestCase):
    """Discovery topics are recorded in <config>/vitals/discovery.json and the
    entities of a source that is no longer running are retired."""
    SOCK_CFG = "homeassistant/sensor/owlet_sock1_heart_rate/config"
    CAM_CFG = "homeassistant/sensor/owlet_nursery_temperature/config"

    def setUp(self):
        p = mock.patch.dict(sys.modules, _fake_paho())
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.record = os.path.join(tmp.name, "vitals", "discovery.json")

    def mqtt(self, log=None):
        m = vp._Mqtt(log or (lambda *_: None), record_path=self.record)
        m.ensure(SETTINGS)
        return m, m.c

    def recorded(self):
        with open(self.record) as f:
            return json.load(f)["topics"]

    @staticmethod
    def sock():
        return {"dsn": "SOCK1", "name": "sock", "kind": "sock", "source": "cloud",
                "sensors": {"heart_rate": 120}, "ts": 1_700_000_000.0}

    @staticmethod
    def cam():
        return {"dsn": "nursery", "name": "nursery", "kind": "cam", "source": "cam",
                "sensors": {"temperature": 22}, "ts": 1_700_000_000.0}

    def test_published_discovery_topics_are_recorded_with_their_source(self):
        m, c = self.mqtt()
        c.on_connect(c, None, {}, 0)
        m.publish_device(self.cam())
        m.publish_device(self.sock())
        rec = self.recorded()
        self.assertEqual(rec[self.CAM_CFG]["source"], "cam")
        self.assertEqual(rec[self.SOCK_CFG]["source"], "cloud")
        self.assertEqual(rec[self.SOCK_CFG]["availability"], "owlet/sock1/availability")

    def test_disabled_sock_poller_entities_are_cleared_and_marked_retired(self):
        # run 1: cloud poller on, both sources publish
        m, c = self.mqtt()
        m.set_active_sources({"cam", "cloud"})
        c.on_connect(c, None, {}, 0)
        m.publish_device(self.cam())
        m.publish_device(self.sock())
        self.assertEqual(set(self.recorded()), {self.CAM_CFG, self.SOCK_CFG})
        # run 2 (restart with OWLET_VITALS_POLL=0): the record survived
        m2, c2 = self.mqtt()
        with mock.patch.dict(os.environ, {"OWLET_VITALS_POLL": "0"}):
            m2.set_active_sources(vp.active_sources())
        self.assertEqual(c2.pubs, [], "nothing is retired before the broker connects")
        c2.on_connect(c2, None, {}, 0)
        self.assertIn((self.SOCK_CFG, "", True), c2.pubs)         # HA deletes the entity
        self.assertIn(("owlet/sock1/availability", "offline", True), c2.pubs)
        self.assertIn(("owlet/sock1/state", "", True), c2.pubs)
        self.assertNotIn((self.CAM_CFG, "", True), c2.pubs)       # active source kept
        self.assertIn((self.SOCK_CFG, 1), c2.qos)                  # QoS 1, not waited on
        # kept in the record, marked retired, until delivery can be assumed
        rec = self.recorded()
        self.assertEqual(set(rec), {self.CAM_CFG, self.SOCK_CFG})
        self.assertIsInstance(rec[self.SOCK_CFG].get("retired_at"), (int, float))
        self.assertNotIn("retired_at", rec[self.CAM_CFG])
        # a later reconnect sends the three retirement messages again
        c2.pubs.clear()
        c2.on_connect(c2, None, {}, 0)
        self.assertIn((self.SOCK_CFG, "", True), c2.pubs)
        self.assertIn(("owlet/sock1/state", "", True), c2.pubs)
        self.assertIn(("owlet/sock1/availability", "offline", True), c2.pubs)
        self.assertNotIn(self.CAM_CFG, c2.topics())

    def write_record(self, topics):
        os.makedirs(os.path.dirname(self.record), exist_ok=True)
        with open(self.record, "w") as f:
            json.dump({"version": 1, "topics": topics}, f)

    def sock_entry(self, **extra):
        e = {"source": "cloud", "uid": "owlet_sock1_heart_rate", "node": "sock1",
             "state": "owlet/sock1/state", "availability": "owlet/sock1/availability"}
        e.update(extra)
        return e

    RETIRE_MSGS = ((SOCK_CFG, "", True), ("owlet/sock1/state", "", True),
                   ("owlet/sock1/availability", "offline", True))

    def test_unconfirmed_entry_is_resent_on_connect_and_once_per_interval(self):
        # a previous run marked it, then crashed before the packets left
        marked_at = time.time() - 3600
        self.write_record({self.SOCK_CFG: self.sock_entry(retired_at=marked_at)})
        m, c = self.mqtt()
        m.set_active_sources({"cam"})
        c.on_connect(c, None, {}, 0)
        t0 = time.time()
        for msg in self.RETIRE_MSGS:
            self.assertIn(msg, c.pubs)
        self.assertEqual(self.recorded()[self.SOCK_CFG]["retired_at"], marked_at)
        # the following poll cycles do NOT re-send it every time ...
        c.pubs.clear()
        for dt in (15, 30, vp.RETIRE_RESEND - 1):
            m.set_active_sources({"cam"}, now=t0 + dt)
        self.assertEqual(c.pubs, [])
        # ... only once the last send is RETIRE_RESEND old, and then once
        m.set_active_sources({"cam"}, now=t0 + vp.RETIRE_RESEND + 1)
        for msg in self.RETIRE_MSGS:
            self.assertIn(msg, c.pubs)
        c.pubs.clear()
        m.set_active_sources({"cam"}, now=t0 + vp.RETIRE_RESEND + 16)
        self.assertEqual(c.pubs, [])
        # a reconnect re-sends it regardless of the interval
        c.on_connect(c, None, {}, 0)
        self.assertIn((self.SOCK_CFG, "", True), c.pubs)
        self.assertIn(self.SOCK_CFG, self.recorded())

    def test_acknowledged_entry_is_dropped_and_not_resent(self):
        self.write_record({self.SOCK_CFG: self.sock_entry()})
        m, c = self.mqtt()
        m.set_active_sources({"cam"})
        c.on_connect(c, None, {}, 0)
        self.assertIn("retired_at", self.recorded()[self.SOCK_CFG])
        c.ack()                                # PUBACK for all three messages
        m.set_active_sources({"cam"})          # next poll applies them
        self.assertEqual(self.recorded(), {})
        c.pubs.clear()
        c.on_connect(c, None, {}, 0)           # next connect
        m.set_active_sources({"cam"}, now=time.time() + vp.RETIRE_RESEND + 1)
        self.assertNotIn(self.SOCK_CFG, c.topics())
        self.assertNotIn("owlet/sock1/state", c.topics())
        # and a restart does not bring it back either
        m2, c2 = self.mqtt()
        m2.set_active_sources({"cam"})
        c2.on_connect(c2, None, {}, 0)
        self.assertNotIn(self.SOCK_CFG, c2.topics())

    def test_partial_acknowledgement_does_not_confirm(self):
        self.write_record({self.SOCK_CFG: self.sock_entry()})
        m, c = self.mqtt()
        m.set_active_sources({"cam"})
        c.on_connect(c, None, {}, 0)
        c.ack(topics={self.SOCK_CFG, "owlet/sock1/state"})   # availability lost
        m.set_active_sources({"cam"})
        self.assertIn(self.SOCK_CFG, self.recorded())
        self.assertNotIn("confirmed_at", self.recorded()[self.SOCK_CFG])
        c.ack()                                              # finally acknowledged
        m.set_active_sources({"cam"})
        self.assertEqual(self.recorded(), {})

    def test_puback_while_the_lock_is_held_does_not_block(self):
        self.write_record({self.SOCK_CFG: self.sock_entry()})
        m, c = self.mqtt()
        self.assertIsNone(c.on_publish, "no publish callback is registered")
        m.set_active_sources({"cam"})
        c.on_connect(c, None, {}, 0)
        # the network thread acknowledges while a publisher holds the lock
        with m._lock:
            t = threading.Thread(target=c.ack, daemon=True)
            t.start()
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "the PUBACK waited for the publisher lock")
        m.set_active_sources({"cam"})          # next pass sees the acknowledgement
        self.assertEqual(self.recorded(), {})

    def test_ordinary_publishes_leave_no_per_message_state(self):
        m, c = self.mqtt()
        m.set_active_sources({"cam"})
        c.on_connect(c, None, {}, 0)
        for i in range(5000):                  # QoS 0 state, nothing to retire
            m.publish_device(dict(self.cam(), sensors={"temperature": 20 + i % 7}))
            m.set_active_sources({"cam"})
        self.assertGreater(len(c.pubs), 4000)
        for name, value in vars(m).items():
            if isinstance(value, collections.abc.Collection) and not isinstance(value, str):
                self.assertLess(len(value), 50, f"_Mqtt.{name} grows with every publish")

    def test_unacknowledged_resends_keep_a_bounded_number_of_handles(self):
        self.write_record({self.SOCK_CFG: self.sock_entry()})
        m, c = self.mqtt()
        m.set_active_sources({"cam"})
        for _ in range(50):                    # reconnects re-send every time
            c.on_connect(c, None, {}, 0)
        track = m._retire_msgs[self.SOCK_CFG]
        self.assertEqual(set(track), {self.SOCK_CFG, "owlet/sock1/state",
                                      "owlet/sock1/availability"})
        for infos in track.values():
            self.assertLessEqual(len(infos), vp.RETIRE_INFLIGHT_KEEP)
        c.ack()
        m.set_active_sources({"cam"})
        self.assertEqual(self.recorded(), {})
        self.assertEqual(m._retire_msgs, {})

    def test_unconfirmed_entry_older_than_seven_days_is_pruned_with_a_warning(self):
        old = time.time() - vp.RETIRE_KEEP - 60
        self.write_record({self.SOCK_CFG: self.sock_entry(retired_at=old)})
        logs = []
        m, c = self.mqtt(log=logs.append)
        m.set_active_sources({"cam"})
        c.on_connect(c, None, {}, 0)
        self.assertNotIn(self.SOCK_CFG, c.topics())
        self.assertEqual(self.recorded(), {})
        warn = [line for line in logs if "WARNING" in line]
        self.assertEqual(len(warn), 1)
        self.assertIn(self.SOCK_CFG, warn[0])

    def test_failed_state_clear_keeps_the_entry_unmarked(self):
        self.write_record({self.SOCK_CFG: self.sock_entry()})
        m, c = self.mqtt()
        real = c.publish

        def publish(topic, payload=None, retain=False, qos=0):   # keywords, other order
            info = real(topic, payload, retain=retain, qos=qos)
            if topic == "owlet/sock1/state":
                info.rc = 4
            return info
        c.publish = publish
        m.set_active_sources({"cam"})
        c.on_connect(c, None, {}, 0)
        self.assertIn((self.SOCK_CFG, "", True), c.pubs)        # discovery clear went out
        self.assertNotIn("retired_at", self.recorded()[self.SOCK_CFG])
        # acknowledging what did go out cannot confirm an unmarked entry
        c.ack()
        m.set_active_sources({"cam"})
        self.assertIn(self.SOCK_CFG, self.recorded())
        self.assertNotIn("retired_at", self.recorded()[self.SOCK_CFG])
        # retried on the next cycle once the publish is accepted
        c.publish = real
        m.set_active_sources({"cam"})
        self.assertIn("retired_at", self.recorded()[self.SOCK_CFG])

    def test_failed_clear_stays_unmarked_and_is_retried_until_confirmed(self):
        m, c = self.mqtt()
        c.on_connect(c, None, {}, 0)
        m.publish_device(self.sock())
        m2, c2 = self.mqtt()
        real = c2.publish
        c2.publish = lambda *a, **k: types.SimpleNamespace(rc=4, mid=None)  # MQTT_ERR_NO_CONN
        c2.on_connect(c2, None, {}, 0)
        m2.set_active_sources({"cam"})
        # refused: kept, and NOT marked retired
        self.assertIn(self.SOCK_CFG, self.recorded())
        self.assertNotIn("retired_at", self.recorded()[self.SOCK_CFG])
        # retried on the next poll cycle; accepted this time -> marked
        c2.publish = real
        m2.set_active_sources({"cam"})
        for msg in self.RETIRE_MSGS:
            self.assertIn(msg, c2.pubs)
        self.assertIn("retired_at", self.recorded()[self.SOCK_CFG])
        # the broker acknowledges all three -> confirmed and dropped
        c2.ack()
        m2.set_active_sources({"cam"})
        self.assertNotIn(self.SOCK_CFG, self.recorded())

    def test_immediate_connack_still_retires(self):
        m, c = self.mqtt()
        c.on_connect(c, None, {}, 0)
        m.publish_device(self.sock())
        # restart with the poller off; the broker answers inside loop_start()
        with mock.patch.dict(sys.modules, _fake_paho(SyncConnectClient)):
            m2 = vp._Mqtt(lambda *_: None, record_path=self.record)
            m2.set_active_sources({"cam"})     # poll_once order: before ensure()
            m2.ensure(SETTINGS)
        c2 = m2.c
        self.assertIsInstance(c2, SyncConnectClient)
        self.assertIn((self.SOCK_CFG, "", True), c2.pubs)
        self.assertIn(("owlet/sock1/availability", "offline", True), c2.pubs)

    def test_failed_pass_is_retried_on_the_next_poll_cycle(self):
        m, c = self.mqtt()
        c.on_connect(c, None, {}, 0)
        m.publish_device(self.sock())
        m2, c2 = self.mqtt()
        m2.set_active_sources({"cam"})
        real = c2.publish
        c2.publish = lambda *a, **k: types.SimpleNamespace(rc=4)   # MQTT_ERR_NO_CONN
        c2.on_connect(c2, None, {}, 0)
        self.assertNotIn((self.SOCK_CFG, "", True), c2.pubs)
        c2.publish = real
        m2.set_active_sources({"cam"})     # unchanged set, next poll cycle
        self.assertIn((self.SOCK_CFG, "", True), c2.pubs)

    def test_active_sources_follow_the_poller_switch(self):
        with mock.patch.dict(os.environ, {"OWLET_VITALS_POLL": "0"}):
            self.assertEqual(vp.active_sources(), {"cam"})
        with mock.patch.dict(os.environ, {"OWLET_VITALS_POLL": "1"}):
            self.assertEqual(vp.active_sources(), {"cam", "cloud"})


class SidecarFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(cs, "VITALS_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        p3 = mock.patch.object(cs, "load_config", lambda: {"cameras": [{"name": "nursery"}]})
        p3.start()
        self.addCleanup(p3.stop)

    def write(self, data, name="nursery"):
        with open(cs.cam_sensors_path(name), "w") as f:
            json.dump(data, f)

    def test_placeholder_name_is_not_read_when_no_camera_is_configured(self):
        # camera_names() invents "owlet" for an empty config; a deleted last
        # camera's sidecar must not come back through it
        now = 10_000.0
        self.write({"temperature": 21.5, "ts": now, "field_ts": {"temperature": now}}, name="owlet")
        with mock.patch.object(cs, "load_config", lambda: {"cameras": []}):
            self.assertEqual(vp._read_cam_sidecars(now=now), [])
        with mock.patch.object(cs, "load_config", lambda: {"cameras": [{"name": "owlet"}]}):
            self.assertEqual([d["dsn"] for d in vp._read_cam_sidecars(now=now)], ["owlet"])

    def test_per_field_staleness(self):
        now = 10_000.0
        self.write({"temperature": 22, "humidity": 45, "ts": now,
                    "field_ts": {"temperature": now - 2, "humidity": now - 3600}})
        d = vp._read_cam_sidecars(now=now)[0]
        self.assertEqual(d["sensors"], {"temperature": 22})
        self.assertEqual(d["ts"], now - 2)
        self.assertFalse(d["stale"])

    def test_dead_stream_sidecar_is_stale(self):
        now = 10_000.0
        self.write({"temperature": 22, "ts": now - 600,
                    "field_ts": {"temperature": now - 600}})
        d = vp._read_cam_sidecars(now=now)[0]
        self.assertEqual(d["sensors"], {})
        self.assertTrue(d["stale"])

    def test_legacy_sidecar_without_field_ts_uses_file_ts(self):
        now = 10_000.0
        self.write({"temperature": 22, "ts": now - 5})
        self.assertEqual(vp._read_cam_sidecars(now=now)[0]["sensors"], {"temperature": 22})
        self.write({"temperature": 22, "ts": now - 500})
        self.assertEqual(vp._read_cam_sidecars(now=now)[0]["sensors"], {})


class TutkSidecarWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "cam-x.json")
        for name, val in (("CAM_SENSORS_PATH", self.path), ("SENSORS_ENABLED", True),
                          ("TEMP_SCALE", 1.0), ("_CAM_SENSORS", {})):
            p = mock.patch.object(tc, name, val)
            p.start()
            self.addCleanup(p.stop)

    def read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_each_field_gets_its_own_timestamp(self):
        tc._publish_cam_sensors(temperature=22)
        t1 = self.read()["field_ts"]["temperature"]
        time.sleep(0.01)
        tc._publish_cam_sensors(humidity=40)
        d = self.read()
        self.assertEqual(d["field_ts"]["temperature"], t1)
        self.assertGreater(d["field_ts"]["humidity"], t1)

    def test_not_available_sentinel_clears_old_value(self):
        tc._publish_cam_sensors(temperature=22, humidity=40)
        t, h = tc._gate_realtime(255, 255)
        tc._publish_cam_sensors(_clear=("temperature", "humidity"), temperature=t,
                                humidity=h, noise=30)
        d = self.read()
        self.assertNotIn("humidity", d)
        self.assertNotIn("temperature", d)
        self.assertEqual(d["noise"], 30)


class DisconnectOrderTests(unittest.TestCase):
    """paho's network thread keeps running while QoS 1 messages are
    unacknowledged and loop_stop() joins it without a timeout, so a settings
    change must disconnect first and only then stop the loop."""

    def test_settings_change_disconnects_before_stopping_the_loop(self):
        with mock.patch.dict(sys.modules, _fake_paho()):
            m = vp._Mqtt(lambda *_: None)
            m.ensure(SETTINGS)
            old = m.c
            m.ensure(dict(SETTINGS, host="other-broker"))
        self.assertEqual(old.calls, ["connect", "loop_start", "disconnect", "loop_stop"])
        self.assertIsNot(m.c, old)


if __name__ == "__main__":
    unittest.main()
