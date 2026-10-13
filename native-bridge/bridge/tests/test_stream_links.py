"""Stream-link fields in the control panel's status/cameras payloads.

The UI builds the RTSP copy URL from rtsp_host/rtsp_user/rtsp_port (which of
OWLET_ADVERTISE_RTSP / PUBLIC_RTSP_PORT / the go2rtc listen port applies is
reported as rtsp_mode and rtsp_published) and only
offers WebRTC/HLS links when api_published is true (go2rtc API widened with
GO2RTC_API_LISTEN). Skipped when Flask/requests aren't installed.
"""
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

    def test_malformed_advertise_falls_back(self):
        for v in ("cams.example", ":8554", "cams.example:x", "user@cams.example:8554",
                  "cams.example/x:8554"):
            with self.subTest(advertise=v):
                j = self._get("/api/status", {"OWLET_ADVERTISE_RTSP": v})
                self.assertEqual(j["rtsp_mode"], "network")
                self.assertEqual(j["rtsp_host"], "owlet-camera")

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


if __name__ == "__main__":
    unittest.main()
