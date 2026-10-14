"""The video frame loop and the stall supervisor must never wait on /config.

On Unraid, /config sat on the FUSE user share; under mover load a write could
block for tens of seconds, and the frame loop (sensor sidecar, log lines) and the
stall supervisor (log + stack dump before avClientStop) blocked with it."""
import io
import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tutk_client as tc  # noqa: E402


class _BlockingStream(io.StringIO):
    def __init__(self, gate):
        super().__init__()
        self.gate = gate

    def write(self, s):
        self.gate.wait(5)
        return super().write(s)


class SensorWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "cam-x.json")
        for name, val in (("CAM_SENSORS_PATH", self.path), ("SENSORS_ENABLED", True),
                          ("TEMP_SCALE", 1.0), ("_CAM_SENSORS", {}),
                          ("_CAM_SENSORS_DIRTY", threading.Event()),
                          ("_SENSOR_WRITER", [None])):
            p = mock.patch.object(tc, name, val)
            p.start()
            self.addCleanup(p.stop)

    def test_publish_returns_while_the_disk_is_stalled_and_the_latest_value_lands(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        real = tc._write_cam_sensors_now
        calls = []

        def stalled_write():
            calls.append(1)
            gate.wait(5)
            real()

        with mock.patch.object(tc, "_write_cam_sensors_now", stalled_write):
            t0 = time.monotonic()
            for temp in (20, 21, 22, 23):
                tc._publish_cam_sensors(temperature=temp)
                time.sleep(0.02)
            self.assertLess(time.monotonic() - t0, 0.5)   # never waited on the write
            gate.set()
            deadline = time.monotonic() + 3
            data = None
            while time.monotonic() < deadline:
                try:
                    with open(self.path) as f:
                        data = json.load(f)
                    if data.get("temperature") == 23:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.02)
        self.assertEqual(data and data.get("temperature"), 23)
        self.assertLessEqual(len(calls), 2)   # coalesced, not one write per reading


class NonBlockingLogTests(unittest.TestCase):
    def setUp(self):
        for name, val in (("_LOG_Q", queue.Queue(maxsize=3)), ("_LOG_WRITER", [None]),
                          ("_LOG_DROPPED", [0])):
            p = mock.patch.object(tc, name, val)
            p.start()
            self.addCleanup(p.stop)

    def test_log_never_blocks_and_counts_dropped_lines(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        out = _BlockingStream(gate)
        with mock.patch("sys.stderr", out):
            t0 = time.monotonic()
            for i in range(10):
                tc.log(f"line {i}")
            self.assertLess(time.monotonic() - t0, 0.5)
            gate.set()
            self.assertTrue(tc._log_drain(3))
            tc.log("after")
            self.assertTrue(tc._log_drain(3))
        text = out.getvalue()
        self.assertIn("[tutk] line 0", text)
        self.assertIn("line(s) dropped", text)
        self.assertIn("[tutk] after", text)

    def test_drain_is_bounded_when_the_sink_is_wedged(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch("sys.stderr", _BlockingStream(gate)):
            tc.log("stuck")
            t0 = time.monotonic()
            self.assertFalse(tc._log_drain(0.3))
            self.assertLess(time.monotonic() - t0, 1.0)


class StallSupervisorOrderTests(unittest.TestCase):
    def test_avclientstop_happens_before_any_log_and_stacks_are_logged(self):
        events = []
        done = threading.Event()

        class FakeAV:
            def avClientStop(self, idx):
                events.append("stop")
                done.set()
                return 0

        st = {"video_at": time.time() - 1000, "frames": 7}
        with mock.patch.object(tc, "log", lambda *a: events.append(" ".join(map(str, a)))), \
                mock.patch.object(tc, "_main_wchan", lambda: "request_wait_answer"):
            tc._stall_supervisor(FakeAV(), 0, st, done)
        self.assertEqual(events[0], "stop")
        joined = "\n".join(events[1:])
        self.assertIn("wchan=request_wait_answer", joined)
        self.assertIn("thread stacks at detection", joined)
        self.assertIn("Thread", joined)
        self.assertIn("avClientStop -> 0", joined)
        self.assertIn("frame loop is back", joined)


if __name__ == "__main__":
    unittest.main()
