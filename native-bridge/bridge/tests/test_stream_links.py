"""Stream-link fields in the control panel's status/cameras payloads.

The UI builds the RTSP copy URL from rtsp_host/rtsp_user/rtsp_port (which of
OWLET_ADVERTISE_RTSP / PUBLIC_RTSP_PORT / the go2rtc listen port applies is
reported as rtsp_mode and rtsp_published) and only
offers WebRTC/HLS links when api_published is true (go2rtc API widened with
GO2RTC_API_LISTEN). Skipped when Flask/requests aren't installed.
"""
import contextlib
import io
import os
import unittest
from unittest import mock

from test_webapp_security import HAVE_FLASK, WebappTestBase, _auth

if HAVE_FLASK:
    import webapp  # noqa: E402
    import config_store as cs  # noqa: E402


@unittest.skipUnless(HAVE_FLASK, "flask/requests not installed")
class StreamLinkPayloadTests(WebappTestBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(webapp.socket, "gethostname", return_value="owlet-camera")
        p.start()
        self.addCleanup(p.stop)

    _ENV = ("GO2RTC_API_LISTEN", "GO2RTC_USER", "GO2RTC_PASS", "GO2RTC_RTSP_LISTEN",
            "PUBLIC_RTSP_PORT", "OWLET_ADVERTISE_RTSP")

    def _get(self, path, env, host=None):
        clean = {k: v for k, v in os.environ.items() if k not in self._ENV}
        clean.update(env)
        kw = {"base_url": f"https://{host}"} if host else {}
        with mock.patch.dict(os.environ, clean, clear=True):
            r = self.c.get(path, headers=_auth(), **kw)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_default_api_is_not_published(self):
        for path in ("/api/status", "/api/cameras"):
            j = self._get(path, {})
            self.assertEqual(j["rtsp_host"], "owlet-camera")
            self.assertEqual(j["rtsp_user"], cs.G2_USER_DEFAULT)
            self.assertEqual(j["rtsp_port"], str(cs.G_RTSP))
            self.assertIs(j["rtsp_published"], False)
            self.assertEqual(j["rtsp_mode"], "network")
            self.assertIs(j["api_published"], False)
            # the password itself is never in the payload
            self.assertNotIn("rtsp_pass", j)
            self.assertNotIn(cs.go2rtc_credentials()[1], str(j))

    def test_custom_rtsp_listen_port_is_advertised(self):
        for v, port in ((":9554", "9554"), ("0.0.0.0:9555", "9555"), ("[::]:9556", "9556")):
            with self.subTest(listen=v):
                for path in ("/api/status", "/api/cameras"):
                    j = self._get(path, {"GO2RTC_RTSP_LISTEN": v})
                    self.assertEqual(j["rtsp_host"], "owlet-camera")
                    self.assertEqual(j["rtsp_port"], port)
                    self.assertIs(j["rtsp_published"], False)

    def test_public_rtsp_port_uses_the_browser_host(self):
        for path in ("/api/status", "/api/cameras"):
            j = self._get(path, {"PUBLIC_RTSP_PORT": "18554", "GO2RTC_RTSP_LISTEN": ":9554"},
                          host="nas.example:8088")
            self.assertEqual(j["rtsp_host"], "nas.example")
            self.assertEqual(j["rtsp_port"], "18554")
            self.assertIs(j["rtsp_published"], True)
            self.assertEqual(j["rtsp_mode"], "published")
        j = self._get("/api/status", {"PUBLIC_RTSP_PORT": "18554"}, host="[2001:db8::5]:8088")
        self.assertEqual(j["rtsp_host"], "[2001:db8::5]")

    def test_invalid_public_rtsp_port_is_ignored(self):
        for v in ("", "abc", "0", "70000"):
            with self.subTest(port=v):
                j = self._get("/api/status", {"PUBLIC_RTSP_PORT": v})
                self.assertEqual(j["rtsp_host"], "owlet-camera")
                self.assertIs(j["rtsp_published"], False)

    def test_explicit_advertise_wins(self):
        env = {"OWLET_ADVERTISE_RTSP": "cams.example:28554",
               "PUBLIC_RTSP_PORT": "18554", "GO2RTC_RTSP_LISTEN": ":9554"}
        for path in ("/api/status", "/api/cameras"):
            j = self._get(path, env, host="nas.example:8088")
            self.assertEqual(j["rtsp_host"], "cams.example")
            self.assertEqual(j["rtsp_port"], "28554")
            self.assertIs(j["rtsp_published"], True)
            self.assertEqual(j["rtsp_mode"], "advertised")
        j = self._get("/api/status", {"OWLET_ADVERTISE_RTSP": "[2001:db8::7]:8554"})
        self.assertEqual((j["rtsp_host"], j["rtsp_port"]), ("[2001:db8::7]", "8554"))

    def test_fully_qualified_advertise_name_keeps_its_terminal_dot(self):
        """RFC 3986 3.2.2: a registered name may end in the root dot."""
        for v, host in (("cams.example.:8554", "cams.example."),
                        ("Cams.Example.:28554", "cams.example."),
                        ("localhost.:8554", "localhost.")):
            with self.subTest(advertise=v):
                j = self._get("/api/status", {"OWLET_ADVERTISE_RTSP": v,
                                              "PUBLIC_RTSP_PORT": "18554"})
                self.assertEqual(j["rtsp_mode"], "advertised")
                self.assertEqual(j["rtsp_host"], host)
                self.assertEqual(j["rtsp_port"], v.rsplit(":", 1)[1])
        # 253 characters before the root dot is the longest name allowed
        long_name = ".".join(["a" * 63] * 3 + ["a" * 61])
        self.assertEqual(len(long_name), 253)
        self.assertIsNotNone(webapp._check_advertised(long_name + ".:8554")[0])
        too_long = ".".join(["a" * 63] * 3 + ["a" * 62])
        self.assertEqual(len(too_long), 254)
        self.assertIsNone(webapp._check_advertised(too_long + ".:8554")[0])

    _MALFORMED = (
        "cams.example", ":8554", "cams.example:x", "user@cams.example:8554",
        "@cams.example:8554", "cams.example/x:8554", "cams.example:8554/live",
        "cams.example:8554?x=1", "cams.example?:8554", "cams.example:8554#f",
        "cams.example#:8554", "cams example:8554", "cams.example :8554",
        "cams\t.example:8554", "cams.example:0", "cams.example:70000",
        "cams.example:+8554", "cams.example:８５５４", "cams.exa%41mple:8554",
        "cams.example,b:8554", "2001:db8::7:8554", "2001:db8::7", "[2001:db8::7]",
        "[cams.example]:8554", "[192.0.2.7]:8554", "[2001:db8::7:8554",
        # a trailing "?" or "#" with nothing after it
        "cams.example:8554?", "cams.example:8554#", "cams.example:8554/",
        # host labels must be DNS labels
        "home_lab.example:8554", "-cams.example:8554", "cams-.example:8554",
        "cams..example:8554", ".cams.example:8554",
        # one terminal dot is allowed (fully qualified), but not two, nor a
        # dot standing in for the whole host
        "cams.example..:8554", ".:8554", "..:8554", ".cams.example.:8554",
        ("a" * 64) + ".example:8554", "cams.example:08554",
        "[fe80::1%eth0]:8554", "[2001:db8::7]:8554:1",
    )

    def test_malformed_advertise_falls_back(self):
        for v in self._MALFORMED:
            with self.subTest(advertise=v):
                j = self._get("/api/status", {"OWLET_ADVERTISE_RTSP": v})
                self.assertEqual(j["rtsp_mode"], "network")
                self.assertEqual(j["rtsp_host"], "owlet-camera")

    def test_malformed_advertise_falls_through_to_public_port(self):
        j = self._get("/api/status", {"OWLET_ADVERTISE_RTSP": "2001:db8::7:8554",
                                      "PUBLIC_RTSP_PORT": "18554"}, host="nas.example:8088")
        self.assertEqual((j["rtsp_mode"], j["rtsp_host"], j["rtsp_port"]),
                         ("published", "nas.example", "18554"))

    def test_malformed_advertise_is_logged_once(self):
        webapp._adv_rejected.clear()
        with mock.patch("builtins.print") as p:
            for _ in range(3):
                self._get("/api/status", {"OWLET_ADVERTISE_RTSP": "cams.example/x:8554"})
        lines = [c for c in p.call_args_list if "OWLET_ADVERTISE_RTSP" in str(c)]
        self.assertEqual(len(lines), 1)
        self.assertIn("path, query or fragment", str(lines[0]))

    def test_rejected_advertise_value_is_never_logged(self):
        secret = "S3ntinel-Pa55w0rd"
        values = (f"rtsp://viewer:{secret}@cams.example:8554",
                  f"viewer:{secret}@cams.example:8554",
                  f"{secret}@cams.example:8554",
                  f"cams.example:8554?password={secret}",
                  f"cams.example:8554#{secret}",
                  f"{secret}_x.example:8554",
                  f"{secret}.example:{secret}",
                  f"[{secret}]:8554")
        webapp._adv_rejected.clear()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            for v in values:
                with self.subTest(advertise=v):
                    j = self._get("/api/status", {"OWLET_ADVERTISE_RTSP": v})
                    self.assertEqual(j["rtsp_mode"], "network")
        logged = out.getvalue() + err.getvalue()
        self.assertEqual(logged.count("ignoring OWLET_ADVERTISE_RTSP"), len(values))
        for part in (secret, secret.lower(), "viewer", "cams.example"):
            self.assertNotIn(part, logged)

    def test_valid_advertise_forms(self):
        for v, want in (("[::1]:8554", ("[::1]", "8554")),
                        ("[2001:DB8::7]:18554", ("[2001:db8::7]", "18554")),
                        ("[::ffff:192.0.2.7]:8554", ("[::ffff:192.0.2.7]", "8554")),
                        ("192.0.2.7:8554", ("192.0.2.7", "8554")),
                        ("Cams.Example:1", ("cams.example", "1")),
                        ("cam-1.home-lab.example:65535", ("cam-1.home-lab.example", "65535"))):
            with self.subTest(advertise=v):
                j = self._get("/api/status", {"OWLET_ADVERTISE_RTSP": v})
                self.assertEqual(j["rtsp_mode"], "advertised")
                self.assertEqual((j["rtsp_host"], j["rtsp_port"]), want)

    def test_env_user_is_reported(self):
        j = self._get("/api/status", {"GO2RTC_USER": "frigate", "GO2RTC_PASS": "s3cret"})
        self.assertEqual(j["rtsp_user"], "frigate")
        self.assertNotIn("s3cret", str(j))

    def test_loopback_listen_is_not_published(self):
        for v in ("127.0.0.1:1984", "localhost:1984", "[::1]:1984", "127.0.0.2:1984"):
            with self.subTest(listen=v):
                self.assertIs(self._get("/api/status", {"GO2RTC_API_LISTEN": v})
                              ["api_published"], False)

    def test_wide_listen_is_published(self):
        for v in (":1984", "0.0.0.0:1984", "192.0.2.10:1984", "[::]:1984"):
            with self.subTest(listen=v):
                for path in ("/api/status", "/api/cameras"):
                    self.assertIs(self._get(path, {"GO2RTC_API_LISTEN": v})
                                  ["api_published"], True)


@unittest.skipUnless(HAVE_FLASK, "flask/requests not installed")
class StatusGateTests(WebappTestBase):
    """/api/status tells the panel which opt-in features the server refuses,
    so it can disable their controls up front (talk, sock probe)."""

    def _status(self, env):
        clean = {k: v for k, v in os.environ.items() if k != "OWLET_VITALS_POLL"}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            r = self.c.get("/api/status", headers=_auth())
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_vitals_poll_off_by_default(self):
        j = self._status({})
        self.assertIs(j["vitals_poll"], False)
        self.assertIn("talk_enabled", j)

    def test_vitals_poll_reported(self):
        self.assertIs(self._status({"OWLET_VITALS_POLL": "1"})["vitals_poll"], True)
        for v in ("0", "", "yes"):
            with self.subTest(value=v):
                self.assertIs(self._status({"OWLET_VITALS_POLL": v})["vitals_poll"], False)

    def test_probe_refused_matches_the_flag(self):
        with mock.patch.dict(os.environ, {"OWLET_VITALS_POLL": "0"}):
            r = self.c.post("/api/vitals/discover", headers=_auth())
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
