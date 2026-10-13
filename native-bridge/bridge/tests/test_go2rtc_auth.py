"""go2rtc exposure hardening: the generated config must require auth and keep
the API on container loopback. Stdlib-only (PyYAML used when available).

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config_store as cs  # noqa: E402

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

HERE = os.path.dirname(os.path.abspath(__file__))


def _section(text, name):
    """Lines of one top-level YAML section of the rendered config."""
    out, cur = [], None
    for ln in text.splitlines():
        if ln and not ln.startswith(" "):
            cur = ln.rstrip(":")
            continue
        if cur == name and ln.strip():
            out.append(ln.strip())
    return out


_GO2RTC_ENV_RE = re.compile(r"\$\{([^}{]+)\}")


def _go2rtc_replace_env_vars(text, environ):
    """Python port of go2rtc 1.9.4 pkg/shell ReplaceEnvVars, which runs over
    the raw config text before YAML parsing: ${NAME} -> value if set, else
    ${NAME:default} -> default, else left as is."""
    def sub(m):
        key, sep, default = m.group(1).partition(":")
        if sep and not key:          # go2rtc only splits on a colon at index > 0
            key, sep, default = m.group(1), "", ""
        if key in environ:
            return environ[key]
        return default if sep else m.group(0)
    return _GO2RTC_ENV_RE.sub(sub, text)


class Go2rtcAuthTests(unittest.TestCase):
    def render(self, **kw):
        return cs.render_go2rtc([{"name": "owlet"}], candidate="", creds=("u1", "p-1_x"), **kw)

    def test_api_binds_loopback_and_requires_auth(self):
        api = _section(self.render(), "api")
        self.assertIn('listen: "127.0.0.1:1984"', api)
        self.assertIn('username: "u1"', api)
        self.assertIn('password: "p-1_x"', api)

    def test_rtsp_requires_auth(self):
        rtsp = _section(self.render(), "rtsp")
        self.assertIn('listen: ":8554"', rtsp)
        self.assertIn('username: "u1"', rtsp)
        self.assertIn('password: "p-1_x"', rtsp)

    def test_no_api_listener_on_all_interfaces_by_default(self):
        self.assertNotIn('listen: ":1984"', self.render())

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_parses_as_go2rtc_keys(self):
        d = yaml.safe_load(self.render())
        self.assertEqual(d["api"], {"listen": "127.0.0.1:1984", "username": "u1",
                                    "password": "p-1_x"})
        self.assertEqual(d["rtsp"]["username"], "u1")
        self.assertEqual(d["rtsp"]["password"], "p-1_x")
        self.assertIn("owlet", d["streams"])

    def test_creds_with_quotes_stay_one_yaml_scalar(self):
        text = cs.render_go2rtc([{"name": "owlet"}], candidate="", creds=('a"b', "c: d"))
        self.assertIn('username: "a\\"b"', text)
        self.assertIn('password: "c: d"', text)

    def test_ordinary_password_renders_unchanged(self):
        text = cs.render_go2rtc([{"name": "owlet"}], candidate="",
                                creds=("user-1", "Ab9_x-Yz.0~"))
        self.assertIn('username: "user-1"', text)
        self.assertIn('password: "Ab9_x-Yz.0~"', text)

    # Credentials that go2rtc's ${...} expansion would rewrite if emitted raw.
    DOLLAR_CREDS = ("u${X:}v", "a${HOME}b")

    def test_dollar_creds_leave_no_substitution_marker_in_raw_text(self):
        text = cs.render_go2rtc([{"name": "owlet"}], candidate="", creds=self.DOLLAR_CREDS)
        api, rtsp = _section(text, "api"), _section(text, "rtsp")
        for sec in (api, rtsp):
            self.assertIn('username: "u\\u0024{X:}v"', sec)
            self.assertIn('password: "a\\u0024{HOME}b"', sec)
        self.assertNotIn("${", text)       # exec sources included

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_dollar_creds_survive_go2rtc_env_substitution(self):
        text = cs.render_go2rtc([{"name": "owlet"}], candidate="", creds=self.DOLLAR_CREDS)
        d = yaml.safe_load(text)
        for sec in ("api", "rtsp"):
            self.assertEqual(d[sec]["username"], "u${X:}v")
            self.assertEqual(d[sec]["password"], "a${HOME}b")
        env = {"HOME": "/root", "X": "evil"}
        for environ in (env, {}):     # variables set, and unset (default branch)
            with self.subTest(environ=environ):
                d2 = yaml.safe_load(_go2rtc_replace_env_vars(text, environ))
                for sec in ("api", "rtsp"):
                    self.assertEqual(d2[sec]["username"].encode(), b"u${X:}v")
                    self.assertEqual(d2[sec]["password"].encode(), b"a${HOME}b")
                # the bash in the exec sources reaches go2rtc untouched too
                self.assertEqual(d2["streams"], d["streams"])
                self.assertEqual(d2["streams"]["owlet"][0], cs._exec_source("owlet"))

    def test_substitution_model_matches_go2rtc(self):
        """Sanity check of the re-implementation the test above relies on."""
        self.assertEqual(_go2rtc_replace_env_vars("${A}|${B:d}|${C}|${D:}", {"A": "1"}),
                         "1|d|${C}|")

    def test_exec_sources_contain_no_hash(self):
        """go2rtc cuts an exec source at the first '#' to parse its options
        (internal/exec/exec.go); a stray '#' would truncate the command."""
        for build in (cs._audio_source, cs._overlay_source):
            self.assertNotIn("#", build("owlet"))


class Go2rtcCredentialSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = mock.patch.multiple(
            cs, SECRETS_DIR=os.path.join(self.tmp.name, "secrets"),
            G2_SECRET_PATH=os.path.join(self.tmp.name, "secrets", "go2rtc.json"))
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"GO2RTC_USER": "frigate", "GO2RTC_PASS": "s3cret"}):
            self.assertEqual(cs.go2rtc_credentials(), ("frigate", "s3cret"))
        self.assertFalse(os.path.exists(cs.G2_SECRET_PATH))

    def _env(self, **kw):
        env = {k: v for k, v in os.environ.items() if k not in ("GO2RTC_USER", "GO2RTC_PASS")}
        env.update(kw)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_password_only_pairs_with_default_user(self):
        with self._env(GO2RTC_PASS="only-pass"):
            self.assertEqual(cs.go2rtc_credentials(), (cs.G2_USER_DEFAULT, "only-pass"))
        self.assertFalse(os.path.exists(cs.G2_SECRET_PATH))

    def test_user_only_pairs_with_stored_password(self):
        with self._env():
            _u, stored = cs.go2rtc_credentials()          # generated + persisted
        with self._env(GO2RTC_USER="frigate"):
            self.assertEqual(cs.go2rtc_credentials(), ("frigate", stored))
            self.assertEqual(cs.go2rtc_credentials(), ("frigate", stored))

    def test_user_only_with_nothing_stored_generates_password(self):
        with self._env(GO2RTC_USER="frigate"):
            u, p = cs.go2rtc_credentials()
            self.assertEqual(u, "frigate")
            self.assertEqual(cs.go2rtc_credentials(), ("frigate", p))
        with open(cs.G2_SECRET_PATH) as fh:
            self.assertEqual(json.load(fh)["password"], p)

    def test_generated_once_persisted_private_and_reused(self):
        env = {k: v for k, v in os.environ.items() if k not in ("GO2RTC_USER", "GO2RTC_PASS")}
        with mock.patch.dict(os.environ, env, clear=True):
            u1, p1 = cs.go2rtc_credentials()
            u2, p2 = cs.go2rtc_credentials()
        self.assertEqual((u1, p1), (u2, p2))
        self.assertEqual(u1, "owlet")
        self.assertGreaterEqual(len(p1), 20)
        with open(cs.G2_SECRET_PATH) as fh:
            self.assertEqual(json.load(fh)["password"], p1)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(cs.G2_SECRET_PATH).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(cs.SECRETS_DIR).st_mode), 0o700)


class FallbackConfigTests(unittest.TestCase):
    def test_baked_in_fallback_is_loopback_only(self):
        with open(os.path.join(HERE, "..", "go2rtc.yaml")) as fh:
            text = fh.read()
        self.assertIn('listen: "127.0.0.1:1984"', text)
        self.assertIn('listen: "127.0.0.1:8554"', text)
        self.assertNotIn('listen: ":', text)


class DeploymentFilesTests(unittest.TestCase):
    def test_compose_publishes_no_go2rtc_ports(self):
        with open(os.path.join(HERE, "..", "docker-compose.yml"), encoding="utf-8") as fh:
            live = [ln.split("#", 1)[0] for ln in fh if not ln.lstrip().startswith("#")]
        text = "".join(live)
        for port in ("1984", "1985", "8554", "8555"):
            self.assertNotIn(port, text)
        self.assertIn('"8088:8088"', text)

    def _compose_env(self):
        """{VAR: value-expression} from the compose service environment."""
        out = {}
        with open(os.path.join(HERE, "..", "docker-compose.yml"), encoding="utf-8") as fh:
            for ln in fh:
                code = ln.split("#", 1)[0].strip()
                m = re.match(r'^(OWLET_\w+|GO2RTC_\w+|PUBLIC_\w+):\s*"(.*)"$', code)
                if m:
                    out[m.group(1)] = m.group(2)
        return out

    def test_compose_policy_switches_are_overridable_with_safe_defaults(self):
        env = self._compose_env()
        for var, default in (("OWLET_REFRESH_KMS", "1"), ("OWLET_FORGET_ACCOUNT", "0"),
                             ("OWLET_VITALS_POLL", "0"), ("OWLET_WATCHDOG", "0"),
                             ("OWLET_TALK", "0")):
            with self.subTest(var=var):
                self.assertEqual(env.get(var), "${%s:-%s}" % (var, default))

    def test_compose_passwords_are_optional_so_the_generated_fallback_works(self):
        env = self._compose_env()
        self.assertEqual(env.get("GO2RTC_PASS"), "${GO2RTC_PASS:-}")
        self.assertEqual(env.get("OWLET_UI_PASS"), "${OWLET_UI_PASS:-}")
        with open(os.path.join(HERE, "..", "docker-compose.yml"), encoding="utf-8") as fh:
            self.assertNotIn(":?", fh.read())   # nothing is mandatory

    def test_compose_forwards_the_rtsp_endpoint_variables_it_recommends(self):
        # The header tells host-networked Frigate users to set these; they only
        # reach the container if the environment block forwards them.
        env = self._compose_env()
        for var in ("PUBLIC_RTSP_PORT", "OWLET_ADVERTISE_RTSP"):
            with self.subTest(var=var):
                self.assertEqual(env.get(var), "${%s:-}" % var)

    def test_compose_config_dir_matches_the_unraid_template(self):
        with open(os.path.join(HERE, "..", "docker-compose.yml"), encoding="utf-8") as fh:
            # live lines only: the header's migration note names the old path
            compose = "".join(ln for ln in fh if not ln.lstrip().startswith("#"))
        with open(os.path.join(HERE, "..", "..", "..", "unraid",
                               "owlet-bridge-native.xml"), encoding="utf-8") as fh:
            template = fh.read()
        m = re.search(r'Target="/config" Default="([^"]+)"', template)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "/mnt/user/appdata/owlet-camera/config")
        self.assertIn("${APPDATA:-/mnt/user/appdata/owlet-camera}/config:/config", compose)
        self.assertNotRegex(compose, r"appdata/owlet(?!-camera)")

    def test_unraid_template_publishes_no_go2rtc_ports(self):
        with open(os.path.join(HERE, "..", "..", "..", "unraid",
                               "owlet-bridge-native.xml"), encoding="utf-8") as fh:
            text = fh.read()
        for target in ('Target="1984"', 'Target="8554"', 'Target="8555"'):
            self.assertNotIn(target, text)


if __name__ == "__main__":
    unittest.main()
