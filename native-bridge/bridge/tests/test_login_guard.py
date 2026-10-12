"""Login-storm brake: account-wide backoff + cap, one login per stream start,
OWLET_REFRESH_KMS=0, key persistence and OWLET_FORGET_ACCOUNT. Stdlib-only;
the Owlet cloud client is faked, nothing touches the network.

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import itertools
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config_store as cs  # noqa: E402
import login_guard as lg  # noqa: E402
import tutk_client as tc  # noqa: E402


class TmpConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        for obj, name, val in (
                (cs, "CONFIG_PATH", os.path.join(d, "owlet.yaml")),
                (cs, "CONFIG_DIR", d),
                (cs, "CAM_DIR", os.path.join(d, "cameras")),
                (cs, "ENV_PATH", os.path.join(d, "owlet.env")),
                (cs, "GEN_PATH", os.path.join(d, "go2rtc.gen.yaml")),
                (cs, "SECRETS_DIR", os.path.join(d, "secrets")),
                (cs, "G2_SECRET_PATH", os.path.join(d, "secrets", "go2rtc.json")),
                (lg, "STATE_PATH", os.path.join(d, "secrets", "login-guard.json"))):
            p = mock.patch.object(obj, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.dir = d


class GuardTests(TmpConfig):
    def setUp(self):
        super().setUp()
        for name, val in (("BASE", 60), ("MAX_DELAY", 3600), ("MAX_ATTEMPTS", 5)):
            p = mock.patch.object(lg, name, val)
            p.start()
            self.addCleanup(p.stop)

    def test_backoff_doubles(self):
        self.assertEqual([lg.delay_for(n) for n in range(1, 8)],
                         [60, 120, 240, 480, 960, 1920, 3600])

    def test_second_login_inside_backoff_is_refused(self):
        self.assertTrue(lg.try_acquire(now=1000)[0])
        ok, why = lg.try_acquire(now=1030)
        self.assertFalse(ok)
        self.assertIn("backoff", why)
        self.assertTrue(lg.try_acquire(now=1061)[0])
        self.assertFalse(lg.try_acquire(now=1061 + 119)[0])   # now 120 s

    def test_hard_cap_then_no_more_logins_ever(self):
        t = 0
        for _ in range(5):
            ok, _why = lg.try_acquire(now=t)
            self.assertTrue(ok)
            t += 10_000
        ok, why = lg.try_acquire(now=t + 10 ** 9)
        self.assertFalse(ok)
        self.assertIn("cap", why)

    def test_success_rearms(self):
        for t in range(5):
            lg.try_acquire(now=t * 10_000)
        lg.record_success()
        self.assertTrue(lg.try_acquire(now=10 ** 6)[0])

    def test_state_is_shared_through_the_file(self):
        lg.try_acquire(now=1000)
        with open(lg.STATE_PATH) as fh:
            st = json.load(fh)
        self.assertEqual(st["attempts"], 1)
        if os.name == "posix":
            self.assertEqual(os.stat(lg.STATE_PATH).st_mode & 0o777, 0o600)


class FakeAPI:
    calls = 0

    def __init__(self, region, email, pw, log=None):
        pass

    def camera_credentials(self, dsn):
        FakeAPI.calls += 1
        return {"uid": "NEWUID", "authkey": "NEWKEY12", "av_password": "avpw"}


class ResolveCredsTests(TmpConfig):
    def setUp(self):
        super().setUp()
        FakeAPI.calls = 0
        mod = types.ModuleType("owlet_api")
        mod.OwletAPI = FakeAPI
        env = {"OWLET_CAMERA_DSN": "OCD123", "OWLET_EMAIL": "a@b.c",
               "OWLET_PASSWORD": "pw", "OWLET_CAMERA_NAME": "nursery"}
        for p in (mock.patch.dict(sys.modules, {"owlet_api": mod}),
                  mock.patch.dict(os.environ, env),
                  mock.patch.object(tc, "UID", "OLDUID"),
                  mock.patch.object(tc, "AUTHKEY", "OLDKEY12"),
                  mock.patch.object(tc, "_LOGINS_THIS_PROCESS", [0]),
                  mock.patch.object(tc, "log", lambda *a: None)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("OWLET_REFRESH_KMS", None)
        os.environ.pop("OWLET_FORGET_ACCOUNT", None)
        cfg = cs.load_config()
        cfg.update(email="a@b.c", password="pw")
        cfg["cameras"] = [dict(cs.CAMERA_DEFAULTS, name="nursery", camera_dsn="OCD123",
                               uid="OLDUID", authkey="OLDKEY12")]
        cs.save_config(cfg)

    def test_start_with_saved_key_does_not_log_in(self):
        self.assertEqual(tc.resolve_creds(refresh=not tc.UID), "OLDUID")
        self.assertEqual(FakeAPI.calls, 0)

    def test_at_most_one_login_per_stream_start(self):
        self.assertEqual(tc.resolve_creds(), "NEWUID")
        lg.record_success()               # even with the guard re-armed …
        self.assertEqual(tc.resolve_creds(), "NEWUID")  # reuses the fetched key
        self.assertEqual(FakeAPI.calls, 1)  # … the process cap holds

    def test_refresh_kms_0_is_honoured(self):
        with mock.patch.dict(os.environ, {"OWLET_REFRESH_KMS": "0"}):
            self.assertEqual(tc.resolve_creds(), "OLDUID")
        self.assertEqual(FakeAPI.calls, 0)

    def test_guard_backoff_blocks_relaunched_process(self):
        tc.resolve_creds()
        with mock.patch.object(tc, "_LOGINS_THIS_PROCESS", [0]), \
                mock.patch.object(tc, "UID", "OLDUID"):            # "new process"
            self.assertEqual(tc.resolve_creds(), "OLDUID")
        self.assertEqual(FakeAPI.calls, 1)

    def test_fetched_key_is_persisted(self):
        tc.resolve_creds()
        cam = cs.find_camera(cs.load_config(), "nursery")
        self.assertEqual((cam["uid"], cam["authkey"], cam["av_password"]),
                         ("NEWUID", "NEWKEY12", "avpw"))
        with open(os.path.join(cs.CAM_DIR, "nursery.env")) as fh:
            self.assertIn("OWLET_UID='NEWUID'", fh.read())

    def test_forget_account_blanks_credentials_after_fetch(self):
        with mock.patch.dict(os.environ, {"OWLET_FORGET_ACCOUNT": "1"}):
            tc.resolve_creds()
            cfg = cs.load_config()
            self.assertEqual((cfg["email"], cfg["password"]), ("", ""))
            self.assertNotIn("OWLET_PASSWORD", os.environ)
        with open(os.path.join(cs.CAM_DIR, "nursery.env")) as fh:
            self.assertIn("OWLET_PASSWORD=''", fh.read())

    def test_forget_waits_until_every_camera_has_a_key(self):
        cfg = cs.load_config()
        cfg["cameras"].append(dict(cs.CAMERA_DEFAULTS, name="hall", camera_dsn="OCD9"))
        self.assertFalse(cs.forget_account_if_keyed(dict(cfg, forget_account="1")))
        cfg["cameras"][1].update(uid="U2", authkey="K2")
        c2 = dict(cfg, forget_account="1")
        self.assertTrue(cs.forget_account_if_keyed(c2))
        self.assertEqual(c2["password"], "")


class MainLoopLoginTests(unittest.TestCase):
    def test_no_video_session_never_triggers_a_login(self):
        calls = []
        seq = iter([6, 6, 6, 4])

        def fake_stream(uid, mode):
            try:
                return next(seq)
            except StopIteration:
                raise SystemExit
        clock = itertools.count(0, 100)
        with mock.patch.object(tc, "UID", "SAVED"), \
                mock.patch.object(tc, "resolve_creds",
                                  lambda refresh=True: calls.append(refresh) or "SAVED"), \
                mock.patch.object(tc, "stream_once", fake_stream), \
                mock.patch.object(tc, "log", lambda *a: None), \
                mock.patch.object(tc.time, "sleep", lambda s: None), \
                mock.patch.object(tc.time, "monotonic", lambda: next(clock)), \
                mock.patch("signal.signal"):
            with self.assertRaises(SystemExit):
                tc.main()
        # start: no refresh (key saved); only the rc=4 connect failure may refresh
        self.assertEqual(calls, [False, True])


if __name__ == "__main__":
    unittest.main()
