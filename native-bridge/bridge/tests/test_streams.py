"""Unit tests for the generated go2rtc stream config (no camera, no container).

These guard the camera-audio producer added alongside the video exec, and the
YAML-safety invariant that bit during its review.

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config_store as cs  # noqa: E402

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def _scalar(raw):
    """Decode one rendered list item. Sources are double-quoted with JSON
    escapes (`$` as \\u0024), which json.loads decodes exactly like YAML does."""
    return json.loads(raw) if raw.startswith('"') else raw


def sources(text):
    """Every `    - <source>` line of a rendered config, decoded."""
    return [_scalar(ln.strip()[2:]) for ln in text.splitlines() if ln.startswith("    - ")]


class YamlSafetyTests(unittest.TestCase):
    """The generated sources used to be emitted as UNQUOTED YAML scalars, where
    a ": " (colon-space) makes a YAML parser read the line as a mapping rather
    than a string. They are double-quoted now; the invariant is kept as a
    second line of defence. Checked as a plain string invariant so the suite
    stays stdlib-only."""

    def test_no_colon_space_in_any_generated_source(self):
        text = cs.render_go2rtc([{"name": "owlet"}, {"name": "nursery"}])
        for src in sources(text):
            self.assertNotIn(": ", src, f"colon-space would become a YAML map: {src}")

    def test_no_colon_space_in_each_builder(self):
        for build in (cs._exec_source, cs._audio_source, cs._overlay_source):
            with self.subTest(builder=build.__name__):
                self.assertNotIn(": ", build("owlet"))


class AudioProducerTests(unittest.TestCase):
    def setUp(self):
        self.text = cs.render_go2rtc([{"name": "owlet"}])

    def _stream(self, name):
        """The source lines belonging to one stream key."""
        out, cur = [], None
        for ln in self.text.splitlines():
            if ln.startswith("  ") and ln.rstrip().endswith(":") and not ln.startswith("    "):
                cur = ln.strip().rstrip(":")
            elif ln.startswith("    - ") and cur == name:
                out.append(_scalar(ln.strip()[2:]))
        return out

    def test_rendered_sources_hide_every_dollar_from_go2rtc(self):
        """go2rtc substitutes ${...} in the raw text before YAML decoding and
        treats everything after the first colon as its default, so a raw
        `${TMPDIR:-/tmp}` became `-/tmp` whenever TMPDIR was unset. The raw
        lines must carry no `${`; decoding must give the bash text back."""
        raw = [ln for ln in self.text.splitlines() if ln.startswith("    - ")]
        self.assertTrue(raw)
        for ln in raw:
            self.assertNotIn("${", ln)
            self.assertNotIn("$", ln)
        video = self._stream("owlet")[0]
        self.assertIn('D="${TMPDIR:-/tmp}"', video)
        self.assertIn('D="${TMPDIR:-/tmp}"', self._stream("owlet")[1])

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_yaml_decodes_the_literal_tmpdir_expansion(self):
        d = yaml.safe_load(self.text)
        video, audio = d["streams"]["owlet"]
        self.assertIn('D="${TMPDIR:-/tmp}"', video)
        self.assertIn('D="${TMPDIR:-/tmp}"', audio)
        self.assertEqual(video, cs._exec_source("owlet"))
        self.assertEqual(audio, cs._audio_source("owlet"))
        self.assertEqual(d["streams"]["owlet_overlay"], [cs._overlay_source("owlet")])

    def test_camera_has_video_then_audio(self):
        s = self._stream("owlet")
        self.assertEqual(len(s), 2)
        # order matters: video first so it is the stream's first track
        self.assertIn("-f h264 -i -", s[0])
        self.assertIn("tutk_client", s[0])
        self.assertIn("-f aac -i", s[1])
        self.assertNotIn("tutk_client", s[1])

    def test_video_exec_still_muxes_video_only(self):
        """The whole point of the split: audio failure cannot reach the picture,
        and talk-back parking the audio probe cannot stall the video mux."""
        video = self._stream("owlet")[0]
        self.assertIn("-c:v copy", video)
        self.assertNotIn("-f aac", video)
        self.assertNotIn("-c:a", video)

    def test_video_exec_hands_off_the_audio_fifo(self):
        video = self._stream("owlet")[0]
        self.assertIn('OWLET_AUDIO_FIFO="$A"', video)
        self.assertIn('A="$D/owlet-audio-owlet"', video)

    def test_audio_fifo_inode_is_stable_across_relaunches(self):
        """The audio reader can be parked in open() waiting for the first frame.
        rm+mkfifo would swap the inode underneath it, leaving that ffmpeg blocked
        on an unlinked pipe forever while tutk_client writes to a new one nobody
        reads -- audio dead until the container restarts. So the audio FIFO is
        create-if-absent and is never removed, unlike the talk FIFO."""
        video = self._stream("owlet")[0]
        self.assertIn('[ -p "$A" ] || mkfifo "$A"', video)
        self.assertNotIn('rm -f "$A"', video)
        trap = video.split("trap ")[1].split("EXIT")[0]
        self.assertNotIn("$A", trap)
        self.assertIn("$T", trap)     # talk FIFO still is cleaned up

    def test_audio_producer_emits_opus_not_copied_aac(self):
        """ffmpeg's RTSP muxer refuses AAC that arrived as ADTS ("AAC with no
        global headers is currently not supported") because the config is per
        frame rather than global extradata, and aac_adtstoasc can't fix it --
        the filter derives extradata only after the header is due. Copying ADTS
        made the first cut of this producer die on launch. Opus is also the only
        thing WebRTC can carry."""
        audio = self._stream("owlet")[1]
        self.assertIn("-c:a libopus", audio)
        self.assertNotIn("-c:a copy", audio)
        self.assertIn("-ar 48000", audio)   # Opus RTP clock rate is always 48k

    def test_audio_producer_waits_without_shell_arithmetic(self):
        """A literal word list instead of a counter: correct even if the text
        ever reaches go2rtc's ${...} pre-substitution unescaped."""
        audio = self._stream("owlet")[1]
        self.assertNotIn("$((", audio)
        self.assertNotIn("$i", audio)
        self.assertIn("for _ in 1 2 3", audio)

    def test_only_tmpdir_is_brace_expanded(self):
        import re
        for src in sources(self.text):
            for m in re.findall(r"\$\{[^}]*\}", src):
                self.assertIn("TMPDIR", m, f"unexpected brace expansion: {m}")

    def test_every_spare_carries_audio_too(self):
        """A self-heal alias swap points the camera name at a spare; a spare
        without the audio producer would silently drop sound."""
        for i in range(1, cs.SPARE_STREAMS + 1):
            s = self._stream(f"owlet_spare{i}")
            self.assertEqual(len(s), 2, f"spare{i} is not a full replacement")
            self.assertIn("-f aac -i", s[1])

    def test_overlay_stream_is_single_source(self):
        self.assertEqual(len(self._stream("owlet_overlay")), 1)

    def test_overlay_filter_is_quoted_and_expansion_free(self):
        """`x=(w-tw)/2` has parentheses, which bash treats as metacharacters:
        unquoted, `bash -c` failed with "syntax error near unexpected token `('"
        and the overlay never started (every DESCRIBE was a 404). The HUD text
        also contains "% RH", which drawtext otherwise parses as an expansion."""
        ov = self._stream("owlet_overlay")[0]
        self.assertIn('-filter:v "drawtext=', ov)
        self.assertIn(':expansion=none', ov)
        # no parenthesis may sit outside the double-quoted filter argument
        quoted = ov.split('-filter:v "', 1)[1].split('" ', 1)[0]
        outside = ov.replace(quoted, "")
        self.assertNotIn("(", outside)
        self.assertNotIn(")", outside)

    def test_audio_fifo_path_matches_the_generated_exec(self):
        """webapp/tutk helpers and the generated bash must agree on the path."""
        base = os.path.basename(cs.audio_fifo_path("owlet"))
        self.assertEqual(base, "owlet-audio-owlet")
        self.assertIn(base, self._stream("owlet")[0])
        self.assertIn(base, self._stream("owlet")[1])


