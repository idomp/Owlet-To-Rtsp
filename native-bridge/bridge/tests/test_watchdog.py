"""Unit tests for the watchdog escalation ladder (no camera, no container).

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import watchdog as wd  # noqa: E402


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def now(self):
        return self.t

    def wall(self):
        return 1_700_000_000.0 + self.t

    def advance(self, s):
        self.t += s


class Harness:
    """One camera, fully injectable, with a scripted liveness."""

    def __init__(self, state_path=None, **kw):
        self.clock = FakeClock()
        self.alive_now = True
        self.age = None
        self.producer_restarts = []
        self.container_restarts = 0
        self.logs = []
        self.wd = wd.Watchdog(
            names=lambda: ["owlet"], alive=lambda n: self.alive_now,
            restart_producer=self._rp, restart_container=self._rc,
            now=self.clock.now, wall=self.clock.wall,
            state_path=state_path, log=self.logs.append,
            producer_age=lambda n: self.age,
            stall=120, producer_restarts=2, cooldown=300, cooldown_max=1800,
            healthy_reset=900, **kw)

    def _rp(self, name):
        self.producer_restarts.append((name, self.clock.t))
        return True

    def _rc(self):
        self.container_restarts += 1

    def run(self, seconds, step=15):
        """Tick every `step` seconds for `seconds` of fake time."""
        for _ in range(int(seconds // step)):
            self.wd.tick()
            self.clock.advance(step)


class LadderTests(unittest.TestCase):
    def test_healthy_camera_never_restarts(self):
        h = Harness()
        h.run(3600)
        self.assertEqual(h.producer_restarts, [])
        self.assertEqual(h.container_restarts, 0)

    def test_stage1_fires_after_stall_not_before(self):
        h = Harness()
        h.alive_now = False
        h.run(119)          # ticks at 0..105s: below STALL
        self.assertEqual(h.producer_restarts, [])
        h.run(30)           # crosses 120s
        self.assertEqual(len(h.producer_restarts), 1)
        self.assertEqual(h.producer_restarts[0][0], "owlet")

    def test_two_stage1_then_container(self):
        h = Harness()
        h.alive_now = False
        h.run(120 * 3 + 15)
        self.assertEqual(len(h.producer_restarts), 2)
        self.assertEqual(h.container_restarts, 1)
        self.assertTrue(h.wd.container_restarted)

    def test_recovery_resets_the_ladder(self):
        h = Harness()
        h.alive_now = False
        h.run(135)          # one stage-1 restart
        self.assertEqual(len(h.producer_restarts), 1)
        h.alive_now = True
        h.run(60)           # recovered
        self.assertIn("recovered", " ".join(h.logs))
        h.alive_now = False
        h.run(135)          # a new outage starts the ladder from stage 1 again
        self.assertEqual(len(h.producer_restarts), 2)
        self.assertEqual(h.container_restarts, 0)

    def test_container_cooldown_keeps_doing_stage1(self):
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "watchdog.json")
            h = Harness(state_path=sp)
            h.alive_now = False
            h.run(120 * 3 + 15)                  # -> container restart #1
            self.assertEqual(h.container_restarts, 1)
            st = json.load(open(sp))
            self.assertEqual(st["container_restarts"]["consecutive"], 1)
            # "After the restart": a fresh watchdog process loads the state.
            h2 = Harness(state_path=sp)
            h2.clock.t = h.clock.t + 60        # GRACE later, still dead
            h2.alive_now = False
            h2.run(120 * 3 + 15)
            # 2 stage-1s, then stage 2 is due ~420s after restart #1; the cooldown
            # is 300s so it IS allowed -> restart #2, and the cooldown becomes 600s.
            self.assertEqual(h2.container_restarts, 1)
            st = json.load(open(sp))
            self.assertEqual(st["container_restarts"]["consecutive"], 2)
            h3 = Harness(state_path=sp)
            h3.clock.t = h2.clock.t + 60
            h3.alive_now = False
            h3.run(120 * 3 + 15)               # due at ~+420s but cooldown is 600s
            self.assertEqual(h3.container_restarts, 0)
            self.assertGreaterEqual(len(h3.producer_restarts), 3)
            self.assertIn("cooldown", " ".join(h3.logs))
            # Restart #2 was at h2's t+360; h3 started 75s later, so its stage-2
            # attempts at +360 and +480 are still inside the 600s cooldown (stage
            # 1 instead); the one at +600 is past it.
            h3.run(260)
            self.assertEqual(h3.container_restarts, 1)
            self.assertEqual(len(h3.producer_restarts), 4)
            st = json.load(open(sp))
            self.assertEqual(st["container_restarts"]["consecutive"], 3)

    def test_healthy_stretch_resets_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "watchdog.json")
            h = Harness(state_path=sp)
            h.wd.state["container_restarts"] = {"last": h.clock.wall() - 10, "consecutive": 3}
            h.alive_now = True
            h.run(899)
            self.assertEqual(h.wd.state["container_restarts"]["consecutive"], 3)
            h.run(30)
            self.assertEqual(h.wd.state["container_restarts"]["consecutive"], 0)
            self.assertEqual(json.load(open(sp))["container_restarts"]["consecutive"], 0)

    def test_state_file_carries_live_status(self):
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "watchdog.json")
            h = Harness(state_path=sp)
            h.run(30)
            st = json.load(open(sp))
            self.assertTrue(st["cameras"]["owlet"]["alive"])
            h.alive_now = False
            h.run(150)
            st = json.load(open(sp))
            self.assertFalse(st["cameras"]["owlet"]["alive"])
            self.assertEqual(st["cameras"]["owlet"]["producer_restarts"], 1)
            self.assertIsNotNone(st["cameras"]["owlet"]["stalled_since"])

    def test_corrupt_state_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "watchdog.json")
            with open(sp, "w") as fh:
                fh.write("{not json")
            h = Harness(state_path=sp)
            self.assertEqual(h.wd.state["container_restarts"]["consecutive"], 0)
            h.run(30)
            json.load(open(sp))  # rewritten cleanly

    def test_removed_camera_is_forgotten(self):
        names = ["owlet", "nursery2"]
        h = Harness()
        h.wd.names = lambda: names
        h.alive_now = False
        h.run(60)
        self.assertIn("nursery2", h.wd.bad_since)
        names.remove("nursery2")
        h.run(15)
        self.assertNotIn("nursery2", h.wd.bad_since)
        self.assertNotIn("nursery2", h.wd.cams)

    def test_young_stream_process_is_not_restarted(self):
        h = Harness()
        h.alive_now = False
        h.age = 20            # the supervisor relaunched it 20 s ago
        h.run(135)
        self.assertEqual(h.producer_restarts, [])
        self.assertIn("recovery in flight", " ".join(h.logs))
        h.age = 300           # old process, still dead -> stage 1 as usual
        h.run(120)
        self.assertEqual(len(h.producer_restarts), 1)


class NamesTests(unittest.TestCase):
    def test_unconfigured_install_has_nothing_to_probe(self):
        # config_store.camera_names() would answer ["owlet"] here (placeholder
        # stream) — the watchdog must not probe a camera that cannot exist yet.
        self.assertEqual(wd._names({"cameras": []}), [])
        self.assertEqual(wd._names({}), [])

    def test_only_connectable_cameras(self):
        cfg = {"email": "e", "password": "p", "cameras": [
            {"name": "owlet", "uid": "ABCDEFGHIJKLMNOPQRST"},   # saved key
            {"name": "nursery2", "camera_dsn": "OCD123"},               # key via KMS
            {"name": "draft", "uid": "", "camera_dsn": ""},             # nothing yet
        ]}
        self.assertEqual(wd._names(cfg), ["owlet", "nursery2"])
        cfg.pop("password")                                      # no login -> DSN alone is useless
        self.assertEqual(wd._names(cfg), ["owlet"])


class SpareStreamTests(unittest.TestCase):
    def test_pick_first_unused_spare(self):
        import config_store as cs
        streams = {"owlet": {"consumers": []}, "owlet_spare1": {"consumers": []},
                   "owlet_spare2": {"consumers": None}, "owlet_spare3": {"consumers": None}}
        self.assertEqual(cs.pick_spare("owlet", streams), "owlet_spare2")
        streams["owlet_spare2"]["consumers"] = []
        self.assertEqual(cs.pick_spare("owlet", streams), "owlet_spare3")
        streams["owlet_spare3"]["consumers"] = [{"id": 9}]
        self.assertIsNone(cs.pick_spare("owlet", streams))          # all used -> escalate
        self.assertIsNone(cs.pick_spare("nursery2", streams))       # no spares defined

    def test_generated_config_has_identical_spares(self):
        import config_store as cs
        cfg = cs.render_go2rtc([dict(cs.CAMERA_DEFAULTS, name="owlet")])
        self.assertIn("  owlet:\n", cfg)
        for sp in cs.spare_names("owlet"):
            self.assertIn(f"  {sp}:\n", cfg)
        # sources are rendered as escaped double-quoted scalars (_yaml_str)
        self.assertEqual(cfg.count(cs._yaml_str(cs._exec_source("owlet"))),
                         1 + cs.SPARE_STREAMS)


class OrphanFfmpegTests(unittest.TestCase):
    def test_orphaned_producer_ffmpeg_is_found_others_are_not(self):
        prod = ("ffmpeg -hide_banner -loglevel warning -fflags +genpts -use_wallclock_as_timestamps 1 "
                "-analyzeduration 5000000 -probesize 5000000 -f h264 -i - -c:v copy -f rtsp "
                "-rtsp_transport tcp rtsp://127.0.0.1:8554/abc")
        procs = [
            (1, 0, "go2rtc -config /config/go2rtc.gen.yaml"),
            (52, 1, prod),                                   # orphan: wrapper gone -> target
            (33, 1, "bash -c set -a; python3 /app/tutk_client.py 2>>/config/tutk-owlet.log | " + prod),
            (40, 33, prod),                                  # live pipeline member -> not an orphan
            (7173, 28, "ffmpeg -hide_banner -loglevel error -rtsp_transport tcp -rw_timeout 15000000 "
                       "-i rtsp://127.0.0.1:8554/owlet -c copy -f mpegts /dev/null"),   # keepalive viewer
            (80, 1, "ffprobe -v error -rtsp_transport tcp -i rtsp://127.0.0.1:8554/owlet"),
        ]
        self.assertEqual(wd.orphan_producer_ffmpegs(procs), [52])
        self.assertEqual(wd.orphan_producer_ffmpegs([]), [])


class ProducerPidTests(unittest.TestCase):
    WRAP = ("bash -c set -a; [ -f /config/cameras/owlet.env ] && . /config/cameras/owlet.env; "
            "python3 /app/tutk_client.py 2>>/config/tutk-owlet.log | ffmpeg -f h264 -i - "
            "-c:v copy -f rtsp rtsp://127.0.0.1:8554/abc")

    def test_finds_wrapper_python_and_ffmpeg_only_for_that_camera(self):
        procs = [
            (1, 0, "go2rtc -config /config/go2rtc.gen.yaml"),
            (33, 1, self.WRAP),
            (39, 33, "python3 /app/tutk_client.py"),
            (40, 33, "ffmpeg -hide_banner -f h264 -i - -c:v copy -f rtsp rtsp://127.0.0.1:8554/abc"),
            (50, 1, self.WRAP.replace("owlet", "nursery2")),
            (51, 50, "python3 /app/tutk_client.py"),
            (60, 22, "tail -n 200 /config/tutk-owlet.log"),   # UI log viewer: never a target
            (25, 1, "python3 /app/watchdog.py"),
        ]
        py, other, wrap = wd.producer_pids("owlet", procs)
        self.assertEqual((py, other, wrap), ([39], [40], [33]))
        py, other, wrap = wd.producer_pids("nursery2", procs)
        self.assertEqual((py, other, wrap), ([51], [], [50]))
        self.assertEqual(wd.producer_pids("ghost", procs), ([], [], []))

    def test_default_camera_matches_baked_in_fallback_log(self):
        wrap = self.WRAP.replace("/config/tutk-owlet.log", "/config/tutk.log")
        procs = [(33, 1, wrap), (39, 33, "python3 /app/tutk_client.py")]
        self.assertEqual(wd.producer_pids("owlet", procs), ([39], [], [33]))


if __name__ == "__main__":
    unittest.main()
