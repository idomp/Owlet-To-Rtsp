"""Stream process supervision: stopping a go2rtc exec source must take the
TUTK client and ffmpeg down with it, and the watchdog is opt-in.

The integration test runs the REAL generated wrapper under bash with stub
`python3`/`ffmpeg` executables (Linux only; no camera, no go2rtc).

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config_store as cs  # noqa: E402
import watchdog as wd  # noqa: E402

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


class ExecOptionTests(unittest.TestCase):
    def test_video_exec_asks_go2rtc_for_sigterm_then_delayed_sigkill(self):
        src = cs._exec_source("owlet")
        cmd, _, opts = src.partition("#")
        self.assertEqual("#" + opts, "#killsignal=15#killtimeout=12")
        self.assertTrue(cmd.endswith("'"), "no space before '#' (YAML comment)")

    def test_no_colon_space(self):
        self.assertNotIn(": ", cs._exec_source("owlet"))

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_options_survive_yaml(self):
        d = yaml.safe_load(cs.render_go2rtc([{"name": "owlet"}], candidate="",
                                            creds=("u", "p")))
        self.assertTrue(d["streams"]["owlet"][0].endswith("#killsignal=15#killtimeout=12"))


def _children(pid):
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as fh:
                st = fh.read().rsplit(")", 1)[1].split()
            if int(st[1]) == pid and st[0] != "Z":
                out.append(int(d))
        except (OSError, IndexError, ValueError):
            continue
    return out


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash")
                     and shutil.which("mkfifo"), "needs Linux bash")
class WrapperKillsChildrenTests(unittest.TestCase):
    def run_wrapper(self, py_stub):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        for name, body in (("python3", py_stub), ("ffmpeg", "exec cat >/dev/null\n")):
            p = os.path.join(bindir, name)
            with open(p, "w") as fh:
                fh.write("#!/bin/sh\n" + body)
            os.chmod(p, 0o755)
        src = cs._exec_source("cam")
        inner = src[len("exec:bash -c '"):src.index("'#")]
        inner = inner.replace("/config/", tmp + "/").replace("{output}", "/dev/null")
        env = dict(os.environ, PATH=bindir + ":" + os.environ.get("PATH", ""), TMPDIR=tmp)
        bash = subprocess.Popen(["bash", "-c", inner], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: bash.poll() is None and bash.kill())
        deadline = time.time() + 5
        kids = []
        while time.time() < deadline and len(kids) < 2:
            time.sleep(0.1)
            kids = _children(bash.pid)
        self.assertEqual(len(kids), 2, "stub tutk_client + ffmpeg should be running")
        t0 = time.time()
        bash.send_signal(signal.SIGTERM)          # exactly what go2rtc does
        bash.wait(timeout=12)                     # go2rtc's killtimeout
        took = time.time() - t0
        time.sleep(0.2)
        left = [k for k in kids if _alive(k)]
        for k in left:
            os.kill(k, signal.SIGKILL)
        self.assertEqual(left, [], "children survived the wrapper")
        self.assertFalse(os.path.exists(os.path.join(tmp, "owlet-talk-cam")),
                         "EXIT trap should remove the talk FIFO")
        return took

    def test_sigterm_to_wrapper_stops_tutk_client_and_ffmpeg(self):
        took = self.run_wrapper("exec sleep 1003\n")
        self.assertLess(took, 3)

    def test_child_ignoring_sigterm_is_killed_within_the_timeout(self):
        # a tutk_client blocked in native code never runs its SIGTERM handler
        took = self.run_wrapper("trap '' TERM\nexec sleep 1004\n")
        self.assertLess(took, 11)


class StopAllProducersTests(unittest.TestCase):
    def test_terms_wrappers_and_children_then_kills_survivors(self):
        procs = [
            (100, 1, "bash -c set -a; ... python3 /app/tutk_client.py 2>>/config/tutk-owlet.log | ffmpeg"),
            (101, 100, "python3 /app/tutk_client.py"),
            (102, 100, "ffmpeg -f h264 -i - -f rtsp rtsp://127.0.0.1:8554/x"),
            (200, 1, "ffmpeg -f aac -i /tmp/owlet-audio-owlet"),     # not ours
        ]
        sent = []
        with mock.patch.object(wd, "_procs", lambda: procs), \
                mock.patch.object(wd, "_kill", lambda pids, sig: sent.append((sorted(pids), sig))), \
                mock.patch.object(wd, "_gone", lambda pids, t: False):
            got = wd.stop_all_producers(names=["owlet"], timeout=0)
        self.assertEqual(got, [100, 101, 102])
        # SIGTERM to the wrapper ONLY (it forwards one to tutk_client + ffmpeg);
        # the children are only SIGKILLed if they outlive the timeout
        self.assertEqual(sent, [([100], signal.SIGTERM),
                                ([100, 101, 102], signal.SIGKILL)])

    def test_children_never_get_a_second_sigterm(self):
        procs = [
            (100, 1, self._wrapper_cmd("a")),
            (101, 100, "python3 /app/tutk_client.py"),
            (102, 100, "ffmpeg -f h264 -i - -f rtsp rtsp://127.0.0.1:8554/a"),
            (110, 1, self._wrapper_cmd("b")),
            (111, 110, "python3 /app/tutk_client.py"),
        ]
        got, sent = self._stop(procs, names=["a", "b"])
        self.assertEqual(got, [100, 101, 102, 110, 111])
        self.assertEqual(sent, [([100, 110], signal.SIGTERM)])

    def test_orphaned_tutk_client_without_wrapper_is_signalled_directly(self):
        procs = [(800, 1, "python3 /app/tutk_client.py")]
        got, sent = self._stop(procs, names=[])
        self.assertEqual(got, [800])
        self.assertEqual(sent, [([800], signal.SIGTERM)])

    def test_nothing_running(self):
        with mock.patch.object(wd, "_procs", lambda: []):
            self.assertEqual(wd.stop_all_producers(names=["owlet"]), [])

    @staticmethod
    def _wrapper_cmd(name):
        """The cmdline /proc shows for the real generated wrapper of `name`."""
        src = cs._exec_source(name)
        return "bash -c " + src[len("exec:bash -c '"):src.index("'#")]

    def _stop(self, procs, names, **kw):
        sent = []
        with mock.patch.object(wd, "_procs", lambda: procs), \
                mock.patch.object(wd, "_kill", lambda pids, sig: sent.append((sorted(pids), sig))), \
                mock.patch.object(wd, "_gone", lambda pids, t: True):
            got = wd.stop_all_producers(names=names, timeout=0, **kw)
        return got, sent

    def test_generated_wrapper_carries_the_producer_marker(self):
        cmd = self._wrapper_cmd("nursery")
        self.assertIn("OWLET_PRODUCER=nursery;", cmd)
        self.assertTrue(wd.is_producer_wrapper(cmd))
        self.assertFalse(wd.is_producer_wrapper("python3 /app/tutk_client.py"))
        self.assertFalse(wd.is_producer_wrapper("tail -n 200 /config/tutk-nursery.log"))

    def test_renamed_camera_producer_is_still_stopped(self):
        # the UI saved "nursery" -> "bedroom" before restarting go2rtc; the
        # producer still runs under the OLD name, which the new config lacks
        procs = [
            (300, 1, self._wrapper_cmd("nursery")),
            (301, 300, "python3 /app/tutk_client.py"),
            (302, 300, "ffmpeg -f h264 -i - -f rtsp rtsp://127.0.0.1:8554/x"),
            (400, 1, "tail -f /config/tutk-nursery.log"),            # not ours
        ]
        got, sent = self._stop(procs, names=["bedroom"])
        self.assertEqual(got, [300, 301, 302])
        self.assertIn(300, sent[0][0])

    def test_deleted_camera_legacy_wrapper_without_marker_is_stopped(self):
        legacy = ("bash -c set -a; [ -f /config/cameras/gone.env ] && . /config/cameras/gone.env; "
                  "python3 /app/tutk_client.py 2>>/config/tutk-gone.log | ffmpeg -f h264 -i - "
                  "-c:v copy -f rtsp rtsp://127.0.0.1:8554/x")
        procs = [(500, 1, legacy), (501, 500, "python3 /app/tutk_client.py")]
        got, _sent = self._stop(procs, names=[])
        self.assertEqual(got, [500, 501])

    def test_old_names_passed_by_the_caller_are_matched_too(self):
        # an unrecognised wrapper shape is still found through the pre-save name
        odd = "sh -c python3 /app/tutk_client.py 2>&1 >>/config/tutk-old.log"
        procs = [(600, 1, odd), (601, 600, "python3 /app/tutk_client.py")]
        self.assertEqual(self._stop(procs, names=["new"])[0], [])
        got, _sent = self._stop(procs, names=["new"], extra_names=["old"])
        self.assertEqual(got, [600, 601])

    def test_falls_back_to_ps_when_proc_is_unreadable(self):
        procs = [(700, 1, self._wrapper_cmd("cam")), (701, 700, "python3 /app/tutk_client.py")]

        def no_proc():
            raise FileNotFoundError("/proc")
        sent = []
        with mock.patch.object(wd, "_procs", no_proc), \
                mock.patch.object(wd, "_procs_ps", lambda: procs), \
                mock.patch.object(wd, "_kill", lambda pids, sig: sent.append(sorted(pids))), \
                mock.patch.object(wd, "_gone", lambda pids, t: True):
            self.assertEqual(wd.stop_all_producers(names=[], timeout=0), [700, 701])


class TutkSigtermHandlerTests(unittest.TestCase):
    def setUp(self):
        import tutk_client as tc
        self.tc = tc
        self.logs = []
        for p in (mock.patch.dict(tc._SHUTTING_DOWN, {"flag": False, "logged": False}),
                  mock.patch.object(tc, "log", lambda *a: self.logs.append(a))):
            p.start()
            self.addCleanup(p.stop)

    def test_second_sigterm_does_not_interrupt_teardown(self):
        exits = 0
        for _ in range(3):
            try:
                self.tc._on_sigterm(signal.SIGTERM, None)
            except SystemExit as e:
                self.assertEqual(e.code, 0)
                exits += 1
        self.assertEqual(exits, 1, "exactly one SystemExit, later SIGTERMs ignored")
        self.assertTrue(self.tc._SHUTTING_DOWN["flag"])
        self.assertEqual(len(self.logs), 1, "the ignored repeat is logged once")

    def test_main_installs_the_idempotent_handler(self):
        installed = {}
        with mock.patch.object(signal, "signal",
                               lambda s, h: installed.__setitem__(s, h)), \
                mock.patch.object(self.tc, "resolve_creds",
                                  side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                self.tc.main()
        self.assertIs(installed[signal.SIGTERM], self.tc._on_sigterm)


class WatchdogDefaultTests(unittest.TestCase):
    def test_off_unless_enabled(self):
        env = {k: v for k, v in os.environ.items() if k != "OWLET_WATCHDOG"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(wd.time, "sleep", side_effect=AssertionError("ran")):
            wd.main()   # returns immediately
        with mock.patch.dict(os.environ, {"OWLET_WATCHDOG": "1"}), \
                mock.patch.object(wd.time, "sleep", side_effect=RuntimeError("started")):
            with self.assertRaises(RuntimeError):
                wd.main()

    def test_startup_script_default_off(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "start-bionic.sh"), encoding="utf-8") as fh:
            self.assertIn('"${OWLET_WATCHDOG:-0}" = "1"', fh.read())


try:
    import flask  # noqa: F401
    import requests  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False


@unittest.skipUnless(HAVE_FLASK, "flask/requests not installed")
class RestartOrderTests(unittest.TestCase):
    def test_producers_stopped_before_go2rtc_restart(self):
        import webapp
        order = []
        with mock.patch.object(wd, "stop_all_producers",
                               lambda: order.append("stop") or [1]), \
                mock.patch.object(webapp.requests, "post",
                                  lambda *a, **k: order.append("restart")):
            webapp._stop_and_restart()
        self.assertEqual(order, ["stop", "restart"])


if __name__ == "__main__":
    unittest.main()
