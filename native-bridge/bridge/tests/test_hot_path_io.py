"""The video frame loop and the stall supervisor must never wait on /config.

On Unraid, /config sat on the FUSE user share; under mover load a write could
block for tens of seconds, and the frame loop (sensor sidecar, log lines, the
first-frame login-guard report) and the stall supervisor (log + stack dump
before avClientStop) blocked with it."""
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)
import tutk_client as tc  # noqa: E402

WAIT = 5     # generous: these are "does it ever return" checks, not timings
HOLD = 60    # stalled fakes stay stalled until the test releases them


def _in_thread(fn) -> threading.Event:
    done = threading.Event()

    def run():
        fn()
        done.set()
    threading.Thread(target=run, daemon=True).start()
    return done


class SensorWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "cam-x.json")
        self.dirty, self.stop = threading.Event(), threading.Event()
        for name, val in (("CAM_SENSORS_PATH", self.path), ("SENSORS_ENABLED", True),
                          ("TEMP_SCALE", 1.0), ("_CAM_SENSORS", {}),
                          ("_CAM_SENSORS_DIRTY", self.dirty),
                          ("_SENSOR_WRITER_STOP", self.stop),
                          ("_SENSOR_WRITER", [None])):
            p = mock.patch.object(tc, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.gate = threading.Event()
        self.addCleanup(self._stop_writer)   # runs after the gate is released

    def _stop_writer(self):
        self.gate.set()
        self.stop.set()
        self.dirty.set()
        t = tc._SENSOR_WRITER[0]
        if t is not None:
            t.join(WAIT)
            self.assertFalse(t.is_alive())

    def _read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_publishing_never_waits_for_a_stalled_write(self):
        entered = threading.Event()
        real_replace = os.replace
        replaces = []

        def stalled_replace(src, dst):
            if dst == self.path:
                replaces.append(1)
                entered.set()
                self.gate.wait(HOLD)
            return real_replace(src, dst)

        with mock.patch("os.replace", stalled_replace):
            tc._publish_cam_sensors(temperature=20)
            self.assertTrue(entered.wait(WAIT), "writer never reached the file write")
            # The writer is now blocked inside the real write path.
            self.assertTrue(tc._CAM_SENSORS_LOCK.acquire(timeout=WAIT),
                            "the sensor lock is held across disk I/O")
            tc._CAM_SENSORS_LOCK.release()
            for temp in (21, 22, 23):
                done = _in_thread(lambda t=temp: tc._publish_cam_sensors(temperature=t))
                self.assertTrue(done.wait(WAIT), "publish blocked behind the stalled write")
            self.gate.set()
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline:
                try:
                    if self._read().get("temperature") == 23:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.02)
            self._stop_writer()
        self.assertEqual(self._read()["temperature"], 23)
        self.assertEqual(len(replaces), 2)   # first value, then one coalesced write


class NonBlockingLogTests(unittest.TestCase):
    def setUp(self):
        self.q = queue.Queue(maxsize=3)
        self.out = []
        self.entered, self.gate = threading.Event(), threading.Event()

        def sink(text):
            self.entered.set()
            self.gate.wait(HOLD)
            self.out.append(text)

        for name, val in (("_LOG_Q", self.q), ("_LOG_WRITER", [None]),
                          ("_LOG_DROPPED", [0]), ("_log_sink", sink)):
            p = mock.patch.object(tc, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._stop_writer)

    def _stop_writer(self):
        self.gate.set()
        t = tc._LOG_WRITER[0]
        if t is not None:
            self.q.put(None)
            t.join(WAIT)
            self.assertFalse(t.is_alive())

    def test_full_queue_drops_exactly_the_overflow_and_reports_it_in_order(self):
        tc.log("line 0")
        self.assertTrue(self.entered.wait(WAIT))   # writer holds line 0, blocked in the sink
        done = _in_thread(lambda: [tc.log(f"line {i}") for i in range(1, 10)])
        self.assertTrue(done.wait(WAIT), "log() blocked on a stalled sink")
        self.gate.set()
        self.assertTrue(tc._log_drain(WAIT))
        lines = [ln.split(" [tutk] ", 1)[1] for ln in "".join(self.out).splitlines()]
        self.assertEqual(lines, ["line 0",
                                 "[log] 6 record(s) dropped while the log was not draining",
                                 "line 1", "line 2", "line 3"])

    def test_drain_is_bounded_when_the_sink_is_wedged(self):
        tc.log("stuck")
        self.assertTrue(self.entered.wait(WAIT))
        t0 = time.monotonic()
        self.assertFalse(tc._log_drain(0.3))
        self.assertLess(time.monotonic() - t0, 3)

    def test_stderr_writes_from_other_modules_are_queued(self):
        tc.log("first")
        self.assertTrue(self.entered.wait(WAIT))
        err = tc._QueuedStderr()
        done = _in_thread(lambda: (print("login guard: warning", file=err), err.flush()))
        self.assertTrue(done.wait(WAIT), "print to the queued stderr blocked")
        self.gate.set()
        self.assertTrue(tc._log_drain(WAIT))
        self.assertEqual("".join(self.out[1:]), "login guard: warning\n")


