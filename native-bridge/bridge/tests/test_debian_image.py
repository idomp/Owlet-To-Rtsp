"""Debian image (Dockerfile + entrypoint.sh): ships every bridge module and
starts go2rtc on the generated, authenticated config like start-bionic.sh.

The entrypoint test runs the REAL script under bash with /config and /app
redirected into a temp dir and stub `python3`/`go2rtc`/`openssl` executables
(Linux only; no camera, no go2rtc).

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.normpath(os.path.join(HERE, ".."))


def _read(name):
    with open(os.path.join(BRIDGE, name), encoding="utf-8") as fh:
        return fh.read()


class DockerfileTests(unittest.TestCase):
    def test_copies_the_whole_bridge_directory(self):
        self.assertIn("COPY . /app/", _read("Dockerfile"))

    def test_dockerignore_keeps_tests_and_tutk_libs_out(self):
        lines = [ln.strip() for ln in _read(".dockerignore").splitlines()]
        self.assertIn("tests/", lines)
        self.assertIn("libs/**/*.so", lines)

    def test_entrypoint_uses_generated_config(self):
        src = _read("entrypoint.sh")
        self.assertIn("python3 /app/render_streams.py", src)
        self.assertIn('exec go2rtc -config "$GO2RTC_CFG"', src)
        self.assertNotIn("exec go2rtc -config /app/go2rtc.yaml", src)


def _stub(path, body):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#!/bin/bash\n" + body + "\n")
    os.chmod(path, 0o755)


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"),
                     "needs Linux bash")
class EntrypointRunTests(unittest.TestCase):
    def _run(self, render_ok):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg, app, bindir = (os.path.join(tmp, d) for d in ("config", "app", "bin"))
        for d in (cfg, app, bindir):
            os.makedirs(d)
        # a file an older version left world-readable
        legacy = os.path.join(cfg, "owlet.yaml")
        with open(legacy, "w") as fh:
            fh.write("x: 1\n")
        os.chmod(legacy, 0o644)

        script = os.path.join(tmp, "entrypoint.sh")
        src = _read("entrypoint.sh").replace("/config", cfg).replace("/app/", app + "/")
        with open(script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src)

        args_file = os.path.join(tmp, "go2rtc.args")
        _stub(os.path.join(bindir, "python3"),
              'case "$1" in\n'
              f'  */render_streams.py) [ "$RENDER_OK" = 1 ] && echo gen > "{cfg}/go2rtc.gen.yaml"'
              ' && exit 0; exit 1;;\n'
              '  *) exit 0;;\nesac')
        _stub(os.path.join(bindir, "go2rtc"), f'echo "$@" > "{args_file}"')
        _stub(os.path.join(bindir, "openssl"), "exit 1")
        env = {"PATH": bindir + ":/usr/bin:/bin", "RENDER_OK": "1" if render_ok else "0"}
        r = subprocess.run(["bash", script], env=env, timeout=30,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        with open(args_file) as fh:
            return fh.read().strip(), cfg, app, legacy

    def test_generated_config_selected_when_render_succeeds(self):
        args, cfg, _app, legacy = self._run(render_ok=True)
        self.assertEqual(args, f"-config {cfg}/go2rtc.gen.yaml")
        self.assertEqual(stat.S_IMODE(os.stat(legacy).st_mode) & 0o077, 0)
        self.assertTrue(os.path.isfile(os.path.join(cfg, "owlet.env")))

    def test_baked_fallback_when_render_fails(self):
        args, _cfg, app, _legacy = self._run(render_ok=False)
        self.assertEqual(args, f"-config {app}/go2rtc.yaml")


if __name__ == "__main__":
    unittest.main()