class AudioFilterTests(unittest.TestCase):
    """The Opus producer's filter chain: async resampler first (bursty P2P
    arrival + wall-clock stamps otherwise reach the phone as jittery RTP
    timing = choppy), then a +10 dB limited gain (the cam mic peaks at only
    -16..-22 dBFS with someone talking). Must stay overridable and YAML-safe."""

    def _with(self, value):
        saved = os.environ.get("OWLET_AUDIO_GAIN_DB")
        if value is None: os.environ.pop("OWLET_AUDIO_GAIN_DB", None)
        else: os.environ["OWLET_AUDIO_GAIN_DB"] = value
        try:
            return cs.audio_filter(), cs._audio_source("owlet")
        finally:
            if saved is None: os.environ.pop("OWLET_AUDIO_GAIN_DB", None)
            else: os.environ["OWLET_AUDIO_GAIN_DB"] = saved

    def test_default_chain(self):
        f, src = self._with(None)
        self.assertEqual(f, "-af aresample=async=1:first_pts=0,volume=10dB,alimiter=limit=0.95 ")
        self.assertIn('-i "$A" ' + f + '-c:a libopus', src)

    def test_resampler_always_present_even_with_gain_disabled(self):
        f, src = self._with("0")
        self.assertEqual(f, "-af aresample=async=1:first_pts=0 ")
        self.assertNotIn("volume=", src)

    def test_override_and_bad_value(self):
        self.assertIn("volume=16dB", self._with("16")[0])
        self.assertIn("volume=10dB", self._with("garbage")[0])

    def test_resampler_precedes_gain(self):
        f = self._with(None)[0]
        self.assertLess(f.index("aresample"), f.index("volume="))

    def test_yaml_safe(self):
        self.assertNotIn(": ", self._with(None)[1])


