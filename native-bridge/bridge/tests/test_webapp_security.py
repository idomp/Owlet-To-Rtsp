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


if __name__ == "__main__":
    unittest.main()
