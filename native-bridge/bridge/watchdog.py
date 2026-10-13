#!/usr/bin/env python3
"""
watchdog.py — self-heal a wedged Owlet P2P / RTSP session, gently first.

go2rtc (PID 1) and the TUTK exec source can wind up "connected" but delivering
ZERO frames — an expired cloud token, a stalled Kalay session, a half-open P2P
socket, or tutk_client's main thread blocked inside the TUTK lib / the stdout
pipe. go2rtc stays alive, so Docker never restarts the container and the RTSP
stream just hangs (Frigate/viewers see a dead camera).

This watchdog ffprobes each camera's local RTSP output on an interval and, when a
camera serves no video, climbs a ladder — cheapest fix first:

  stage 0  tutk_client.py's own stall supervisor (OWLET_STALL_TIMEOUT, ~30s) —
           runs inside the stream process, nothing to do here.
  stage 1  after OWLET_WATCHDOG_STALL seconds: SIGTERM that camera's
           tutk_client (clean session release), kill any orphaned producer
           ffmpeg, and REPLACE the camera's go2rtc stream object (PUT
           /api/streams) so a wedged go2rtc fan-out can't keep the stream
           dead; go2rtc relaunches ONLY that stream with a fresh KMS key.
           go2rtc, the web UI, the other cameras and this watchdog all stay
           up. Repeated OWLET_WATCHDOG_PRODUCER_RESTARTS times, one STALL
           window apart.
  stage 2  if the camera is still dead after those: kill go2rtc (PID 1) so
           Docker restarts the whole container (fresh login, fresh go2rtc).
           Rate-limited: a container restart is allowed at most once per
           OWLET_WATCHDOG_COOLDOWN seconds, doubling for every consecutive
           restart up to OWLET_WATCHDOG_COOLDOWN_MAX, and the counter only
           resets after OWLET_WATCHDOG_HEALTHY_RESET seconds of every camera
           being healthy. While on cooldown the watchdog keeps doing stage 1.

Why the ladder (field data, 2026-09-11, one camera, one day): 22 container
restarts, every one of them the same shape — video frames stop, audio keeps
flowing, tutk_client never logs its own "no video" timeout (its main thread is
blocked, so the in-process check can't run), and the old watchdog's only move
was a full container restart 3+ minutes later. Each restart bounced go2rtc, the
web UI, the vitals poller and a fresh Owlet cloud login, and the camera's
single P2P slot was still held by the dying session, so the relaunched process
often got a dud session and the cycle repeated ("amplifying wedges", TOT-33).
A producer-only restart fixes the same fault in seconds without any of that.

  OWLET_WATCHDOG=1                       enable (DEFAULT OFF until the ladder is
                                         validated on real hardware)
  OWLET_WATCHDOG_STALL=120               seconds of no frames between actions
  OWLET_WATCHDOG_INTERVAL=15             seconds between probes
  OWLET_WATCHDOG_GRACE=60                startup grace before the first probe
  OWLET_WATCHDOG_PRODUCER_RESTARTS=2     stage-1 attempts before stage 2
  OWLET_WATCHDOG_COOLDOWN=300            min seconds between container restarts
  OWLET_WATCHDOG_COOLDOWN_MAX=1800       cap for the doubling cooldown
  OWLET_WATCHDOG_HEALTHY_RESET=900       all-healthy seconds that reset the cooldown

STALL is deliberately longer than the in-process supervisor's full cycle
(detect ~30s + unblock 8s + relaunch + the camera's 20s session hold + a 25s
reconnect wait ≈ 90s) so this never pre-empts a recovery that is already in
flight; the old 45s value (TOT-33) did exactly that.

State (last container restart, consecutive count) persists in
<config>/vitals/watchdog.json across restarts; the same file carries a live
per-camera status the control panel shows under /api/status → "watchdog".

`python3 watchdog.py --check` probes once and exits 0 (all cameras healthy, or
none configured yet) / 1 (a camera is dead) — used by the Docker HEALTHCHECK.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time

import config_store as cs

STALL = int(os.environ.get("OWLET_WATCHDOG_STALL", "120"))
INTERVAL = int(os.environ.get("OWLET_WATCHDOG_INTERVAL", "15"))
GRACE = int(os.environ.get("OWLET_WATCHDOG_GRACE", "60"))
PRODUCER_RESTARTS = int(os.environ.get("OWLET_WATCHDOG_PRODUCER_RESTARTS", "2"))
COOLDOWN = int(os.environ.get("OWLET_WATCHDOG_COOLDOWN", "300"))
COOLDOWN_MAX = int(os.environ.get("OWLET_WATCHDOG_COOLDOWN_MAX", "1800"))
HEALTHY_RESET = int(os.environ.get("OWLET_WATCHDOG_HEALTHY_RESET", "900"))
# A stream process younger than this is a recovery in flight (the in-process
# stall supervisor sees a wedge ~60 s later than this probe does, because the
# pipe/socket buffers absorb a minute of video first); never restart it.
RECOVERY_GRACE = int(os.environ.get("OWLET_WATCHDOG_RECOVERY_GRACE", "90"))
PROBE_TIMEOUT = 15
STATE_PATH = os.path.join(cs.CONFIG_DIR, "vitals", "watchdog.json")


def log(m: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[watchdog]", m, flush=True)


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #
def _alive(name: str) -> bool:
    """True if the camera's local RTSP is currently serving video frames.
    Connecting as a consumer also makes go2rtc (re)launch an on-demand exec
    source that isn't running — so a probe right after a stage-1 restart is what
    brings the stream back even if no viewer is attached."""
    url = f"rtsp://127.0.0.1:{cs.G_RTSP}/{name}"
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
             "-rw_timeout", "12000000", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", url],
            capture_output=True, timeout=PROBE_TIMEOUT,
        )
        return r.returncode == 0 and b"video" in r.stdout
    except Exception:  # noqa: BLE001
        return False


def _names(cfg: dict | None = None) -> list[str]:
    """Cameras that can actually stream (see cs.streamable_names). NOT
    cs.camera_names() — that falls back to a placeholder `owlet` when nothing is
    configured yet, and probing it made the old watchdog "self-heal" a fresh
    install into a restart loop every couple of minutes until the first camera
    was added."""
    try:
        return cs.streamable_names(cfg)
    except Exception:  # noqa: BLE001
        return []


def _check_once() -> int:
    names = _names()
    if not names:
        return 0  # nothing configured yet -> report healthy
    return 1 if any(not _alive(n) for n in names) else 0


# --------------------------------------------------------------------------- #
# stage 1: restart one camera's stream process (go2rtc relaunches it)
# --------------------------------------------------------------------------- #
def _procs_ps() -> list[tuple[int, int, str]]:
    """(pid, ppid, cmdline) from `ps`, for when /proc can't be listed."""
    r = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True,
                       text=True, timeout=10)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out.append((int(parts[0]), int(parts[1]), parts[2].strip()))
    return out


