"""Room sensors reach MQTT/HUD from the camera session alone; the sock cloud
poller (which WRITES APP_ACTIVE to the sock) is off by default. Stdlib-only;
the Owlet cloud modules are faked.

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
import vitals_poller as vp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class FakeMq:
    def __init__(self):
        self.devices = []

    def ensure(self, s):
        pass

    def publish_device(self, d):
        self.devices.append(d)


class Boom(Exception):
    pass


def _cloud_modules(counter):
    api = types.ModuleType("owlet_api")

    class OwletError(Exception):
        pass

    class OwletAPI:
        def __init__(self, *a, **k):
            counter["api"] += 1

    api.OwletAPI, api.OwletError = OwletAPI, OwletError
    vit = types.ModuleType("owlet_vitals")

    class OwletVitals:
        def __init__(self, api, log=None):
            pass

        def snapshot(self):
            counter["snapshot"] += 1   # (the real one POSTs APP_ACTIVE=1 to socks)
            return [{"dsn": "SOCK1", "name": "sock", "kind": "sock",
                     "sensors": {"heart_rate": 120}}]

    vit.OwletVitals = OwletVitals
    return {"owlet_api": api, "owlet_vitals": vit}


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.counter = {"api": 0, "snapshot": 0}
        for p in (mock.patch.multiple(cs, CONFIG_PATH=os.path.join(d, "owlet.yaml"),
                                      CONFIG_DIR=d, VITALS_DIR=d),
                  mock.patch.object(vp, "SNAP", os.path.join(d, "snapshot.json")),
                  mock.patch.object(cs, "load_config", lambda: {
                      "email": "a@b.c", "password": "pw", "region": "world",
                      "cameras": [{"name": "nursery"}], "mqtt": {}}),
                  mock.patch.object(cs, "camera_names", lambda cfg=None: ["nursery"]),
                  mock.patch.dict(sys.modules, _cloud_modules(self.counter))):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("OWLET_VITALS_POLL", None)
        now = time.time()
        with open(cs.cam_sensors_path("nursery"), "w") as f:
            json.dump({"temperature": 22, "humidity": 41, "noise": 30, "brightness": 12,
                       "ts": now, "field_ts": {"temperature": now, "humidity": now,
                                               "noise": now, "brightness": now}}, f)

    def test_defaults(self):
        self.assertFalse(vp.cloud_enabled())
        self.assertTrue(vp.publisher_enabled())

    def test_room_sensors_published_without_any_cloud_call(self):
        mq = FakeMq()
        vp.poll_once(mq, vp._Cloud(lambda *_: None), lambda *_: None)
        self.assertEqual(self.counter, {"api": 0, "snapshot": 0})
        cams = [d for d in mq.devices if d["kind"] == "cam"]
        self.assertEqual(cams[0]["sensors"],
                         {"temperature": 22, "humidity": 41, "noise": 30, "brightness": 12})
        with open(vp.SNAP) as f:
            self.assertTrue(json.load(f)["ok"])

    def test_sock_poller_only_when_opted_in(self):
        with mock.patch.dict(os.environ, {"OWLET_VITALS_POLL": "1"}):
            mq = FakeMq()
            vp.poll_once(mq, vp._Cloud(lambda *_: None), lambda *_: None)
        self.assertEqual(self.counter["snapshot"], 1)
        self.assertIn("sock", {d["kind"] for d in mq.devices})

    def test_cloud_backoff_does_not_delay_local_publishing(self):
        cloud = vp._Cloud(lambda *_: None)
        cloud.next_at = time.time() + 300      # cloud in backoff
        with mock.patch.dict(os.environ, {"OWLET_VITALS_POLL": "1"}):
            mq = FakeMq()
            vp.poll_once(mq, cloud, lambda *_: None)
        self.assertEqual(self.counter["snapshot"], 0)
        self.assertEqual([d["kind"] for d in mq.devices], ["cam"])


class StartupScriptTests(unittest.TestCase):
    def test_publisher_started_independently_of_sock_poller(self):
        with open(os.path.join(HERE, "..", "start-bionic.sh"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('"${OWLET_SENSOR_PUBLISH:-1}" != "0"', text)
        self.assertNotIn("${OWLET_VITALS_POLL:-1}", text)


try:
    import flask  # noqa: F401
    import requests  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False


@unittest.skipUnless(HAVE_FLASK, "flask/requests not installed")
class DiscoverGateTests(unittest.TestCase):
    def test_sock_discovery_endpoint_disabled_by_default(self):
        import base64
        import webapp
        with mock.patch.object(webapp, "UI_USER", "u"), mock.patch.object(webapp, "UI_PASS", "p"):
            r = webapp.app.test_client().post(
                "/api/vitals/discover",
                headers={"Authorization": "Basic " + base64.b64encode(b"u:p").decode()})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
