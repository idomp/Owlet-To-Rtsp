"""Config writers share one lock: config_transaction() read-modify-write,
unique temp names, and persistence failures that surface instead of being
swallowed. Stdlib-only; nothing touches the network.

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import os
import re
import sys
import tempfile
import threading
import time
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
        cfg = cs.load_config()
        cfg["cameras"] = [dict(cs.CAMERA_DEFAULTS, name="nursery", camera_dsn="OCD123",
                               uid="OLDUID", authkey="OLDKEY12")]
        self.assertTrue(cs.save_config(cfg))

    def leftovers(self):
        return [f for f in os.listdir(self.dir) if ".tmp" in f]


def _add_cameras(prefix, n):
    for i in range(n):
        with cs.config_transaction(regenerate=False) as cfg:
            names = [c["name"] for c in cfg["cameras"]]
            time.sleep(0.001)             # widen the read-modify-write window
            cfg["cameras"].append(dict(cs.CAMERA_DEFAULTS, name=f"{prefix}-{i}"))
            assert f"{prefix}-{i}" not in names


class TransactionTests(TmpConfig):
    def test_two_threads_never_lose_an_update(self):
        n = 25
        start = threading.Barrier(2)

        def worker(prefix):
            start.wait()
            _add_cameras(prefix, n)
        threads = [threading.Thread(target=worker, args=(p,)) for p in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        names = {c["name"] for c in cs.load_config()["cameras"]}
        self.assertEqual(len(names), 2 * n + 1)
        for p in ("a", "b"):
            for i in range(n):
                self.assertIn(f"{p}-{i}", names)
        self.assertEqual(self.leftovers(), [])

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork()")
    def test_two_processes_never_lose_an_update(self):
        n = 15
        pids = []
        for p in ("x", "y"):
            pid = os.fork()
            if pid == 0:                  # child
                code = 0
                try:
                    _add_cameras(p, n)
                except BaseException:  # noqa: BLE001
                    code = 1
                os._exit(code)
            pids.append(pid)
        for pid in pids:
            _, status = os.waitpid(pid, 0)
            self.assertEqual(status, 0)
        names = {c["name"] for c in cs.load_config()["cameras"]}
        self.assertEqual(len(names), 2 * n + 1)

    def test_key_refresh_waits_for_a_panel_save_and_both_survive(self):
        done = threading.Event()

        def refresh():
            cs.persist_camera_key("nursery", {"uid": "NEWUID", "authkey": "NEWKEY12"})
            done.set()
        with cs.config_transaction(regenerate=False) as cfg:
            t = threading.Thread(target=refresh)
            t.start()
            self.assertFalse(done.wait(0.3))   # blocked on the shared lock
            cfg["mqtt"]["host"] = "broker.example.com"
        t.join(10)
        self.assertTrue(done.is_set())
        cfg = cs.load_config()
        self.assertEqual(cs.find_camera(cfg, "nursery")["uid"], "NEWUID")
        self.assertEqual(cfg["mqtt"]["host"], "broker.example.com")

    def test_nested_transactions_keep_both_levels_changes(self):
        with cs.config_transaction(regenerate=False) as cfg:
            cfg["mqtt"]["prefix"] = "ha"
            with cs.config_transaction(regenerate=False) as inner:
                self.assertIs(inner, cfg)
                inner["mqtt"]["host"] = "broker.example.com"
            self.assertTrue(cs.persist_camera_key(
                "nursery", {"uid": "NEWUID", "authkey": "NEWKEY12"}))
            cfg["email"] = "user@example.com"      # outer keeps modifying after
        saved = cs.load_config()
        self.assertEqual(saved["mqtt"]["prefix"], "ha")
        self.assertEqual(saved["mqtt"]["host"], "broker.example.com")
        self.assertEqual(cs.find_camera(saved, "nursery")["uid"], "NEWUID")
        self.assertEqual(saved["email"], "user@example.com")
        # persist_camera_key asked for regeneration: the outer commit honours it
        with open(os.path.join(cs.CAM_DIR, "nursery.env")) as fh:
            self.assertIn("OWLET_UID='NEWUID'", fh.read())

    def test_nested_transaction_writes_nothing_before_the_outer_commit(self):
        before = cs.load_config()["email"]
        with cs.config_transaction(regenerate=False):
            with cs.config_transaction(regenerate=False) as inner:
                inner["email"] = "user@example.com"
            self.assertEqual(cs.load_config()["email"], before)
        self.assertEqual(cs.load_config()["email"], "user@example.com")

    def test_failed_inner_transaction_is_rolled_back_outer_kept(self):
        with cs.config_transaction(regenerate=False) as cfg:
            cfg["mqtt"]["prefix"] = "ha"
            try:
                with cs.config_transaction(regenerate=False) as inner:
                    inner["cameras"] = []
                    raise ValueError("abort inner")
            except ValueError:
                pass
            self.assertEqual(len(cfg["cameras"]), 1)
        saved = cs.load_config()
        self.assertEqual(saved["mqtt"]["prefix"], "ha")
        self.assertEqual([c["name"] for c in saved["cameras"]], ["nursery"])

    def test_save_config_inside_a_transaction_raises(self):
        with cs.config_transaction(regenerate=False):
            with self.assertRaises(RuntimeError):
                cs.save_config(cs.load_config(), regenerate=False)
        self.assertTrue(cs.save_config(cs.load_config(), regenerate=False))

    def test_body_exception_writes_nothing(self):
        before = open(cs.CONFIG_PATH).read()
        with self.assertRaises(RuntimeError):
            with cs.config_transaction() as cfg:
                cfg["cameras"] = []
                raise RuntimeError("abort")
        self.assertEqual(open(cs.CONFIG_PATH).read(), before)

    def test_unchanged_config_is_not_rewritten(self):
        with mock.patch.object(cs, "_write_config") as w:
            with cs.config_transaction() as cfg:
                cs.find_camera(cfg, "nobody")
        w.assert_not_called()

    def test_resubmitting_after_failed_render_regenerates(self):
        env_path = os.path.join(cs.CAM_DIR, "nursery.env")

        def submit():
            with cs.config_transaction() as cfg:
                cfg["email"] = "user@example.com"
        with mock.patch.object(cs, "generate", side_effect=OSError(13, "Permission denied")):
            with self.assertRaises(cs.ConfigWriteError):
                submit()
        self.assertEqual(cs.load_config()["email"], "user@example.com")  # JSON committed
        with open(env_path) as fh:
            self.assertNotIn("user@example.com", fh.read())           # files stale
        submit()                                   # same settings, cause fixed
        with open(env_path) as fh:
            self.assertIn("OWLET_EMAIL='user@example.com'", fh.read())

    def test_unchanged_without_regenerate_renders_nothing(self):
        with mock.patch.object(cs, "generate") as g, \
                mock.patch.object(cs, "_atomic_write") as w:
            with cs.config_transaction(regenerate=False) as cfg:
                cs.find_camera(cfg, "nobody")
        g.assert_not_called()
        w.assert_not_called()

    def test_write_failure_propagates(self):
        with mock.patch.object(cs, "_atomic_write", side_effect=OSError(28, "No space")):
            with self.assertRaises(cs.ConfigWriteError):
                with cs.config_transaction() as cfg:
                    cfg["email"] = "user@example.com"
            with self.assertRaises(cs.ConfigWriteError):
                cs.persist_camera_key("nursery", {"uid": "NEWUID", "authkey": "NEWKEY12"})
            self.assertFalse(cs.save_config(cs.load_config()))


class AtomicWriteTests(TmpConfig):
    def test_temp_names_are_unique_and_cleaned_up(self):
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(src)
            return real_replace(src, dst)
        path = os.path.join(self.dir, "x.json")
        with mock.patch("os.replace", spy):
            for _ in range(3):
                cs._atomic_write(path, "{}")
        self.assertEqual(len(set(seen)), 3)
        pat = re.compile(re.escape(path) + r"\.tmp-%d-[0-9a-f]+$" % os.getpid())
        for s in seen:
            self.assertRegex(s, pat)
        self.assertEqual(self.leftovers(), [])

    def test_failed_write_leaves_no_temp_and_keeps_original(self):
        path = os.path.join(self.dir, "x.json")
        cs._atomic_write(path, "old")
        with mock.patch("os.fsync", side_effect=OSError(28, "No space")):
            with self.assertRaises(OSError):
                cs._atomic_write(path, "new")
        self.assertEqual(open(path).read(), "old")
        self.assertEqual(self.leftovers(), [])


class RenderStreamsLockTests(TmpConfig):
    def test_render_waits_for_the_lock_and_renders_the_saved_config(self):
        import render_streams
        env_path = os.path.join(cs.CAM_DIR, "nursery.env")
        result = []
        done = threading.Event()

        def render():
            with mock.patch.object(sys, "argv", ["render_streams.py"]), \
                    mock.patch.object(render_streams.sys, "stderr", mock.Mock()):
                result.append(render_streams.main())
            done.set()
        with cs.config_lock():
            t = threading.Thread(target=render)
            t.start()
            self.assertFalse(done.wait(0.3))   # blocked on the shared lock
            with cs.config_transaction(regenerate=False) as cfg:
                cfg["email"] = "user@example.com"
            with open(env_path) as fh:
                self.assertNotIn("user@example.com", fh.read())
        t.join(10)
        self.assertEqual(result, [0])
        with open(env_path) as fh:             # rendered from the saved config
            self.assertIn("OWLET_EMAIL='user@example.com'", fh.read())

    def test_render_fails_cleanly_when_the_lock_is_unavailable(self):
        import render_streams
        with mock.patch.object(sys, "argv", ["render_streams.py"]), \
                mock.patch.object(cs, "_config_file_lock",
                                  side_effect=cs.ConfigWriteError("read-only")), \
                mock.patch.object(sys, "stderr"):
            self.assertEqual(render_streams.main(), 1)


class StreamPersistFailureTests(TmpConfig):
    def test_refreshed_key_kept_in_memory_when_persist_fails(self):
        class API:
            def __init__(self, *a, **k):
                pass

            def camera_credentials(self, dsn):
                return {"uid": "NEWUID", "authkey": "NEWKEY12"}
        mod = types.ModuleType("owlet_api")
        mod.OwletAPI = API
        logs = []
        env = {"OWLET_CAMERA_DSN": "OCD123", "OWLET_EMAIL": "user@example.com",
               "OWLET_PASSWORD": "pw", "OWLET_CAMERA_NAME": "nursery"}
        with mock.patch.dict(sys.modules, {"owlet_api": mod}), \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(tc, "UID", "OLDUID"), \
                mock.patch.object(tc, "AUTHKEY", "OLDKEY12"), \
                mock.patch.object(tc, "log", logs.append), \
                mock.patch.object(cs, "persist_camera_key",
                                  side_effect=cs.ConfigWriteError("read-only")):
            os.environ.pop("OWLET_REFRESH_KMS", None)
            self.assertEqual(tc.resolve_creds(), "NEWUID")
            self.assertEqual(tc.AUTHKEY, "NEWKEY12")
        self.assertTrue(any(m.startswith("ERROR: could not persist") for m in logs))
        self.assertEqual(cs.find_camera(cs.load_config(), "nursery")["uid"], "OLDUID")


if __name__ == "__main__":
    unittest.main()
