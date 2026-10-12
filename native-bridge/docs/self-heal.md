# Self‑healing streams — how the bridge recovers a wedged camera

The Owlet cam allows exactly one P2P (Kalay/TUTK) session, and that session can
silently wedge: video frames stop arriving, audio keeps flowing, nothing errors.
go2rtc stays up, Docker sees a healthy process, and Frigate just shows a frozen
camera. This document describes the three recovery layers, why they are shaped
the way they are, and the field data behind the numbers.

## What a wedge actually looks like

Evidence from one camera, 2026‑09‑11 (Unraid, `:beta` image from July):

* **22 container restarts in one day**, every one of them the same shape in
  `tutk-owlet.log`: clean login, clean `IOTC_Connect_ByUIDEx -> 0`, clean AV
  start, `N frames forwarded` every 15 s … then the video counter simply stops.
  `[audio] N audio frames forwarded` and `[talk] alive` keep logging.
* Most stalls hit **60–100 s after a (re)connect** (e.g. 1342 frames at 21:41:30,
  nothing after); a few sessions ran for hours (2 h 1 min, 3 h 43 min) before
  stalling.
* **Not one `no video for 15s` line was ever logged.** That check lives in the
  frame loop on the main thread. Its absence means the main thread never came
  back from `avRecvFrameData2()` (or from the `stdout` write into ffmpeg) — the
  in‑process reconnect that TOT‑33 relied on could never run.
* The only thing that noticed was the container watchdog, 3+ minutes later, and
  its only tool was killing PID 1. That restarted go2rtc, the web UI, the vitals
  poller and did a fresh Owlet cloud login — and because the dying session still
  held the camera's single P2P slot for ~20 s, the relaunched process often got a
  dud session and the cycle repeated. Restarting *more* made it *worse*.

## What the first real stall showed (2026-09-12 10:07, new image)

Frigate restarted at 10:06 and the stream wedged as usual. The supervisor fired
30 s later with the diagnostic: the main thread was blocked in **`pipe_write`**
— not in the TUTK lib. ffmpeg had stopped draining the pipe because *its* socket
to go2rtc was full: `/proc/net/tcp` showed the producer connection in FIN_WAIT1
with 2.6 MB unsent, and go2rtc's `/api/streams` kept listing that dead producer
with **zero consumers**. go2rtc 1.9.4 had stopped reading the producer — a
fan-out wedged on a consumer that went away with the Frigate restart. Killing
our processes (hard exit, then the watchdog's stage 1) changed nothing: go2rtc
never relaunched the exec, and only the stage-2 container restart brought the
camera back (7 min dark). go2rtc's API refuses to create `exec:` sources dynamically (`PUT` → `New()` →
`Validate()` rejects them, HTTP 400), but `PATCH ?name=X&src=<existing stream>`
simply **aliases X to that stream's object**. So the generated config now carries
eight never-started identical spares per camera (`owlet_spare1..8`), and on a
wedge both layers point the camera's name at the first unused spare: consumers
land on a fresh object whose exec launches on demand, and the wedged object is
abandoned until the next restart. Verified on an isolated go2rtc 1.9.4: a
consumer that joined after the swap received frames from a new exec process
while the old object kept serving its old consumer. A deliberately non-reading
consumer did *not* reproduce the wedge on 1.9.4 or on the latest go2rtc, so the
trigger is something in the Frigate-restart reconnect sequence, not raw
back-pressure; the spare swap sidesteps it regardless of cause.

## The three layers

| Layer | Where | Trigger | Action | Typical time to video |
|---|---|---|---|---|
| **0 · frame loop** | `tutk_client.py` main thread | `avRecvFrameData2` returns *no data* for `OWLET_NO_VIDEO_TIMEOUT` (15 s) | returns, waits `OWLET_RECONNECT_WAIT` (25 s), reconnects with a fresh KMS key | ~45 s |
| **1 · stall supervisor** *(new)* | `tutk_client.py` side thread | no video frame forwarded for `OWLET_STALL_TIMEOUT` (30 s) **and** layer 0 didn't fire → the main thread is blocked | logs the main thread's kernel wait channel + a stack dump of every thread; calls `avClientStop()` to break a receive blocked in the lib → layer 0 takes over and reconnects cleanly; if the thread still isn't back after `OWLET_STALL_UNBLOCK_WAIT` (8 s) → aliases the camera's go2rtc stream to an unused spare (`PATCH /api/streams`), kills the ffmpeg on our stdout pipe (found by pipe inode), then `os._exit` so the stream relaunches | ~40–90 s |
| **2 · container watchdog, stage 1** *(new)* | `watchdog.py` | go2rtc's RTSP for the camera serves no video for `OWLET_WATCHDOG_STALL` (120 s) | `SIGTERM` **only that camera's** `tutk_client` (its handler releases the camera session cleanly), kill any orphaned producer ffmpeg, and alias the camera's go2rtc stream to an unused spare so a wedged fan-out cannot keep it dead (no spare left → escalate); go2rtc relaunches the exec on the next consumer connect — the watchdog's own next probe is a consumer. go2rtc, the UI and other cameras stay up. Repeated `OWLET_WATCHDOG_PRODUCER_RESTARTS` (2) times, 120 s apart. | ~2–4 min |
| **2 · container watchdog, stage 2** | `watchdog.py` | still dead after the stage‑1 attempts | kill go2rtc (PID 1) → Docker restarts the container (fresh login, fresh go2rtc). **Rate‑limited:** at most once per `OWLET_WATCHDOG_COOLDOWN` (300 s), doubling per consecutive restart up to `OWLET_WATCHDOG_COOLDOWN_MAX` (1800 s); the counter resets after `OWLET_WATCHDOG_HEALTHY_RESET` (900 s) of every camera healthy. While on cooldown the watchdog keeps doing stage 1. | ≥ 6 min |

