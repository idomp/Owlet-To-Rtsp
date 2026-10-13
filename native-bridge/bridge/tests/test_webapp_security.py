"""Control-panel security tests (Flask test client; no camera, no network).

Skipped when Flask/requests aren't installed (the CI unit job is stdlib-only).

Run:  python3 -m pytest native-bridge/bridge/tests -q
"""
import base64
import io
import os
import sys
import tempfile
import unittest
import zipfile
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


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("lib/x86_64/libIOTCAPIs.so", b"\x7fELF")
    return buf.getvalue()


@unittest.skipUnless(HAVE_FLASK, "flask/requests not installed")
class WebappTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        patches = [
            mock.patch.object(webapp, "UI_USER", USER),
            mock.patch.object(webapp, "UI_PASS", PASS),
            mock.patch.object(webapp, "UPLOAD_DIR", os.path.join(d, "uploads")),
            mock.patch.object(cs, "CONFIG_PATH", os.path.join(d, "owlet.yaml")),
            mock.patch.object(cs, "CONFIG_DIR", d),
            mock.patch.object(cs, "CAM_DIR", os.path.join(d, "cameras")),
            mock.patch.object(cs, "ENV_PATH", os.path.join(d, "owlet.env")),
            mock.patch.object(cs, "GEN_PATH", os.path.join(d, "go2rtc.gen.yaml")),
            mock.patch.object(cs, "SECRETS_DIR", os.path.join(d, "secrets")),
            mock.patch.object(cs, "G2_SECRET_PATH", os.path.join(d, "secrets", "go2rtc.json")),
            mock.patch.object(webapp, "restart_go2rtc", lambda *a, **k: None),
            mock.patch.object(webapp, "_go2rtc_streams", lambda: {}),
            mock.patch.object(webapp, "UI_SECRET_PATH", os.path.join(d, "secrets", "ui.json")),
            mock.patch.object(webapp, "_AUTH", {"t": 0.0, "user": "", "pass": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.c = webapp.app.test_client()
        self.dir = d


class UploadApkTests(WebappTestBase):
    def post(self, name, data):
        with mock.patch("lib_extract.provision", return_value=(True, "ok")) as prov:
            r = self.c.post("/api/upload_apk", headers=_auth(),
                            data={"apk": (io.BytesIO(data), name)},
                            content_type="multipart/form-data")
        return r, prov

    def test_valid_apk_saved_only_in_uploads_dir_with_generated_name(self):
        r, prov = self.post("Owlet.apkm", _zip_bytes())
        self.assertEqual(r.status_code, 200, r.get_json())
        files = os.listdir(os.path.join(self.dir, "uploads"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith("owlet-") and files[0].endswith(".apkm"))
        self.assertEqual(prov.call_args[0][1], [os.path.join(self.dir, "uploads")])
        # nothing written into the config root
        self.assertEqual(sorted(os.listdir(self.dir)), ["uploads"])

    def test_config_file_names_are_rejected(self):
        for name in ("go2rtc.gen.yaml", "owlet.yaml", "owlet.env", "x.apk.yaml"):
            with self.subTest(name=name):
                r, prov = self.post(name, _zip_bytes())
                self.assertEqual(r.status_code, 400)
                prov.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.dir, "go2rtc.gen.yaml")))

    def test_path_traversal_rejected(self):
        for name in ("../owlet.apk", "..\\owlet.apk", "sub/owlet.apk", "/etc/x.apk"):
            with self.subTest(name=name):
                r, _ = self.post(name, _zip_bytes())
                self.assertEqual(r.status_code, 400)

    def test_non_zip_content_rejected(self):
        r, prov = self.post("owlet.apk", b"#!/bin/sh\necho pwned\n")
        self.assertEqual(r.status_code, 400)
        prov.assert_not_called()
        self.assertEqual(os.listdir(os.path.join(self.dir, "uploads"))
                         if os.path.isdir(os.path.join(self.dir, "uploads")) else [], [])

    def test_zip_magic_but_corrupt_rejected(self):
        r, prov = self.post("owlet.apk", b"PK\x03\x04garbage-not-a-zip")
        self.assertEqual(r.status_code, 400)
        prov.assert_not_called()
        self.assertEqual(os.listdir(os.path.join(self.dir, "uploads")), [])

    def test_size_cap(self):
        with mock.patch.object(webapp, "UPLOAD_MAX", 1024):
            r, prov = self.post("owlet.apk", b"PK\x03\x04" + b"\0" * 4096)
        self.assertEqual(r.status_code, 413)
        prov.assert_not_called()

    def test_only_latest_upload_kept(self):
        self.post("a.apk", _zip_bytes())
        self.post("b.xapk", _zip_bytes())
        files = os.listdir(os.path.join(self.dir, "uploads"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith(".xapk"))


class AuthDefaultTests(WebappTestBase):
    def test_no_credentials_means_401(self):
        r = self.c.get("/api/config")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.c.get("/api/config", headers=_auth()).status_code, 200)

    def test_without_env_creds_a_password_is_generated_not_open(self):
        with mock.patch.object(webapp, "UI_USER", ""), mock.patch.object(webapp, "UI_PASS", ""):
            self.assertEqual(self.c.get("/api/config").status_code, 401)
            path = webapp.UI_SECRET_PATH
            self.assertTrue(os.path.exists(path))
            if os.name == "posix":
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            import json
            d = json.load(open(path))
            tok = base64.b64encode(f"{d['username']}:{d['password']}".encode()).decode()
            r = self.c.get("/api/config", headers={"Authorization": f"Basic {tok}"})
            self.assertEqual(r.status_code, 200)
            # the password itself never reaches the in-app log
            self.assertFalse(any(d["password"] in ln for ln in webapp.LOG))

    def test_clearing_saved_ui_creds_does_not_disable_auth(self):
        with mock.patch.object(webapp, "UI_USER", ""), mock.patch.object(webapp, "UI_PASS", ""):
            cs.save_config(dict(cs.load_config(), ui_user="", ui_pass=""), regenerate=False)
            self.assertEqual(self.c.get("/api/cameras").status_code, 401)

    def _basic(self, u, p):
        tok = base64.b64encode(f"{u}:{p}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def test_password_only_env_pairs_with_admin(self):
        with mock.patch.object(webapp, "UI_USER", ""), \
                mock.patch.object(webapp, "UI_PASS", "env-only-pass"):
            self.assertEqual(webapp._ui_creds(), ("admin", "env-only-pass"))
            r = self.c.get("/api/config", headers=self._basic("admin", "env-only-pass"))
            self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.exists(webapp.UI_SECRET_PATH))

    def test_user_only_env_pairs_with_generated_password(self):
        with mock.patch.object(webapp, "UI_USER", "panel"), \
                mock.patch.object(webapp, "UI_PASS", ""):
            user, pw = webapp._ui_creds()
            import json
            with open(webapp.UI_SECRET_PATH) as fh:
                stored = json.load(fh)["password"]
            self.assertEqual((user, pw), ("panel", stored))
            r = self.c.get("/api/config", headers=self._basic("panel", stored))
            self.assertEqual(r.status_code, 200)
            r = self.c.get("/api/config", headers=self._basic("admin", stored))
            self.assertEqual(r.status_code, 401)

    def test_user_only_env_pairs_with_saved_ui_password(self):
        cs.save_config(dict(cs.load_config(), ui_user="saved", ui_pass="saved-pass"),
                       regenerate=False)
        with mock.patch.object(webapp, "UI_USER", "panel"), \
                mock.patch.object(webapp, "UI_PASS", ""):
            self.assertEqual(webapp._ui_creds(), ("panel", "saved-pass"))

    def test_unwritable_secret_fails_closed(self):
        with mock.patch.object(webapp, "UI_USER", ""), mock.patch.object(webapp, "UI_PASS", ""),                 mock.patch.object(webapp, "_generated_ui_creds", lambda create=True: ("", "")):
            self.assertEqual(self.c.get("/api/config").status_code, 503)


class CameraMaskTests(WebappTestBase):
    def setUp(self):
        super().setUp()
        cfg = cs.load_config()
        cfg["cameras"] = [dict(cs.CAMERA_DEFAULTS, name="nursery", uid="UID123",
                               authkey="AbCd1234", av_password="pw")]
        cs.save_config(cfg)

    def test_authkey_is_masked(self):
        r = self.c.get("/api/cameras", headers=_auth())
        cam = r.get_json()["cameras"][0]
        self.assertEqual(cam["authkey"], "********")
        self.assertNotIn("AbCd1234", r.get_data(as_text=True))

    def test_masked_authkey_round_trip_keeps_real_key(self):
        r = self.c.post("/api/cameras/nursery", headers=_auth(),
                        json={"authkey": "********", "uid": "UID123"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(cs.find_camera(cs.load_config(), "nursery")["authkey"], "AbCd1234")


class _FakeClient:
    instances = []

    def __init__(self, *a, **k):
        self.connected_to = None
        self.on_connect = None
        _FakeClient.instances.append(self)

    def username_pw_set(self, u, p):
        self.creds = (u, p)

    def connect(self, host, port, keepalive):
        self.connected_to = (host, port)

    def loop(self, timeout=0.1):
        if self.on_connect:
            self.on_connect(self, None, {}, 0)

    def disconnect(self):
        pass


class MqttTestEndpointTests(WebappTestBase):
    def setUp(self):
        super().setUp()
        cfg = cs.load_config()
        cfg["mqtt"] = dict(cs.MQTT_DEFAULTS, enabled="1", host="10.0.0.5", port="1883",
                           user="ha", password="brokerpw")
        cs.save_config(cfg, regenerate=False)
        _FakeClient.instances = []
        fake = mock.MagicMock()
        fake.Client = _FakeClient
        del fake.CallbackAPIVersion
        paho = mock.MagicMock()
        paho.mqtt.client = fake
        self.mods = mock.patch.dict(sys.modules, {"paho": paho, "paho.mqtt": paho.mqtt,
                                                  "paho.mqtt.client": fake})
        self.mods.start()
        self.addCleanup(self.mods.stop)

    def test_foreign_host_is_refused_and_never_contacted(self):
        r = self.c.post("/api/mqtt/test", headers=_auth(), json={"host": "attacker.example"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(_FakeClient.instances, [])

    def test_uses_saved_broker(self):
        r = self.c.post("/api/mqtt/test", headers=_auth(), json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(_FakeClient.instances[0].connected_to, ("10.0.0.5", 1883))
        self.assertEqual(_FakeClient.instances[0].creds, ("ha", "brokerpw"))

    def test_same_host_is_fine(self):
        r = self.c.post("/api/mqtt/test", headers=_auth(), json={"host": "10.0.0.5"})
        self.assertEqual(r.status_code, 200)


class TalkGateTests(WebappTestBase):
    def setUp(self):
        super().setUp()
        cfg = cs.load_config()
        cfg["cameras"] = [dict(cs.CAMERA_DEFAULTS, name="nursery", uid="U")]
        cs.save_config(cfg)

    def test_talk_play_and_sound_upload_disabled_by_default(self):
        self.assertFalse(webapp.TALK_ENABLED)
        r1 = self.c.post("/api/talk/nursery", headers=_auth(),
                         data={"audio": (io.BytesIO(b"x"), "a.webm")},
                         content_type="multipart/form-data")
        r2 = self.c.post("/api/play/nursery", headers=_auth(), json={"file": "a.mp3"})
        r3 = self.c.post("/api/sounds", headers=_auth(),
                         data={"file": (io.BytesIO(b"x"), "a.mp3")},
                         content_type="multipart/form-data")
        for r in (r1, r2, r3):
            self.assertEqual(r.status_code, 403)
        self.assertFalse(os.path.isdir(os.path.join(self.dir, "sounds")))


class ConfigWriteFailureTests(WebappTestBase):
    """A config save that fails must come back as a JSON 500, never as ok."""

    def setUp(self):
        super().setUp()
        cfg = cs.load_config()
        cfg["cameras"] = [dict(cs.CAMERA_DEFAULTS, name="nursery", uid="U")]
        cs.save_config(cfg)
        p = mock.patch.object(cs, "_write_config",
                              side_effect=cs.ConfigWriteError("cannot write: read-only"))
        p.start()
        self.addCleanup(p.stop)

    def test_failing_saves_return_500(self):
        calls = (
            ("/api/config", {"region": "europe"}),
            ("/api/mqtt", {"host": "broker.example.com"}),
            ("/api/cameras", {"camera_dsn": "", "name": "hall"}),
            ("/api/cameras/nursery", {"uid": "U2"}),
            ("/api/volume/nursery", {"percent": 40}),
            ("/api/diagnose", {"email": "user@example.com"}),
        )
        for url, body in calls:
            with self.subTest(url=url), \
                    mock.patch.object(webapp, "_start_camera_diagnose", lambda n: None), \
                    mock.patch.object(webapp.threading, "Thread"):
                r = self.c.post(url, headers=_auth(), json=body)
                self.assertEqual(r.status_code, 500, r.get_data(as_text=True))
                self.assertFalse(r.get_json()["ok"])
                self.assertIn("could not save", r.get_json()["error"])
        r = self.c.delete("/api/cameras/nursery", headers=_auth())
        self.assertEqual(r.status_code, 500)
        self.assertEqual([c["name"] for c in cs.load_config()["cameras"]], ["nursery"])

    def test_unknown_camera_is_404_without_a_write(self):
        r = self.c.post("/api/cameras/nobody", headers=_auth(), json={"uid": "X"})
        self.assertEqual(r.status_code, 404)
        cs._write_config.assert_not_called()


class NonAsciiCredentialTests(WebappTestBase):
    """hmac.compare_digest() raises TypeError for str arguments outside ASCII,
    which turned a saved non-ASCII password into a 500 on every login."""
    N_USER, N_PASS = "usér", "pässא\U0001F476"

    def test_creds_match_handles_non_ascii_without_raising(self):
        self.assertTrue(webapp._creds_match(self.N_USER, self.N_PASS, self.N_USER, self.N_PASS))
        self.assertFalse(webapp._creds_match(self.N_USER, "wrong", self.N_USER, self.N_PASS))
        self.assertFalse(webapp._creds_match("", "", self.N_USER, self.N_PASS))
        self.assertTrue(webapp._creds_match(USER, PASS, USER, PASS))

    def test_non_ascii_saved_password_never_turns_logins_into_500(self):
        with mock.patch.object(webapp, "UI_USER", self.N_USER), \
             mock.patch.object(webapp, "UI_PASS", self.N_PASS):
            good = base64.b64encode(f"{self.N_USER}:{self.N_PASS}".encode("utf-8")).decode()
            r = self.c.get("/api/status", headers={"Authorization": f"Basic {good}"})
            # the framework's header decoding decides whether non-ASCII
            # credentials can match at all; what must never happen is a 500
            self.assertIn(r.status_code, (200, 401), r.status_code)
            bad = base64.b64encode(f"{self.N_USER}:nope".encode("utf-8")).decode()
            r = self.c.get("/api/status", headers={"Authorization": f"Basic {bad}"})
            self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