def _procs() -> list[tuple[int, int, str]]:
    """(pid, ppid, cmdline) for every process /proc lets us read."""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
            with open(f"/proc/{pid}/stat") as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except Exception:  # noqa: BLE001
            continue
        out.append((pid, ppid, cmd))
    return out


def producer_pids(name: str, procs=None) -> tuple[list[int], list[int], list[int]]:
    """Find the go2rtc exec wrapper for one camera and its children.

    The generated exec is `bash -c '… python3 /app/tutk_client.py 2>>/config/
    tutk-<name>.log | ffmpeg …'`, so the wrapper is the only process whose
    cmdline carries BOTH `tutk_client.py` and that log name (a `tail` of the log
    from the UI has no tutk_client.py in it; the python child has no log name in
    its argv). Returns (python pids, other children e.g. ffmpeg, wrapper pids)."""
    markers = [f"tutk-{name}.log"]
    if name == cs.DEFAULT_CAM_NAME:
        markers.append("/config/tutk.log")  # baked-in single-cam fallback config
    procs = _procs() if procs is None else procs
    me = os.getpid()
    wrappers = {p for p, _pp, c in procs
                if p != me and "tutk_client.py" in c and any(m in c for m in markers)}
    kids = [(p, c) for p, pp, c in procs if pp in wrappers]
    pythons = [p for p, c in kids if "tutk_client.py" in c]
    others = [p for p, c in kids if "tutk_client.py" not in c]
    return pythons, others, sorted(wrappers)