The stage‑1 delay (120 s) is deliberately longer than a full layer‑1 cycle
(30 s detect + 8 s unblock + relaunch + the camera's 20 s slot hold + 25 s
reconnect wait ≈ 90 s) so the watchdog never pre‑empts a recovery that is already
in flight. TOT‑33's 45 s did exactly that.

## Where to look

* `/config/tutk-<camera>.log` — `[stall] …` lines from layer 1, including
  `wchan=` (e.g. `pipe_write` = ffmpeg stopped draining; a futex/socket wait =
  blocked inside the TUTK lib) and a Python stack dump of every thread. This is
  the diagnostic that was missing; a few of these will settle *where* the wedge
  is.
* Container log (`docker logs owlet-bridge-native`) — `[watchdog] …` lines:
  `no video for Ns (next action in …)`, `stage 1: restarting this camera's stream
  process (1/2)`, `stage 2: restarting the container`, `recovered after Ns (n
  stream restarts)`, cooldown notices.
* `/config/vitals/watchdog.json`, also served at `GET /api/status` → `watchdog`:
  per‑camera `alive` / `last_alive` / `stalled_since` / `producer_restarts`, plus
  `container_restarts.{last,consecutive,allowed_at}`.
* Docker health (`docker inspect … .State.Health`) — `python3 watchdog.py --check`
  is the HEALTHCHECK; it only *reports*, the ladder above does the healing.

## Also fixed on the way

The old watchdog probed `config_store.camera_names()`, which answers a
placeholder `owlet` when nothing is configured yet — so a fresh install with
no camera added was "healed" into a container restart every couple of
minutes. The watchdog now only probes cameras that can actually stream (a
saved UID, or a DSN plus an account login); with none, it idles and the
HEALTHCHECK reports healthy, as the docs always claimed.

## Tuning

All env vars are optional; defaults are what the field data suggested.

| Var | Default | Meaning |
|---|---|---|
| `OWLET_SPARE_STREAMS` | `8` | identical never-started go2rtc spare streams per camera, one consumed per wedge recovery between container restarts |
| `OWLET_STALL_TIMEOUT` | `30` | seconds without a forwarded video frame before layer 1 acts (kept ≥ `OWLET_NO_VIDEO_TIMEOUT` + 10) |
| `OWLET_STALL_UNBLOCK_WAIT` | `8` | seconds to give `avClientStop()` before the hard exit |
| `OWLET_WATCHDOG` | `0` | off by default until validated; `1` enables the container watchdog |
| `OWLET_WATCHDOG_STALL` | `120` | seconds of no frames between watchdog actions |
| `OWLET_WATCHDOG_INTERVAL` | `15` | seconds between probes |
| `OWLET_WATCHDOG_GRACE` | `60` | startup grace before the first probe |
| `OWLET_WATCHDOG_RECOVERY_GRACE` | `90` | a stream process younger than this is a recovery in flight; the watchdog holds off instead of killing it (its probe sees a wedge ~60 s before the process does) |
| `OWLET_WATCHDOG_PRODUCER_RESTARTS` | `2` | stage‑1 attempts before a container restart |
| `OWLET_WATCHDOG_COOLDOWN` | `300` | minimum seconds between container restarts |
| `OWLET_WATCHDOG_COOLDOWN_MAX` | `1800` | cap for the doubling cooldown |
| `OWLET_WATCHDOG_HEALTHY_RESET` | `900` | all‑healthy seconds that reset the cooldown |

The ladder is unit‑tested without a camera:
`python3 -m unittest discover -s native-bridge/bridge/tests -v` (CI runs it
before every image build).

## Open question the new logging will answer

TOT‑35 added `IOTC_Session_Check()` logging (`mode=P2P|Relay|LAN`) on the theory
that the short‑lived sessions are the relay‑mode ones. **That check is now opt‑in
(`OWLET_SESSION_CHECK=1`) and off by default:** its first production run
(2026‑09‑12) crashed the stream process after every connect because the Kalay
lib fills a much larger struct than the two‑uint one it was handed, and go2rtc
relaunched the crashing exec ~60×/min until the Owlet KMS rate‑limited the
account. When enabled it now uses an oversized buffer and logs the raw bytes. The stall supervisor's
`wchan` + stack dumps add the other half: *where* the thread is stuck. Once a
dozen real stalls have been logged with both, correlate them; if relay mode is
the culprit, `OWLET_REQUIRE_P2P=1` becomes the fix rather than the recovery.
