"""The video frame loop and the stall supervisor must never wait on /config.

On Unraid, /config sat on the FUSE user share; under mover load a write could
block for tens of seconds, and the frame loop (sensor sidecar, log lines, the
first-frame login-guard report) and the stall supervisor (log + stack dump
before avClientStop) blocked with it."""
import json
import os
import queue
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

    def _stop_writer(self):
        self.stop.set()
        self.dirty.set()
        t = tc._SENSOR_WRITER[0]
        if t is not None:
            t.join(3)
            self.assertFalse(t.is_alive())

    def _read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_publishing_never_waits_for_a_stalled_write(self):
        entered, gate = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        real_replace = os.replace
        replaces = []

        def stalled_replace(src, dst):
            if dst == self.path:
                replaces.append(1)
                entered.set()
                gate.wait(5)
            return real_replace(src, dst)

        with mock.patch("os.replace", stalled_replace):
            tc._publish_cam_sensors(temperature=20)
            self.assertTrue(entered.wait(3), "writer never reached the file write")
            # The writer is now blocked inside the real write path.
            self.assertTrue(tc._CAM_SENSORS_LOCK.acquire(timeout=0.5),
                            "the sensor lock is held across disk I/O")
            tc._CAM_SENSORS_LOCK.release()
            for temp in (21, 22, 23):
                done = threading.Event()
                threading.Thread(target=lambda t=temp: (tc._publish_cam_sensors(temperature=t),
                                                        done.set()), daemon=True).start()
                self.assertTrue(done.wait(1), "publish blocked behind the stalled write")
            gate.set()
            deadline = time.monotonic() + 3
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


class _LogFixture(unittest.TestCase):
    def setUp(self):
        self.q = queue.Queue(maxsize=3)
        self.out = []
        self.entered, self.gate = threading.Event(), threading.Event()
        self.addCleanup(self.gate.set)

        def sink(text):
            self.entered.set()
            self.gate.wait(5)
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
            t.join(3)
            self.assertFalse(t.is_alive())


class NonBlockingLogTests(_LogFixture):
    def test_full_queue_drops_exactly_the_overflow_and_reports_it_in_order(self):
        tc.log("line 0")
        self.assertTrue(self.entered.wait(3))   # writer holds line 0, blocked in the sink
        t0 = time.monotonic()
        for i in range(1, 10):
            tc.log(f"line {i}")                 # 1-3 fit the queue, 4-9 are dropped
        self.assertLess(time.monotonic() - t0, 0.5)
        self.gate.set()
        self.assertTrue(tc._log_drain(3))
        text = "".join(self.out)
        lines = [ln.split(" [tutk] ", 1)[1] for ln in text.splitlines()]
        self.assertEqual(lines, ["line 0",
                                 "[log] 6 line(s) dropped while the log was not draining",
                                 "line 1", "line 2", "line 3"])

    def test_drain_is_bounded_when_the_sink_is_wedged(self):
        tc.log("stuck")
        self.assertTrue(self.entered.wait(3))
        t0 = time.monotonic()
        self.assertFalse(tc._log_drain(0.3))
        self.assertLess(time.monotonic() - t0, 1.0)


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

    def test_first_frame_returns_while_the_guard_state_file_is_stalled(self):
        import login_guard
        entered, gate = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        seen = []

        def stalled_record(fp):
            entered.set()
            gate.wait(5)
            seen.append(fp)
            return False

        expected = login_guard.key_fingerprint(tc.UID, tc.AUTHKEY, tc.AV_PASSWORD)
        with mock.patch.object(login_guard, "record_video", stalled_record):
            t0 = time.monotonic()
            tc._on_first_frame()
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertTrue(entered.wait(3))
            self.assertFalse(tc._guard_drain(0.2))   # bounded while stalled
            gate.set()
            self.assertTrue(tc._guard_drain(3))
        self.assertEqual(seen, [expected])
        self.q.put(None)
        tc._GUARD_WORKER[0].join(3)
        self.assertFalse(tc._GUARD_WORKER[0].is_alive())


class ExitWithWedgedLogTests(unittest.TestCase):
    """A real process must exit promptly even when its log sink never returns."""

    def _run(self, ending: str) -> subprocess.CompletedProcess:
        script = textwrap.dedent(f"""
            import sys, threading, time
            sys.path.insert(0, {BRIDGE!r})
            import tutk_client as tc
            entered = threading.Event()
            def wedged(text):
                entered.set()
                time.sleep(60)
            tc._log_sink = wedged
            tc._install_excepthooks()
            tc.log("first line")
            assert entered.wait(5)
            tc.log("queued behind the wedge")
            {ending}
        """)
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=20)
        r.elapsed = time.monotonic() - t0
        return r

    def test_sys_exit(self):
        r = self._run("sys.exit(0)")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(r.elapsed, 8)

    def test_normal_end(self):
        r = self._run("pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(r.elapsed, 8)

    def test_uncaught_exception(self):
        r = self._run("raise RuntimeError('boom')")
        self.assertEqual(r.returncode, 1)
        self.assertLess(r.elapsed, 8)


if __name__ == "__main__":
    unittest.main()
