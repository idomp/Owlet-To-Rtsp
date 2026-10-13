"""MQTT availability after reconnect + room-sensor freshness. Stdlib-only:
paho is replaced by a fake module, no broker is contacted.

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config_store as cs  # noqa: E402
import tutk_client as tc  # noqa: E402
import vitals_poller as vp  # noqa: E402


class FakeClient:
    def __init__(self, *a, **k):
        self.pubs = []
        self.on_connect = None
        self.will = None

    def username_pw_set(self, u, p):
        pass

    def will_set(self, topic, payload, retain=False):
        self.will = (topic, payload, retain)

    def connect(self, host, port, keepalive):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def publish(self, topic, payload, retain=False):
        self.pubs.append((topic, payload, retain))

    # helpers
    def topics(self):
        return [t for t, _p, _r in self.pubs]


def _fake_paho():
    client_mod = types.ModuleType("paho.mqtt.client")
    client_mod.Client = FakeClient
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
        self.m = vp._Mqtt(lambda *_: None)
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

        def publish(topic, payload, retain=False):
            if topic == avail and flip["thread"] is None:
                # the poll thread runs mid-replay and flips the camera offline
                t = threading.Thread(target=self.m.publish_device, args=(self.cam(),))
                flip["thread"] = t
                t.start()
                t.join(0.3)     # blocks on the lock with the fix; finishes without it
            real_publish(topic, payload, retain)

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


class SidecarFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(cs, "VITALS_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(cs, "camera_names", lambda cfg=None: ["nursery"])
        p2.start()
        self.addCleanup(p2.stop)

    def write(self, data):
        with open(cs.cam_sensors_path("nursery"), "w") as f:
            json.dump(data, f)

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


if __name__ == "__main__":
    unittest.main()