class StreamReadyTests(unittest.TestCase):
    """Control-plane endpoints (sound machine, device info, LED, live volume)
    must gate on the talk FIFO, which the exec creates before tutk_client and
    removes on exit -- not on the audiocmd file, which the exec deletes at every
    start and which only exists after the first request is written."""

    def test_ready_iff_talk_fifo_exists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            saved = os.environ.get("TMPDIR")
            os.environ["TMPDIR"] = d
            try:
                self.assertFalse(cs.stream_ready("owlet"))
                os.mkfifo(cs.talk_fifo_path("owlet"))
                self.assertTrue(cs.stream_ready("owlet"))
                # the audiocmd file being absent must not matter
                self.assertFalse(os.path.exists(cs.audiocmd_file_path("owlet")))
                self.assertTrue(cs.stream_ready("owlet"))
            finally:
                if saved is None: os.environ.pop("TMPDIR", None)
                else: os.environ["TMPDIR"] = saved


class KeepaliveArgvTests(unittest.TestCase):
    def test_no_rw_timeout_and_noninteractive(self):
        """This build's ffmpeg rejects -rw_timeout on RTSP ("Option not found",
        exit 8) and prompts to overwrite /dev/null without -y/-nostdin. Either one
        silently kills the warm viewer (its stderr is DEVNULL)."""
        import keepalive
        argv = keepalive._argv("owlet")
        self.assertNotIn("-rw_timeout", argv)
        self.assertIn("-timeout", argv)
        self.assertIn("-nostdin", argv)
        self.assertIn("-y", argv)
        self.assertEqual(argv[-3:], ["-f", "mpegts", "/dev/null"])
        self.assertIn("rtsp://127.0.0.1:8554/owlet", argv)



if __name__ == "__main__":
    unittest.main()
