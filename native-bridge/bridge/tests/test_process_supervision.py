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
        self.assertEqual(sent[0], ([100], signal.SIGTERM))
        self.assertEqual(sent[1], ([101, 102], signal.SIGTERM))
        self.assertEqual(sent[2], ([100, 101, 102], signal.SIGKILL))

    def test_nothing_running(self):
        with mock.patch.object(wd, "_procs", lambda: []):
            self.assertEqual(wd.stop_all_producers(names=["owlet"]), [])


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
