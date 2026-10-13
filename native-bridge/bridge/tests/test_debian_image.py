"""Debian image (Dockerfile + entrypoint.sh): ships every bridge module and
starts go2rtc on the generated per-camera config like start-bionic.sh.

The entrypoint test runs the REAL script under bash with /config and /app
redirected into a temp dir and stub `python3`/`go2rtc`/`openssl` executables
(Linux only; no camera, no go2rtc).

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.normpath(os.path.join(HERE, ".."))


def _read(name):
    with open(os.path.join(BRIDGE, name), encoding="utf-8") as fh:
        return fh.read()


def _docker_rule_regex(pattern):
    """One .dockerignore pattern as a regex, following Docker's documented
    semantics (moby/patternmatcher): matched against the slash-separated path
    from the context root, leading/trailing slashes dropped, `*` and `?` never
    cross a `/`, `**` matches any number of directories (including none)."""
    pat = pattern.strip("/")
    out, i = "", 0
    while i < len(pat):
        ch = pat[i]
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pat.startswith("**", i):
            out += ".*"
            i += 2
            continue
        if ch == "*":
            out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        elif ch == "[":
            j = pat.find("]", i)
            out += pat[i:j + 1] if j > i else re.escape(ch)
            i = (j if j > i else i)
        else:
            out += re.escape(ch)
        i += 1
    return re.compile(out + r"\Z")


def _ignore_rules():
    rules = []
    for ln in _read(".dockerignore").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        neg = ln.startswith("!")
        rules.append((neg, _docker_rule_regex(ln[1:] if neg else ln)))
    return rules


def _ignored(path, rules=None):
    """True when `path` (relative to the bridge dir) stays out of the build
    context: the last rule matching the path or one of its parent directories
    decides, like `docker build` does."""
    rules = _ignore_rules() if rules is None else rules
    parts = path.strip("/").split("/")
    prefixes = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    out = False
    for neg, rx in rules:
        if any(rx.match(p) for p in prefixes):
            out = not neg
    return out


# Things that can sit in the bridge directory on a user's machine and must
# never reach an image built with `COPY . /app/`.
SENSITIVE = (
    ".env", ".env.local", "owlet.env", "secrets.env", "sub/dir/.env",
    "secrets/credentials.json", "state.json", "config/state.json",
    "config/owlet.yaml", "config/secrets/ui.json", "owlet.yaml",
    "go2rtc.gen.yaml", "cameras/owlet.json",
    # sidecars the bridge itself writes next to those files
    "owlet.yaml.tmp", "owlet.yaml.bad", "owlet.env.tmp", "vitals/snapshot.json.tmp",
    "docker-compose.override.yaml", "compose.override.yaml",
    "cert.pem", "ssl/key.pem", "tls.key", "server.crt", "store.p12",
    "owlet.apk", "Owlet.apkm", "owlet.xapk", "owlet.apks", "uploads/a1b2.apk",
    "libs/Owlet.zip", "owlet-bundle.zip",
    "bundle/lib/x86_64/libIOTCAPIs.so", "libs/x86_64/libIOTCAPIs.so", "libAVAPIs.so",
    ".venv/bin/python", ".venv-cam/lib/site.py", "venv/bin/python",
    "tutk.log", "tutk-owlet.log", "logs/watchdog.log",
    "tests/test_debian_image.py", "__pycache__/webapp.cpython-312.pyc", "webapp.pyc",
    "docker-compose.override.yml", "frame01.png",
)


def _bridge_files():
    """Every file under the bridge dir, as context-relative paths."""
    out = []
    for root, dirs, files in os.walk(BRIDGE):
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), BRIDGE).replace(os.sep, "/"))
    return out


class DockerignoreTests(unittest.TestCase):
    def test_matcher_follows_docker_semantics(self):
        rules = [(False, _docker_rule_regex("*.env"))]
        self.assertTrue(_ignored("a.env", rules))
        self.assertFalse(_ignored("sub/a.env", rules))       # no `**/`: root only
        rules = [(False, _docker_rule_regex("**/*.env"))]
        self.assertTrue(_ignored("a.env", rules))
        self.assertTrue(_ignored("sub/a.env", rules))
        rules = [(False, _docker_rule_regex("secrets/"))]
        self.assertTrue(_ignored("secrets/x/y.json", rules))  # dir excludes contents
        rules = [(False, _docker_rule_regex("*.json")),
                 (True, _docker_rule_regex("keep.json"))]
        self.assertFalse(_ignored("keep.json", rules))        # last match wins

    def test_sensitive_paths_are_excluded(self):
        rules = _ignore_rules()
        for path in SENSITIVE:
            with self.subTest(path=path):
                self.assertTrue(_ignored(path, rules), f"{path} would be baked into the image")

    def test_source_stays_in_the_context(self):
        # The Debian image needs every module, template and script.
        rules = _ignore_rules()
        for path in _bridge_files():
            if path.startswith("tests/") or path.endswith((".so", ".pyc")):
                continue
            if path.endswith((".py", ".sh", ".js", ".css", ".html", ".txt")) \
                    or path in ("go2rtc.yaml", "Dockerfile", "Dockerfile.bionic"):
                with self.subTest(path=path):
                    self.assertFalse(_ignored(path, rules), f"{path} is source but ignored")

    def test_bionic_copy_list_is_not_ignored(self):
        # Dockerfile.bionic copies an explicit list; every file in it must
        # survive the shared .dockerignore or that build breaks.
        rules = _ignore_rules()
        sources = []
        for ln in _read("Dockerfile.bionic").splitlines():
            ln = ln.strip()
            if not ln.startswith("COPY ") or "--from=" in ln:
                continue
            sources.extend(ln.split()[1:-1])
        self.assertIn("webapp.py", sources)
        self.assertIn("templates", sources)
        for src in sources:
            full = os.path.join(BRIDGE, src)
            self.assertTrue(os.path.exists(full), f"COPY source {src} missing")
            if os.path.isdir(full):
                files = [p for p in _bridge_files() if p.startswith(src.rstrip("/") + "/")]
                self.assertTrue(files, f"{src}/ is empty")
            else:
                files = [src]
            for f in files:
                with self.subTest(path=f):
                    self.assertFalse(_ignored(f, rules), f"Dockerfile.bionic copies {f}")


class DockerfileTests(unittest.TestCase):
    def test_copies_the_whole_bridge_directory(self):
        # Why the .dockerignore above matters.
        self.assertIn("COPY . /app/", _read("Dockerfile"))

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
            return fh.read().strip(), cfg, app

    def test_generated_config_selected_when_render_succeeds(self):
        args, cfg, _app = self._run(render_ok=True)
        self.assertEqual(args, f"-config {cfg}/go2rtc.gen.yaml")
        self.assertTrue(os.path.isfile(os.path.join(cfg, "owlet.env")))

    def test_baked_fallback_when_render_fails(self):
        args, _cfg, app = self._run(render_ok=False)
        self.assertEqual(args, f"-config {app}/go2rtc.yaml")


if __name__ == "__main__":
    unittest.main()
