"""Stream-link fields in the control panel's status/cameras payloads.

The UI builds the RTSP copy URL from rtsp_host/rtsp_user/rtsp_port and only
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

    def _get(self, path, env):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("GO2RTC_API_LISTEN", "GO2RTC_USER", "GO2RTC_PASS")}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            r = self.c.get(path, headers=_auth())
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_default_api_is_not_published(self):
        for path in ("/api/status", "/api/cameras"):
            j = self._get(path, {})
            self.assertEqual(j["rtsp_host"], "owlet-camera")
            self.assertEqual(j["rtsp_user"], cs.G2_USER_DEFAULT)
            self.assertEqual(j["rtsp_port"], str(cs.G_RTSP))
            self.assertIs(j["api_published"], False)
            # the password itself is never in the payload
            self.assertNotIn("rtsp_pass", j)
            self.assertNotIn(cs.go2rtc_credentials()[1], str(j))

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