class StallSupervisorTests(unittest.TestCase):
    def test_evidence_is_queued_before_avclientstop_and_needs_no_source_lookup(self):
        events = []
        done = threading.Event()

        class FakeAV:
            def avClientStop(self, idx):
                events.append("stop")
                done.set()
                return 0

        def no_source(*a, **k):
            raise AssertionError("stack capture read source files")

        st = {"video_at": time.time() - 1000, "frames": 7}
        with mock.patch.object(tc, "log", lambda *a: events.append(" ".join(map(str, a)))), \
                mock.patch.object(tc, "_main_wchan", lambda: "request_wait_answer"), \
                mock.patch("linecache.getline", no_source), \
                mock.patch("linecache.getlines", no_source):
            tc._stall_supervisor(FakeAV(), 0, st, done)
        stop_at = events.index("stop")
        before, after = "\n".join(events[:stop_at]), "\n".join(events[stop_at + 1:])
        self.assertIn("wchan=request_wait_answer", before)
        self.assertIn("thread stacks at detection", before)
        self.assertIn("most recent call first", before)
        self.assertIn("in _stall_supervisor", before)
        self.assertIn("avClientStop -> 0", after)
        self.assertIn("frame loop is back", after)


class FirstFrameGuardReportTests(unittest.TestCase):
    def setUp(self):
        self.q = queue.Queue(maxsize=16)
        for name, val in (("_GUARD_Q", self.q), ("_GUARD_WORKER", [None]),
                          ("_SUCCESS_RECORDED", [False]), ("_SESSION_VIDEO", [False]),
                          ("_HAD_VIDEO", [False])):
            p = mock.patch.object(tc, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.gate = threading.Event()
        self.addCleanup(self._stop_worker)

    def _stop_worker(self):
        self.gate.set()
        t = tc._GUARD_WORKER[0]
        if t is not None:
            self.q.put(None)
            t.join(WAIT)
            self.assertFalse(t.is_alive())

    def test_first_frame_returns_while_the_guard_state_file_is_stalled(self):
        import login_guard
        entered = threading.Event()
        seen = []

        def stalled_record(fp):
            entered.set()
            self.gate.wait(HOLD)
            seen.append(fp)
            return False

        expected = login_guard.key_fingerprint(tc.UID, tc.AUTHKEY, tc.AV_PASSWORD)
        with mock.patch.object(login_guard, "record_video", stalled_record):
            done = _in_thread(tc._on_first_frame)
            self.assertTrue(done.wait(WAIT), "the frame loop waited for the guard report")
            self.assertTrue(entered.wait(WAIT))
            self.assertFalse(tc._guard_drain(0.2))   # bounded while stalled
            self.gate.set()
            self.assertTrue(tc._guard_drain(WAIT))
        self.assertEqual(seen, [expected])

    def test_delayed_report_keeps_the_key_of_the_session_that_had_video(self):
        import login_guard
        entered = threading.Event()
        seen = []

        def stalled_record(fp):
            entered.set()
            self.gate.wait(HOLD)
            seen.append(fp)
            return False

        with mock.patch.object(tc, "UID", "UID-A"), mock.patch.object(tc, "AUTHKEY", "k-a"), \
                mock.patch.object(tc, "AV_PASSWORD", "p-a"), \
                mock.patch.object(login_guard, "record_video", stalled_record):
            key_a = login_guard.key_fingerprint("UID-A", "k-a", "p-a")
            tc._dispatch_guard_report("earlier")    # occupies the worker
            self.assertTrue(entered.wait(WAIT))
            tc._on_first_frame()                    # queued behind it, on key A
            tc.UID, tc.AUTHKEY, tc.AV_PASSWORD = "UID-B", "k-b", "p-b"   # refreshed key
            self.gate.set()
            self.assertTrue(tc._guard_drain(WAIT))
        self.assertEqual(seen, ["earlier", key_a])


@unittest.skipUnless(hasattr(signal, "SIGTERM") and os.name == "posix", "POSIX only")
class ExitWithWedgedStderrTests(unittest.TestCase):
    """A real stream process must exit promptly while its stderr (the /config log
    file in production) is wedged. Here stderr is a pipe nobody reads: once its
    buffer is full every write to fd 2 blocks, which is what a stalled FUSE
    write looks like to the process."""

    PRELUDE = """
        import os, signal, sys, threading, time
        sys.path.insert(0, {bridge!r})
        import tutk_client as tc
        tc._install_queued_stderr()
        for i in range(200):                       # ~200 KB: fills the pipe, writer blocks
            tc.log("x" * 1000)
        threading.Thread(target=lambda: print("other module warning", file=sys.stderr),
                         daemon=True).start()
        print("from the main thread", file=sys.stderr)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and tc._log_drain(0.1):
            pass
        if tc._log_drain(0.5):
            os._exit(42)                           # premise failed: stderr is not wedged
    """

    def _start(self, body: str) -> subprocess.Popen:
        script = textwrap.dedent(self.PRELUDE).format(bridge=BRIDGE) + textwrap.dedent(body)
        p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        self.addCleanup(self._reap, p)
        return p

    @staticmethod
    def _reap(p):
        if p.poll() is None:
            p.kill()
        p.wait()
        p.stdout.close()
        p.stderr.close()

    def _wait(self, p, limit=12) -> int:
        try:
            return p.wait(timeout=limit)   # stderr deliberately never read
        except subprocess.TimeoutExpired:
            self.fail("process hung on its wedged stderr")

    def test_sys_exit(self):
        self.assertEqual(self._wait(self._start("sys.exit(0)")), 0)

    def test_normal_end(self):
        self.assertEqual(self._wait(self._start("pass")), 0)

    def test_uncaught_exception(self):
        self.assertEqual(self._wait(self._start("raise RuntimeError('boom')")), 1)

    def test_sigterm_twice(self):
        p = self._start("""
            signal.signal(signal.SIGTERM, tc._on_sigterm)
            sys.stdout.write("ready\\n"); sys.stdout.flush()
            try:
                while True:
                    time.sleep(0.1)
            finally:
                # "teardown" that the second SIGTERM must not cut short
                sys.stdout.write("teardown-start\\n"); sys.stdout.flush()
                time.sleep(1.0)
                sys.stdout.write("teardown-done\\n"); sys.stdout.flush()
        """)
        import select

        def line():
            ready, _, _ = select.select([p.stdout], [], [], 15)
            self.assertTrue(ready, "the child stopped talking")
            return p.stdout.readline().strip()

        self.assertEqual(line(), b"ready")
        p.send_signal(signal.SIGTERM)
        self.assertEqual(line(), b"teardown-start")
        p.send_signal(signal.SIGTERM)              # lands inside the teardown
        self.assertEqual(line(), b"teardown-done")
        self.assertEqual(self._wait(p), 0)

    def test_stall_escalation_hard_exit(self):
        p = self._start("""
            class AV:
                def avClientStop(self, idx):
                    return 0                       # the blocked thread never comes back
            tc.STALL_UNBLOCK_WAIT = 0.3
            tc._stdout_readers = lambda: []
            tc._reset_go2rtc_stream = lambda name: None
            st = {"video_at": time.time() - 1000, "frames": 1}
            threading.Thread(target=tc._stall_supervisor,
                             args=(AV(), 0, st, threading.Event()), daemon=True).start()
            time.sleep(60)
        """)
        self.assertEqual(self._wait(p), 6)


if __name__ == "__main__":
    unittest.main()
