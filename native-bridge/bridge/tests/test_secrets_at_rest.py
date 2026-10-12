"""Credentials at rest + log hygiene. The cloud client test fakes the HTTP
session (no network) and is skipped without `requests` (stdlib-only CI).

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config_store as cs  # noqa: E402

try:
    import requests  # noqa: F401
    import owlet_api  # noqa: E402
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..", "..")


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@unittest.skipUnless(os.name == "posix", "POSIX permissions")
class FileModeTests(unittest.TestCase):
    def test_config_env_and_generated_files_are_private(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.umask(0o022)   # a permissive umask must not matter
            self.addCleanup(os.umask, old)
            paths = dict(CONFIG_PATH=os.path.join(d, "owlet.yaml"), CONFIG_DIR=d,
                         CAM_DIR=os.path.join(d, "cameras"),
                         ENV_PATH=os.path.join(d, "owlet.env"),
                         GEN_PATH=os.path.join(d, "go2rtc.gen.yaml"),
                         SECRETS_DIR=os.path.join(d, "secrets"),
                         G2_SECRET_PATH=os.path.join(d, "secrets", "go2rtc.json"))
            with mock.patch.multiple(cs, **paths):
                cfg = cs.load_config()
                cfg.update(email="a@b.c", password="hunter2")
                cfg["cameras"] = [dict(cs.CAMERA_DEFAULTS, name="nursery", uid="U",
                                       authkey="K")]
                self.assertTrue(cs.save_config(cfg))
                for p in (cs.CONFIG_PATH, cs.ENV_PATH, cs.GEN_PATH,
                          os.path.join(cs.CAM_DIR, "nursery.env"), cs.G2_SECRET_PATH):
                    self.assertEqual(_mode(p), 0o600, p)
                self.assertEqual(_mode(cs.CAM_DIR), 0o700)
                self.assertEqual(_mode(cs.SECRETS_DIR), 0o700)


class NoWorldWritableAdviceTests(unittest.TestCase):
    def test_no_chmod_777_command_anywhere(self):
        files = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, "BETA.md"),
                 os.path.join(HERE, "..", "start-bionic.sh"),
                 os.path.join(HERE, "..", "webapp.py"),
                 os.path.join(HERE, "..", "static", "app.js")]
        for f in files:
            with open(f, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIsNone(re.search(r"chmod\s+(-R\s+)?777\s+/", text), f)

    def test_startup_sets_private_umask(self):
        with open(os.path.join(HERE, "..", "start-bionic.sh"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("umask 077", text)
        self.assertIn("chmod -R go-rwx /config", text)


class _Resp:
    def __init__(self, code, data):
        self.status_code = code
        self._d = data
        self.text = str(data)

    def json(self):
        return self._d


@unittest.skipUnless(HAVE_REQUESTS, "requests not installed")
class LogHygieneTests(unittest.TestCase):
    def test_login_and_kms_logs_carry_no_secret(self):
        lines = []
        api = owlet_api.OwletAPI("world", "parent@example.com", "S3cretPassw0rd!",
                                 log=lines.append)
        idtok = "eyJhbGciOiJSUzI1NiJ9.VERYSECRETTOKEN.signature"
        sess = mock.MagicMock()
        sess.post.return_value = _Resp(200, {"idToken": idtok})
        sess.get.return_value = _Resp(200, {"tutkid": "ABCDEFGHJKLMNPQRST12",
                                            "authKey": "Ak9xZ2Qw", "password": "avpass99"})
        api.s = sess
        creds = api.camera_credentials("OCD123")
        self.assertEqual(creds["authkey"], "Ak9xZ2Qw")
        blob = "\n".join(lines)
        for secret in ("S3cretPassw0rd!", idtok, idtok[:4] + "…", "Ak9xZ2Qw", "avpass99",
                       "parent@example.com", "ABCDEFGHJKLMNPQRST12"):
            self.assertNotIn(secret, blob)

    def test_mask_reveals_no_characters(self):
        m = owlet_api._mask("abcdefghijklmnop")
        self.assertNotIn("abcd", m)
        self.assertNotIn("nop", m)

    def test_redact_props(self):
        r = owlet_api.redact_props({"APP_ACTIVE": 1, "PSK_KEY": "zzzz9999",
                                    "auth_token": "t0k3n", "TEMP": 21})
        self.assertEqual(r["APP_ACTIVE"], 1)
        self.assertEqual(r["TEMP"], 21)
        self.assertNotIn("zzzz9999", str(r))
        self.assertNotIn("t0k3n", str(r))


if __name__ == "__main__":
    unittest.main()
