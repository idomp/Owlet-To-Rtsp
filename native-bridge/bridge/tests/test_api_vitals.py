"""/api/vitals serves the freshly filtered camera sensors, never a stale copy
from the snapshot cache (Flask test client; no camera, no network).

Skipped when Flask/requests aren't installed (the CI unit job is stdlib-only).

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import flask  # noqa: F401
    import requests  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False

if HAVE_FLASK:
    import webapp  # noqa: E402
    import config_store as cs  # noqa: E402

USER, PASS = "tester", "correct-horse"


def _auth():
    tok = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}


@unittest.skipUnless(HAVE_FLASK, "flask/requests not installed")
class ApiVitalsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = self.dir = tmp.name
        self.snap = os.path.join(d, "snapshot.json")
        for p in (mock.patch.object(webapp, "UI_USER", USER),
                  mock.patch.object(webapp, "UI_PASS", PASS),
                  mock.patch.object(webapp, "UI_SECRET_PATH", os.path.join(d, "ui.json")),
                  mock.patch.object(webapp, "_AUTH", {"t": 0.0, "user": "", "pass": ""}),
                  mock.patch.object(webapp, "VITALS_CACHE", self.snap),
                  mock.patch.object(cs, "VITALS_DIR", d),
                  mock.patch.object(cs, "camera_names", lambda cfg=None: ["nursery", "den"])):
            p.start()
            self.addCleanup(p.stop)
        self.c = webapp.app.test_client()
        self.now = time.time()

    def sidecar(self, name, **fields):
        """fields: key -> (value, age in seconds)"""
        data = {k: v for k, (v, _a) in fields.items()}
        data["ts"] = self.now
        data["field_ts"] = {k: self.now - a for k, (_v, a) in fields.items()}
        with open(cs.cam_sensors_path(name), "w") as f:
            json.dump(data, f)

    def cached(self, *devices):
        with open(self.snap, "w") as f:
            json.dump({"ts": self.now - 30, "ok": True, "devices": list(devices)}, f)

    def get(self):
        r = self.c.get("/api/vitals?units=metric", headers=_auth())
        self.assertEqual(r.status_code, 200)
        return {d["dsn"]: d for d in r.get_json()["devices"]}

    def test_expired_field_is_absent_not_the_cached_value(self):
        # snapshot taken while humidity was still fresh
        self.cached({"dsn": "nursery", "name": "nursery", "kind": "cam",
                     "sensors": {"temperature": 22, "humidity": 45}, "ts": self.now - 30})
        # since then humidity expired; temperature is current
        self.sidecar("nursery", temperature=(23, 2), humidity=(45, 3600))
        dev = self.get()["nursery"]
        self.assertEqual(dev["sensors"].get("temperature"), 23)
        self.assertIsNone(dev["sensors"].get("humidity"))

    def test_camera_with_only_expired_fields_loses_its_cached_entry(self):
        self.cached({"dsn": "den", "name": "den", "kind": "cam",
                     "sensors": {"temperature": 21}, "ts": self.now - 30})
        self.sidecar("den", temperature=(21, 3600))
        dev = self.get().get("den")
        self.assertTrue(dev is None or not dev["sensors"].get("temperature"))

    def test_cached_camera_no_longer_configured_is_dropped(self):
        # "attic" was renamed/deleted; only its old snapshot copy remains
        self.cached({"dsn": "attic", "name": "attic", "kind": "cam",
                     "sensors": {"temperature": 19}, "ts": self.now - 30},
                    {"dsn": "SOCK1", "name": "sock", "kind": "sock",
                     "sensors": {"heart_rate": 120}, "ts": self.now - 30})
        self.sidecar("nursery", temperature=(22, 1))
        devs = self.get()
        self.assertNotIn("attic", devs)
        self.assertIn("SOCK1", devs)
        self.assertEqual(devs["nursery"]["sensors"]["temperature"], 22)

    def test_cloud_devices_in_the_snapshot_are_kept(self):
        self.cached({"dsn": "SOCK1", "name": "sock", "kind": "sock",
                     "sensors": {"heart_rate": 120}, "ts": self.now - 30})
        self.sidecar("nursery", temperature=(22, 1))
        devs = self.get()
        self.assertEqual(devs["SOCK1"]["sensors"]["heart_rate"], 120)
        self.assertEqual(devs["nursery"]["sensors"]["temperature"], 22)


if __name__ == "__main__":
    unittest.main()