def orphan_producer_ffmpegs(procs=None) -> list[int]:
    """ffmpeg processes that repackage a tutk_client pipe into go2rtc (`-f h264 -i -`
    ... `-f rtsp`) but whose parent is PID 1: their bash wrapper is gone, so the
    python feeding them is dead and they only hold go2rtc's producer slot open
    (seen 2026-09-12 10:08 after a hard exit). Always safe to kill."""
    procs = _procs() if procs is None else procs
    return sorted(p for p, pp, c in procs
                  if pp == 1 and c.startswith("ffmpeg") and "-f h264 -i -" in c and "-f rtsp" in c)


def reset_go2rtc_stream(name: str) -> bool:
    """Alias the camera's go2rtc stream name to a fresh, never-used spare stream
    (config_store.go2rtc_swap_to_spare). go2rtc 1.9 can wedge a stream's fan-out;
    it then never reads the producer again, keeps the dead producer listed and
    never relaunches the exec — only a container restart used to fix this
    (7-minute outage on 2026-09-12). Its API refuses new exec sources (PUT -> 400)
    but a PATCH alias to a config-defined spare works. False when no spare is
    left, so the ladder escalates to the container restart."""
    try:
        return cs.go2rtc_swap_to_spare(name, log=log) is not None
    except Exception as e:  # noqa: BLE001
        log(f"{name}: go2rtc spare swap failed: {e}")
        return False


def producer_age(name: str) -> float | None:
    """Seconds since the camera's tutk_client started, or None if none is running."""
    pythons, _o, _w = producer_pids(name)
    ages = []
    for p in pythons:
        try:
            with open(f"/proc/{p}/stat") as fh:
                start_ticks = int(fh.read().rsplit(")", 1)[1].split()[19])
            with open("/proc/uptime") as fh:
                up = float(fh.read().split()[0])
            ages.append(up - start_ticks / os.sysconf("SC_CLK_TCK"))
        except Exception:  # noqa: BLE001
            continue
    return min(ages) if ages else None


