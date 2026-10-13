"""Login-storm brake: account-wide backoff + cap, one login per connection
attempt (never after a session with video), record_success only at the first
video of a freshly refreshed key (and never shortening the interval),
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

    def test_success_resets_attempts_but_never_the_interval(self):
        for t in (0, 10_000, 20_000):
            self.assertTrue(lg.try_acquire(now=t)[0])
        pending = lg.status()["next_allowed"]
        self.assertEqual(pending, 20_000 + lg.delay_for(3))
        lg.record_success()
        self.assertEqual(lg.status()["attempts"], 0)
        self.assertEqual(lg.status()["next_allowed"], pending)
        self.assertFalse(lg.try_acquire(now=pending - 1)[0])
        self.assertTrue(lg.try_acquire(now=pending)[0])
        self.assertEqual(lg.status()["attempts"], 1)   # escalation starts over
        self.assertEqual(lg.status()["next_allowed"], pending + lg.BASE)

    def test_success_right_after_a_login_keeps_base_interval(self):
        self.assertTrue(lg.try_acquire(now=1000)[0])
        lg.record_success()
        self.assertFalse(lg.try_acquire(now=1000 + lg.BASE - 1)[0])
        self.assertTrue(lg.try_acquire(now=1000 + lg.BASE)[0])

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
                  mock.patch.object(tc, "AV_PASSWORD", tc.AV_PASSWORD),
                  mock.patch.object(tc, "_SUCCESS_RECORDED", [False]),
                  mock.patch.object(tc, "_SESSION_VIDEO", [False]),
                  mock.patch.object(tc, "_HAD_VIDEO", [False]),
                  mock.patch.object(tc, "_GUARD_ERR_LOGGED", [False]),
                  mock.patch.object(tc, "_KEY_REFRESHED", [False]),
                  mock.patch.object(tc, "_SESSION_FRESH_KEY", [False]),
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

    def test_one_login_per_call_then_guard_backoff_holds(self):
        self.assertEqual(tc.resolve_creds(), "NEWUID")
        self.assertEqual(FakeAPI.calls, 1)
        self.assertEqual(tc.resolve_creds(), "NEWUID")  # inside the backoff:
        self.assertEqual(FakeAPI.calls, 1)              # reuses the fetched key

    def test_long_lived_process_can_refresh_again_once_guard_permits(self):
        # record_success() no longer clears the pending interval, so let the
        # backoff elapse instead (time is moved, the guard values are real)
        self.assertEqual(tc.resolve_creds(), "NEWUID")
        later = tc.time.time() + lg.delay_for(1) + 1
        with mock.patch.object(tc.time, "time", lambda: later):
            tc.resolve_creds()
        self.assertEqual(FakeAPI.calls, 2)  # no per-process lifetime cap

    def test_only_a_changed_key_counts_as_refreshed(self):
        tc.resolve_creds()                       # OLDUID -> NEWUID
        self.assertTrue(tc._KEY_REFRESHED[0])
        tc._KEY_REFRESHED[0] = False
        later = tc.time.time() + lg.delay_for(1) + 1
        with mock.patch.object(tc.time, "time", lambda: later):
            self.assertEqual(tc.resolve_creds(), "NEWUID")   # same key again
        self.assertEqual(FakeAPI.calls, 2)
        self.assertFalse(tc._KEY_REFRESHED[0])

    def test_failed_or_refused_refresh_is_not_a_refreshed_key(self):
        lg.try_acquire()                         # guard in backoff
        tc.resolve_creds()
        self.assertEqual(FakeAPI.calls, 0)
        self.assertFalse(tc._KEY_REFRESHED[0])

    def test_refresh_kms_0_is_honoured(self):
        with mock.patch.dict(os.environ, {"OWLET_REFRESH_KMS": "0"}):
            self.assertEqual(tc.resolve_creds(), "OLDUID")
        self.assertEqual(FakeAPI.calls, 0)

    def test_guard_backoff_blocks_relaunched_process(self):
        tc.resolve_creds()
        with mock.patch.object(tc, "UID", "OLDUID"):               # "new process"
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


class FakeGuard:
    """Stands in for login_guard: scripted try_acquire verdicts, counted calls."""

    def __init__(self, permits=()):
        self.permits = list(permits)
        self.acquires = 0
        self.successes = 0

    def module(self):
        mod = types.ModuleType("login_guard")
        mod.try_acquire = self.try_acquire
        mod.record_success = self.record_success
        return mod

    def try_acquire(self, now=None):
        self.acquires += 1
        ok = self.permits.pop(0) if self.permits else True
        return ok, "" if ok else "login backoff, next allowed in 30s"

    def record_success(self):
        self.successes += 1


class ScriptedAPI:
    """camera_credentials() raises (transient KMS failure) or returns a key,
    in the scripted order."""
    script: list = []
    calls = 0
    times: list = []            # (patched) wall-clock time of each login
    clock = staticmethod(lambda: 0.0)

    def __init__(self, region, email, pw, log=None):
        pass

    def camera_credentials(self, dsn):
        ScriptedAPI.calls += 1
        ScriptedAPI.times.append(ScriptedAPI.clock())
        outcome = ScriptedAPI.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ReconnectLifecycleTests(TmpConfig):
    """main()'s reconnect loop with the real resolve_creds(); stream_once,
    the Owlet cloud client, login_guard and time are faked. One virtual clock
    drives time.time() (the real guard's backoff), time.monotonic() and
    time.sleep(); each session lasts `session_secs` of it."""

    session_secs = 20

    def setUp(self):
        super().setUp()
        self.now = [1_000_000.0]
        ScriptedAPI.calls = 0
        ScriptedAPI.script = []
        ScriptedAPI.times = []
        ScriptedAPI.clock = staticmethod(lambda: self.now[0])
        self.guard = FakeGuard()
        api_mod = types.ModuleType("owlet_api")
        api_mod.OwletAPI = ScriptedAPI
        env = {"OWLET_CAMERA_DSN": "OCD123", "OWLET_EMAIL": "user@example.com",
               "OWLET_PASSWORD": "pw", "OWLET_CAMERA_NAME": "nursery"}

        def sleep(s):
            self.now[0] += s
        self.uids = []
        self.logs = []
        for p in (mock.patch.dict(sys.modules, {"owlet_api": api_mod,
                                                "login_guard": self.guard.module()}),
                  mock.patch.dict(os.environ, env),
                  mock.patch.object(tc, "UID", "OLDUID"),
                  mock.patch.object(tc, "AUTHKEY", "OLDKEY12"),
                  mock.patch.object(tc, "AV_PASSWORD", tc.AV_PASSWORD),
                  mock.patch.object(tc, "AV_SECURITY_MODE", "2"),
                  mock.patch.object(tc, "RECONNECT_WAIT", 25),
                  mock.patch.object(tc, "_SUCCESS_RECORDED", [False]),
                  mock.patch.object(tc, "_SESSION_VIDEO", [False]),
                  mock.patch.object(tc, "_HAD_VIDEO", [False]),
                  mock.patch.object(tc, "_GUARD_ERR_LOGGED", [False]),
                  mock.patch.object(tc, "_KEY_REFRESHED", [False]),
                  mock.patch.object(tc, "_SESSION_FRESH_KEY", [False]),
                  mock.patch.object(tc, "log", self.logs.append),
                  mock.patch.object(tc.time, "sleep", sleep),
                  mock.patch.object(tc.time, "monotonic", lambda: self.now[0]),
                  mock.patch.object(tc.time, "time", lambda: self.now[0]),
                  mock.patch("signal.signal")):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("OWLET_REFRESH_KMS", None)
        os.environ.pop("OWLET_FORGET_ACCOUNT", None)
        cfg = cs.load_config()
        cfg.update(email="user@example.com", password="pw")
        cfg["cameras"] = [dict(cs.CAMERA_DEFAULTS, name="nursery", camera_dsn="OCD123",
                               uid="OLDUID", authkey="OLDKEY12")]
        cs.save_config(cfg)

    def run_main(self, sessions):
        """Each session is (video: bool, rc_or_exception)."""
        seq = iter(sessions)

        def fake_stream(uid, mode):
            self.uids.append(uid)
            try:
                video, result = next(seq)
            except StopIteration:
                raise SystemExit(0)
            self.now[0] += self.session_secs
            if video:
                for _ in range(3):        # the frame loop's guard: once per session
                    if not tc._SESSION_VIDEO[0]:
                        tc._on_first_frame()
            if isinstance(result, BaseException):
                raise result
            return result
        with mock.patch.object(tc, "stream_once", fake_stream):
            with self.assertRaises(SystemExit):
                tc.main()

    def test_stale_key_transient_failure_retries_after_guard_permits(self):
        ScriptedAPI.script = [RuntimeError("KMS HTTP 503"),
                              {"uid": "NEWUID", "authkey": "NEWKEY12", "av_password": "avpw"}]
        self.guard.permits = [True, False, True]
        # stale key: connect fails; login #1 fails transiently; next connect
        # fails again but the guard is in backoff; the one after, it permits.
        self.run_main([(False, 4), (False, 4), (False, 4)])
        self.assertEqual(ScriptedAPI.calls, 2)
        self.assertEqual(self.guard.acquires, 3)
        self.assertEqual(self.uids, ["OLDUID", "OLDUID", "OLDUID", "NEWUID"])
        self.assertEqual(cs.find_camera(cs.load_config(), "nursery")["uid"], "NEWUID")
        self.assertTrue(any("login backoff" in m for m in self.logs))

    def test_session_with_video_never_logs_in(self):
        self.run_main([(True, 4), (True, 5), (True, 6), (True, 0)])
        self.assertEqual(ScriptedAPI.calls, 0)
        self.assertEqual(self.guard.acquires, 0)
        self.assertTrue(any("previous session had video" in m for m in self.logs))

    @staticmethod
    def new_keys(n, start=0):
        return [{"uid": f"UID{i}", "authkey": f"KEY{i:05d}"} for i in range(start, start + n)]

    def test_success_is_recorded_once_per_session(self):
        # two refreshed-key sessions with video (the second ends in SIGTERM):
        # one record each, however many frames each forwarded. (Was two
        # saved-key sessions; saved-key video no longer records anything.)
        ScriptedAPI.script = self.new_keys(2)
        self.run_main([(False, 4), (True, 6), (False, 4), (True, SystemExit(0))])
        self.assertEqual(ScriptedAPI.calls, 2)
        self.assertEqual(self.guard.successes, 2)

    def test_saved_key_video_records_nothing(self):
        self.run_main([(True, 6), (True, 6), (True, SystemExit(0))])
        self.assertEqual(self.guard.successes, 0)

    def test_failed_refresh_then_saved_key_video_records_nothing(self):
        ScriptedAPI.script = [RuntimeError("KMS HTTP 503"),
                              {"uid": "OLDUID", "authkey": "OLDKEY12"}]  # same key
        self.run_main([(False, 4), (True, 6), (False, 4), (True, 6)])
        self.assertEqual(ScriptedAPI.calls, 2)
        self.assertEqual(self.guard.successes, 0)

    def test_fresh_key_counts_only_for_the_next_session(self):
        # the refreshed key's first session fails; the video comes a session
        # later (refresh refused by the guard): not this attempt's login
        ScriptedAPI.script = self.new_keys(1)
        self.guard.permits = [True, False]
        self.run_main([(False, 4), (False, 4), (True, 6)])
        self.assertEqual(self.uids, ["OLDUID", "UID0", "UID0", "UID0"])
        self.assertEqual(self.guard.successes, 0)

    def test_success_is_recorded_once_within_a_session(self):
        # adjusted: only a session on a freshly fetched key records at all
        for _ in range(3):
            tc._on_first_frame()
        self.assertEqual(self.guard.successes, 0)
        tc._SESSION_FRESH_KEY[0] = True
        for _ in range(3):
            tc._on_first_frame()
        self.assertEqual(self.guard.successes, 1)

    def _use_real_guard(self, max_attempts):
        # the real guard with its real intervals (BASE 60 s); only the cap is
        # lowered where a test needs it, and time comes from the virtual clock
        for obj, name, val in ((lg, "BASE", 60), (lg, "MAX_DELAY", 3600),
                               (lg, "MAX_ATTEMPTS", max_attempts)):
            p = mock.patch.object(obj, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(sys.modules, {"login_guard": lg})
        p.start()
        self.addCleanup(p.stop)

    def login_gaps(self):
        t = ScriptedAPI.times
        return [b - a for a, b in zip(t, t[1:])]

    def test_successful_rotations_never_exhaust_the_cap(self):
        # one long-lived process: video, then the key rotates (connect fails),
        # a refresh fetches the new key and video flows again; repeated more
        # often than the cap allows logins without video in between. A round
        # takes 20+5+20+25 = 70 s, past the real 60 s interval, so every
        # rotation may refresh.
        cap = 3
        self._use_real_guard(cap)
        rounds = cap + 3
        ScriptedAPI.script = self.new_keys(rounds)
        sessions = [(True, 6)]
        for _ in range(rounds):
            sessions += [(False, 4), (True, 6)]
        self.run_main(sessions)
        self.assertEqual(ScriptedAPI.calls, rounds)
        self.assertTrue(all(g >= lg.BASE for g in self.login_gaps()))
        self.assertFalse(any("cap reached" in m for m in self.logs))
        self.assertEqual(lg.status()["attempts"], 0)
        self.assertEqual(self.uids[-1], f"UID{rounds - 1}")

    def test_refreshes_without_video_still_count_towards_the_cap(self):
        # real intervals: 60 + 120 s must pass before the third login, so the
        # run has more failed connects (25 s each) than before
        cap = 3
        self._use_real_guard(cap)
        ScriptedAPI.script = self.new_keys(cap)
        self.run_main([(True, 6)] + [(False, 4)] * 15)
        self.assertEqual(ScriptedAPI.calls, cap)
        self.assertTrue(all(g >= lg.BASE for g in self.login_gaps()))
        self.assertTrue(any("cap reached" in m for m in self.logs))
        self.assertEqual(lg.status()["attempts"], cap)

    def test_saved_key_video_cannot_lift_the_brake(self):
        # the reported storm: transient connect failure -> login (failing,
        # or returning the key we already hold) -> video with the saved key,
        # every 10+5+10+25 = 50 s. Real BASE, real default cap of 5.
        self._use_real_guard(5)
        self.session_secs = 10
        ScriptedAPI.script = [RuntimeError("KMS HTTP 503"),
                              {"uid": "OLDUID", "authkey": "OLDKEY12"}] * 20
        sessions = [(True, 6)]
        for _ in range(30):                 # 1500 s, past 60+120+240+480 s
            sessions += [(False, 4), (True, 6)]
        self.run_main(sessions)
        gaps = self.login_gaps()
        self.assertTrue(gaps)
        for n, gap in enumerate(gaps, start=1):
            self.assertGreaterEqual(gap, lg.BASE)
            self.assertGreaterEqual(gap, lg.delay_for(n))   # escalation kept
        self.assertEqual(ScriptedAPI.calls, 5)              # cap still applies
        self.assertTrue(any("cap reached" in m for m in self.logs))
        self.assertEqual(lg.status()["attempts"], 5)
        self.assertEqual(set(self.uids), {"OLDUID"})

    def test_refreshed_key_video_resets_attempts_but_holds_the_interval(self):
        self._use_real_guard(5)
        lg.try_acquire(now=0)               # two earlier logins, long ago
        lg.try_acquire(now=10_000)
        ScriptedAPI.script = self.new_keys(1)
        self.run_main([(True, 6), (False, 4), (True, 6)])
        self.assertEqual(ScriptedAPI.calls, 1)
        login = ScriptedAPI.times[0]
        st = lg.status()
        self.assertEqual(st["attempts"], 0)                       # escalation reset
        self.assertEqual(st["next_allowed"], login + lg.delay_for(3))  # unchanged
        self.assertGreaterEqual(st["next_allowed"], login + lg.BASE)
        self.assertFalse(lg.try_acquire(now=login + lg.BASE - 1)[0])

    def test_other_cameras_saved_key_video_keeps_an_exhausted_cap(self):
        # camera A has used up the account-wide budget; camera B's process
        # streams on its saved key. B's video says nothing about A's logins.
        self._use_real_guard(5)
        for i in range(5):
            self.assertTrue(lg.try_acquire(now=i * 10_000)[0])
        self.run_main([(True, 6), (True, 6), (True, SystemExit(0))])
        self.assertEqual(len(self.uids), 3)
        self.assertEqual(ScriptedAPI.calls, 0)
        self.assertEqual(lg.status()["attempts"], 5)
        ok, why = lg.try_acquire(now=10 ** 9)
        self.assertFalse(ok)
        self.assertIn("cap", why)

    def test_video_then_broken_pipe_records_success(self):
        # adjusted: start without a saved key, so the session runs on the key
        # the start-up refresh fetched (saved-key video records nothing)
        ScriptedAPI.script = self.new_keys(1)
        with mock.patch.object(tc, "UID", ""), \
                mock.patch.object(tc, "stream_once",
                                  lambda uid, mode: (tc._on_first_frame(),
                                                     (_ for _ in ()).throw(BrokenPipeError()))):
            tc.main()                      # returns: exits for a clean go2rtc restart
        self.assertEqual(ScriptedAPI.calls, 1)
        self.assertEqual(self.guard.successes, 1)

    def test_record_success_failure_is_swallowed_and_logged_once(self):
        def boom():
            raise RuntimeError("disk gone")
        self.guard.record_success = boom
        ScriptedAPI.script = self.new_keys(2)
        with mock.patch.dict(sys.modules, {"login_guard": self.guard.module()}):
            self.run_main([(False, 4), (True, 6), (False, 4), (True, 6)])
        self.assertEqual(ScriptedAPI.calls, 2)
        self.assertEqual(sum("could not reset the login guard" in m for m in self.logs), 1)

    def test_refresh_kms_0_never_logs_in_across_reconnects(self):
        with mock.patch.dict(os.environ, {"OWLET_REFRESH_KMS": "0"}):
            self.run_main([(False, 4), (False, 5), (False, 4), (False, 5), (False, 4)])
        self.assertEqual(ScriptedAPI.calls, 0)
        self.assertEqual(self.guard.acquires, 0)
        self.assertEqual(set(self.uids), {"OLDUID"})
        self.assertTrue(any("refresh disabled" in m for m in self.logs))


if __name__ == "__main__":
    unittest.main()