def _alive_pid(p: int) -> bool:
    """A zombie (not yet reaped by its parent) counts as gone."""
    try:
        with open(f"/proc/{p}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except Exception:  # noqa: BLE001
        return False


def _kill(pids, sig) -> None:
    for p in pids:
        try:
            os.kill(p, sig)
        except ProcessLookupError:
            pass
        except Exception as e:  # noqa: BLE001
            log(f"kill {p}: {e}")


def _gone(pids, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while True:
        if not any(_alive_pid(p) for p in pids):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.25)


def restart_producer(name: str) -> bool:
    """SIGTERM the camera's tutk_client so it releases the camera session
    cleanly (its SIGTERM handler runs stream_once's teardown), lets ffmpeg hit
    EOF and exit, and go2rtc relaunches the exec — with a fresh KMS key — on the
    next consumer connect (the keepalive, or our own next probe)."""
    pythons, others, wrappers = producer_pids(name)
    orphans = orphan_producer_ffmpegs()
    if orphans:
        log(f"{name}: killing orphaned producer ffmpeg {orphans} (feeder already dead, "
            "only holding go2rtc's producer slot)")
        _kill(orphans, signal.SIGTERM)
        if not _gone(orphans, 3):
            _kill(orphans, signal.SIGKILL)
    if not (pythons or others or wrappers):
        log(f"{name}: no live stream process to restart")
        # go2rtc may still think the (dead) producer is alive — swap the stream
        # object so the next consumer connect really relaunches the exec.
        reset_go2rtc_stream(name)
        return bool(orphans)
    log(f"{name}: SIGTERM tutk_client {pythons} (clean camera-session release)")
    _kill(pythons, signal.SIGTERM)
    if pythons and not _gone(pythons, 6):
        log(f"{name}: tutk_client ignored SIGTERM for 6s -> SIGKILL")
        _kill(pythons, signal.SIGKILL)
        _gone(pythons, 2)
    rest = others + wrappers
    if rest and not _gone(rest, 4):        # ffmpeg normally exits on EOF by itself
        _kill(rest, signal.SIGTERM)
        if not _gone(rest, 3):
            _kill(rest, signal.SIGKILL)
    # Even with the processes gone, a wedged go2rtc stream object would never
    # relaunch the exec; replace it so the next consumer connect starts fresh.
    reset_go2rtc_stream(name)
    log(f"{name}: stream process stopped and go2rtc stream replaced — relaunches on "
        "the next consumer connect (keepalive/probe, a few seconds)")
    return True


# Every generated stream wrapper's argv starts `set -a; OWLET_PRODUCER=<slug>;`
# (config_store._exec_source). Wrappers generated before that marker existed
# (and the baked-in fallback config) are recognised by their tutk log redirect.
PRODUCER_MARKER = "OWLET_PRODUCER="
_TUTK_LOG_REDIRECT = re.compile(r"2>>\s*\S*/tutk(?:-\S*?)?\.log")


def is_producer_wrapper(cmd: str) -> bool:
    """True for a go2rtc exec wrapper (the bash that runs tutk_client | ffmpeg).
    The tutk_client python child carries neither the marker nor the redirect in
    its argv, and a `tail` of a tutk log has no tutk_client.py in it."""
    if "tutk_client.py" not in cmd:
        return False
    return PRODUCER_MARKER in cmd or bool(_TUTK_LOG_REDIRECT.search(cmd))


def _process_table():
    """The process table from /proc, else from `ps`; None if neither works."""
    for source in (_procs, _procs_ps):
        try:
            return source()
        except Exception:  # noqa: BLE001
            continue
    return None


def stop_all_producers(names=None, timeout: float = 10.0, extra_names=()) -> list[int]:
    """Stop every camera's stream process tree (wrapper bash, tutk_client,
    ffmpeg) BEFORE go2rtc is restarted. go2rtc's /api/restart re-execs itself
    in place without stopping its exec sources, so their children used to be
    left running, still holding each camera's single P2P slot. SIGTERM the
    wrappers (their trap forwards it to tutk_client + ffmpeg), then SIGKILL
    anything still alive after `timeout`. Returns the PIDs that were targeted.

    Wrappers are found by their marker in the process table, NOT only by the
    camera names in the config: the web UI saves a rename/delete before it
    restarts go2rtc, so a producer still running under the OLD name is not in
    the new config but still holds that camera's P2P session. The union of the
    current config names and `extra_names` (the pre-save names the caller
    passes) is matched too, as the fallback for wrappers the marker check
    doesn't recognise."""
    try:
        names = list(names) if names is not None else cs.camera_names()
    except Exception:  # noqa: BLE001
        names = []
    names = list(dict.fromkeys(list(names) + [n for n in extra_names if n]))
    procs = _process_table()
    if procs is None:
        log("process table unreadable; cannot stop stream processes")
        return []
    me = os.getpid()
    wrappers = {p for p, _pp, c in procs if p != me and is_producer_wrapper(c)}
    for n in names:
        _py, _others, wr = producer_pids(n, procs)
        wrappers.update(wr)
    kids = {p for p, pp, _c in procs if pp in wrappers}
    allp = sorted(wrappers | kids)
    if not allp:
        return []
    _kill(sorted(wrappers), signal.SIGTERM)
    _kill(sorted(kids), signal.SIGTERM)
    if not _gone(allp, timeout):
        _kill(allp, signal.SIGKILL)
        _gone(allp, 2)
    return allp


# --------------------------------------------------------------------------- #
# stage 2: restart the whole container
# --------------------------------------------------------------------------- #
def restart_container() -> None:
    # go2rtc is PID 1 (start-bionic.sh execs it); killing it exits the container
    # so Docker's restart policy relaunches with a fresh login + fresh KMS creds.
    log("killing go2rtc (PID 1) so Docker restarts the container with fresh creds")
    try:
        subprocess.run(["pkill", "-TERM", "-x", "go2rtc"], timeout=10)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(6)
    try:  # belt-and-suspenders if go2rtc wasn't PID 1 for some reason
        os.kill(1, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #
class Watchdog:
    """Per-camera escalation state machine. Every dependency (probe, restart
    actions, clocks, state file, logger) is injectable so the ladder is unit-
    testable without a camera or a container."""

    def __init__(self, names=_names, alive=_alive,
                 restart_producer=restart_producer,
                 restart_container=restart_container,
                 producer_age=producer_age, recovery_grace: int = RECOVERY_GRACE,
                 now=time.monotonic, wall=time.time,
                 state_path: str | None = STATE_PATH, log=log,
                 stall: int = STALL, producer_restarts: int = PRODUCER_RESTARTS,
                 cooldown: int = COOLDOWN, cooldown_max: int = COOLDOWN_MAX,
                 healthy_reset: int = HEALTHY_RESET):
        self.names, self.alive = names, alive
        self.restart_producer, self.restart_container = restart_producer, restart_container
        self.producer_age, self.recovery_grace = producer_age, recovery_grace
        self.now, self.wall, self.log = now, wall, log
        self.state_path = state_path
        self.stall, self.max_producer = stall, producer_restarts
        self.cooldown, self.cooldown_max, self.healthy_reset = cooldown, cooldown_max, healthy_reset
        self.state = self._load_state()
        self.bad_since: dict[str, float] = {}      # name -> monotonic first-dead
        self.last_action: dict[str, float] = {}    # name -> monotonic last restart
        self.producer_count: dict[str, int] = {}   # name -> stage-1 restarts this outage
        self.cams: dict[str, dict] = {}            # name -> status for the state file
        self.all_healthy_since: float | None = None
        self.container_restarted = False

    # -- persisted state ---------------------------------------------------- #
    def _load_state(self) -> dict:
        st = {"container_restarts": {"last": None, "consecutive": 0}}
        if not self.state_path:
            return st
        try:
            with open(self.state_path) as fh:
                data = json.load(fh) or {}
            cr = data.get("container_restarts") or {}
            st["container_restarts"] = {
                "last": cr.get("last") if isinstance(cr.get("last"), (int, float)) else None,
                "consecutive": int(cr.get("consecutive") or 0),
            }
        except Exception:  # noqa: BLE001  (missing / corrupt -> fresh)
            pass
        return st

    def _write_state(self) -> None:
        if not self.state_path:
            return
        cr = self.state["container_restarts"]
        doc = {
            "updated": self.wall(),
            "cameras": self.cams,
            "container_restarts": dict(cr, allowed_at=self.container_allowed_at()),
            "config": {"stall": self.stall, "producer_restarts": self.max_producer,
                       "cooldown": self.cooldown, "cooldown_max": self.cooldown_max,
                       "healthy_reset": self.healthy_reset},
        }
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            cs._atomic_write(self.state_path, json.dumps(doc, indent=1))
        except Exception:  # noqa: BLE001  (read-only /config: run without persistence)
            pass

    def container_allowed_at(self) -> float:
        cr = self.state["container_restarts"]
        last, k = cr.get("last"), int(cr.get("consecutive") or 0)
        if not last or k <= 0:
            return 0.0
        return float(last) + min(self.cooldown * (2 ** (k - 1)), self.cooldown_max)

    # -- one probe cycle ---------------------------------------------------- #
    def tick(self) -> None:
        now = self.now()
        names = self.names()
        any_bad = False
        for name in names:
            if self.alive(name):
                if name in self.bad_since:
                    n = self.producer_count.get(name, 0)
                    self.log(f"{name}: recovered after {int(now - self.bad_since[name])}s "
                             f"({n} stream restart{'s' if n != 1 else ''})")
                self.bad_since.pop(name, None)
                self.last_action.pop(name, None)
                self.producer_count.pop(name, None)
                self.cams[name] = {"alive": True, "last_alive": self.wall(),
                                   "stalled_since": None, "producer_restarts": 0}
                continue
            any_bad = True
            self._handle_dead(name, now)
        for gone in [n for n in list(self.bad_since) if n not in names]:
            self.bad_since.pop(gone, None); self.last_action.pop(gone, None)
            self.producer_count.pop(gone, None)
        for gone in [n for n in list(self.cams) if n not in names]:
            self.cams.pop(gone, None)
        # A long all-healthy stretch forgives past container restarts, so the
        # next genuine wedge gets the short cooldown again.
        cr = self.state["container_restarts"]
        if names and not any_bad:
            if self.all_healthy_since is None:
                self.all_healthy_since = now
            elif cr.get("consecutive") and now - self.all_healthy_since >= self.healthy_reset:
                self.log(f"all cameras healthy for {self.healthy_reset}s — "
                         "container-restart cooldown reset")
                cr["consecutive"] = 0
        else:
            self.all_healthy_since = None
        self._write_state()

    def _handle_dead(self, name: str, now: float) -> None:
        since = self.bad_since.setdefault(name, now)
        dead = int(now - since)
        waited = now - self.last_action.get(name, since)
        n = self.producer_count.get(name, 0)
        cam = self.cams.setdefault(name, {})
        cam.update({"alive": False, "stalled_since": cam.get("stalled_since") or self.wall(),
                    "producer_restarts": n})
        if waited < self.stall:
            self.log(f"{name}: no video for {dead}s "
                     f"(next action in {int(self.stall - waited)}s)")
            return
        try:
            age = self.producer_age(name)
        except Exception:  # noqa: BLE001
            age = None
        if age is not None and age < self.recovery_grace:
            # The in-process supervisor (or a previous stage 1) just relaunched
            # this stream; give it time instead of killing the fresh process.
            self.log(f"{name}: stream process is only {int(age)}s old — recovery in "
                     f"flight, holding off ({self.recovery_grace}s grace)")
            self.last_action[name] = now - self.stall + self.recovery_grace
            return
        if n < self.max_producer:
            self.log(f"{name}: stalled {dead}s -> stage 1: restarting this camera's "
                     f"stream process ({n + 1}/{self.max_producer})")
            self._stage1(name, now)
            return
        allowed_at = self.container_allowed_at()
        wall = self.wall()
        if wall < allowed_at:
            self.log(f"{name}: still dead after {n} stream restarts; a container restart "
                     f"is on cooldown for {int(allowed_at - wall)}s more -> stage 1 again")
            self._stage1(name, now)
            return
        cr = self.state["container_restarts"]
        cr["last"] = wall
        cr["consecutive"] = int(cr.get("consecutive") or 0) + 1
        self.log(f"{name}: still dead after {n} stream restarts -> stage 2: restarting "
                 f"the container (restart #{cr['consecutive']} in this run of trouble; "
                 f"next one allowed no sooner than {int(self.container_allowed_at() - wall)}s "
                 "after it)")
        self.last_action[name] = now
        self._write_state()
        self.container_restarted = True
        self.restart_container()

    def _stage1(self, name: str, now: float) -> None:
        try:
            self.restart_producer(name)
        except Exception as e:  # noqa: BLE001
            self.log(f"{name}: stream restart failed: {e}")
        self.producer_count[name] = self.producer_count.get(name, 0) + 1
        self.last_action[name] = now
        self.cams[name]["producer_restarts"] = self.producer_count[name]


def main() -> None:
    # Off by default: the restart ladder has not been validated end-to-end
    # (see the reviews' notes on the ffprobe probe); opt in with OWLET_WATCHDOG=1.
    if os.environ.get("OWLET_WATCHDOG", "0") != "1":
        return
    time.sleep(GRACE)
    wd = Watchdog()
    cr = wd.state["container_restarts"]
    if cr.get("consecutive"):
        log(f"resumed after a self-healing container restart (#{cr['consecutive']}); "
            f"next container restart allowed in "
            f"{max(0, int(wd.container_allowed_at() - time.time()))}s, stream-level "
            "restarts are not rate-limited")
    while True:
        wd.tick()
        if wd.container_restarted:
            return  # PID 1 is going down; nothing more to do
        time.sleep(INTERVAL)


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(_check_once())
    main()
