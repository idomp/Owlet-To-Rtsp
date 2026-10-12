<div align="center">

<img src="assets/logo.png" width="140" alt="Owlet-To-RTSP logo">

# Owlet‑To‑RTSP

**Turn an Owlet Cam into a plain RTSP stream for Frigate, Home Assistant, or any RTSP client — running entirely on your own server.**

No phone. No Android emulator. No cloud relay. It talks to the camera directly over ThroughTek/Kalay (TUTK), the [docker‑wyze‑bridge](https://github.com/mrlt8/docker-wyze-bridge) way.

![image](https://img.shields.io/badge/ghcr.io-owlet--bridge--native-2496ED?logo=docker&logoColor=white)
![platform](https://img.shields.io/badge/runs%20on-Unraid%20%C2%B7%20x86__64-39d98a)
![output](https://img.shields.io/badge/output-RTSP%20%C2%B7%20WebRTC%20%C2%B7%20HLS-3fa7ff)
![status](https://img.shields.io/badge/status-working-39d98a)

</div>

---

> **Status: working & verified.** Tested end‑to‑end on an **Owlet Dream Duo 3.0** → Unraid (x86‑64) → **Frigate**: live H.264 pulled straight off the camera over Kalay, repackaged to RTSP locally, recording 24/7. As far as we know, this is the **first working local Owlet → RTSP bridge.**

## What you get

- 🦉 **A single container** — drop in your Owlet login, get `rtsp://<host>:8554/owlet`.
- 🌐 **A friendly web UI** — status dashboard, in‑browser APK upload, one‑click "Add & connect," live logs.
- 🏠 **100% local video** — once connected, frames go camera → bridge directly over your LAN; they never touch Owlet's or ThroughTek's servers.
- 🎙️ **Two‑way audio** — hold‑to‑talk into the room (from any browser), play uploaded MP3/WAV/AAC files, or trigger the camera's **built‑in lullabies and soothing sounds** (with a hardware sleep timer) — all reverse‑engineered from the app, byte‑for‑byte.
- 🌡️ **Room sensors → Home Assistant** — temperature (live, per video frame), humidity, sound level, brightness, Wi‑Fi RSSI, auto‑published via MQTT discovery.
- 🎛️ **Camera controls** — speaker volume (0–100 %, with live read‑back), status indicator light on/off, model/firmware info.
- 📹 **Multiple cameras** — add as many cams as you have, each with its own RTSP stream.
- 🔁 **Self‑healing** — keeps the camera session alive 24/7 and reconnects automatically after a Frigate restart or a power cut.
- 🚫 **Nothing proprietary shipped** — you supply your own copy of the Owlet app; the bridge extracts the TUTK libraries from it for you.

> Everything the bridge does is replicated from the official Owlet app's own protocol (decompiled and verified) — no cloud relay, no emulator. The one exception is **motion/sound *push* alerts**, which the app delivers via Firebase cloud messaging, not the local camera link — so those can't be reproduced locally (the live **sound level** is published instead, which you can threshold in Home Assistant).

---

## How it works

The Owlet Cam speaks **ThroughTek / Kalay (TUTK)** — the same P2P video platform the Wyze cameras use. This bridge talks that protocol directly:

```
 Owlet account (email / password / camera DSN)
        │
        ▼
  webapp.py ── control panel :8088
        │   • Owlet cloud login   (Firebase → Ayla SSO → Ayla)
        │   • camera‑key fetch     (camera‑kms.owletdata.com → UID / AuthKey / password)
        ▼  /config/owlet.env
  tutk_client.py ── Kalay/TUTK P2P, DTLS ──▶ Owlet Cam
        │   • licenses + inits the TUTK SDK   (region US, app license key)
        │   • IOTC_Connect_ByUIDEx            (authKey)
        │   • avClientStartEx                 (DTLS AV login)
        │   • avRecvFrameData2 loop ─ raw H.264 → stdout
        │   • two‑way talk: avServStart2 on free channel ─ AAC → speaker
        │   • room sensors: temp per frame + GET_REALTIME_DATA IOCTL
        ▼
  ffmpeg + go2rtc ──▶  RTSP :8554  ·  WebRTC :8555  ·  HLS / preview UI :1984
```

Everything is configured in the browser. The **Add & connect** button runs the *real* Owlet login, fetches the camera's Kalay credentials, and streams every step to a live log.

---

## Quick start (Unraid)

> ⚠️ **Networking matters.** Use **Bridge** networking — *not* a `br0`/macvlan custom network. On macvlan the bridge gets its own LAN IP that the Unraid host (and host/bridge‑networked containers like Frigate) **cannot reach**, so Frigate can't pull the stream. Bridge networking with published ports works for everything (the camera dials *outbound*, so P2P is fine — same as docker‑wyze‑bridge).

### 1 · Add the container

Docker → **Add Container** → paste the template URL:

```
https://raw.githubusercontent.com/btoth525/Owlet-To-Rtsp/main/unraid/owlet-bridge-native.xml
```

It is **secure by default**: only the control panel is published.

| Service | Host port | Container port | Notes |
|---|---|---|---|
| Control panel | `8088` | `8088` | HTTP Basic auth |
| go2rtc API / video UI | *not published* | `1984` | bound to container loopback: the API can rewrite go2rtc's config and run commands |
| **RTSP** | *not published* | `8554` | Frigate pulls it over a **shared Docker network**, with go2rtc credentials |
| WebRTC | *not published* | `8555` | |

Create a user‑defined network once (`docker network create owlet-net`) and set **Network Type = `owlet-net`** on both this container and Frigate. Set **`GO2RTC_PASS`** in the template (or leave it blank and read the generated one from `/config/secrets/go2rtc.json`, mode 0600).

Two volumes: **`/config`** (settings) and **`/app/libs`** (the TUTK libraries).

### 2 · Make `/config` writable

The container runs as a non‑root (Termux) user, so the Unraid appdata folder must be writable by it:

```bash
chmod -R 777 /mnt/user/appdata/owlet/config
```

### 3 · Provide the TUTK libraries (in the browser)

The proprietary ThroughTek `.so` libraries aren't shipped — you supply your own copy of the Owlet app. Open the control panel at **`http://<unraid-ip>:8088`**, go to the **🧩 TUTK libraries** card, click **Upload & extract**, and pick your Owlet **`.apkm`** (or `.apk`). The bridge pulls the five x86‑64 libraries out of it for you.

> These libraries are Android binaries linked against Bionic libc, so glibc can't load them. The image runs a **Termux (Bionic) userspace** so they load on a normal x86‑64 Linux kernel — no Android, no `binder`/`ashmem`, no privileged mode.

### 4 · Sign in and add your camera

In the control panel:

1. Enter your Owlet **email / password** and **region**, then click **Save account**.
2. Enter your camera **DSN** — it looks like **`OCD…`** (that's a **capital letter O**, not a zero). Find it in the Owlet app under the camera, or on the camera base. Give it a name (e.g. `nursery`).
3. Click **＋ Add & connect** — it logs in, fetches the camera UID / AuthKey, and starts the stream automatically.

### 5 · Use it

- Control panel: `https://<unraid-ip>:8088`
- **RTSP (from Frigate on `owlet-net`): `rtsp://<GO2RTC_USER>:<GO2RTC_PASS>@owlet-bridge-native:8554/<camera-name>`** ← point Frigate here.
- The go2rtc video UI is not reachable from the LAN any more. If you really want it, set `GO2RTC_API_LISTEN=:1984` and publish the port yourself; go2rtc then requires `GO2RTC_USER`/`GO2RTC_PASS` for every non‑loopback client.

> **Two‑way talk requires HTTPS.** The browser's microphone API only works on a secure origin. Open the panel on `https://` (self‑signed cert — accept the warning once) and the 🎤 hold‑to‑talk button becomes active.

---

## Feed it into Frigate

Add the bridge as a go2rtc stream, then a record‑only camera. **Disable birdseye** for it — Frigate's birdseye decodes the full 1440p feed continuously and will spike your CPU otherwise:

```yaml
go2rtc:
  streams:
    Owlet_Cam:
      # the bridge over the shared Docker network (use your camera name and GO2RTC_PASS)
      - rtsp://owlet:<GO2RTC_PASS>@owlet-bridge-native:8554/nursery

cameras:
  Owlet_Cam:
    enabled: true
    birdseye:
      enabled: false                          # don't decode 1440p just for the mosaic
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/Owlet_Cam
          input_args: preset-rtsp-restream
          roles: [record]
    detect:
      enabled: false
    record:
      enabled: true
```

> The Owlet stream is **video‑only** (no audio track in the RTSP stream), so don't add an `audio` role. The first connect takes ~10 s (P2P + DTLS handshake), so Frigate's ffmpeg may retry once — that's normal.

---

## Run it without Unraid

**docker run:**
```bash
docker network create owlet-net        # once; attach Frigate to it as well
docker run -d --name owlet-bridge --restart unless-stopped --network owlet-net \
  -p 8088:8088 \
  -e GO2RTC_USER=owlet -e GO2RTC_PASS='<long random password>' \
  -v /path/to/owlet/config:/config \
  -v /path/to/owlet/libs:/app/libs \
  ghcr.io/btoth525/owlet-bridge-native:latest
```

**docker compose:** see [`native-bridge/bridge/docker-compose.yml`](native-bridge/bridge/docker-compose.yml).

The `PUBLIC_*` vars just tell the UI which host ports you mapped, so the copy‑URLs and the "Open video UI" button point to the right place.

---

## Features in detail

### Self‑healing streams
The camera's single P2P session can silently wedge (video stops, audio keeps
flowing). Three layers put it back without you touching anything: the stream
process notices its own frame clock stopping (~30 s) and reconnects; the
container watchdog restarts just that camera's stream process if it doesn't
(2 min); and only as a last, rate‑limited resort restarts the container. Live
counters are in `/api/status` → `watchdog`. Details, timings and the field data
behind them: [docs/self-heal.md](native-bridge/docs/self-heal.md).

### Two‑way audio (talk‑back)

Open the **🎙️ Talk & sounds** card. The **🎤 Hold to talk** button captures your microphone in real time, encodes it to AAC‑LC 8 kHz mono (matching the format the Owlet app uses), and pushes it to the camera's speaker over the Kalay AV server channel.

The exact transport, reverse‑engineered from the app:

1. `IOTC_Session_Get_Free_Channel` — pick a free channel on the live session
2. `avServExit(session, chan)` — clear any lingering AV state on that channel
3. Send `SPEAKERSTART` IOCTL (0x350) carrying the channel number
4. Wait for the camera's `0x600b6` ack
5. `avServStart2(session, "", "", timeout, 0, chan)` — accept the camera's inbound connection
6. Stream AAC frames via `avSendAudioData`

### Audio files & lullabies

- **Play a file** — drag MP3/WAV/AAC/OGG files onto the drop zone, then click to play. The file is transcoded to AAC‑LC 8 kHz and sent to the camera in real time.
- **Sleep timer** — hardware‑enforced by the camera (15/30/45/60/120 min).
- **Loop** — repeats the current file or lullaby.
- **Built‑in camera sounds** — click **🔄 Load camera sounds** to list the soothing sounds stored on the camera itself (the same ones the Owlet app uses). Click any to play; **⏹ Stop** to silence.

### Room sensors

The bridge publishes five sensor values:

| Sensor | Source | Update rate |
|---|---|---|
| Temperature | Embedded in every video frame | ~1 s (live) |
| Humidity | `GET_REALTIME_DATA` IOCTL | every 2 s |
| Sound level (dB) | `GET_REALTIME_DATA` IOCTL | every 2 s |
| Brightness | `GET_REALTIME_DATA` IOCTL | every 2 s |
| Wi‑Fi RSSI | `GET_REALTIME_DATA` IOCTL | every 2 s |

Temperature rides every single video frame in the extended frame‑info struct — it updates at the camera's frame rate and is throttled to ~1 s writes so the sidecar isn't written 25× per second.

### Home Assistant / MQTT

Enable MQTT in the **🏠 Home Assistant** card. Every sensor above is published with Home Assistant MQTT discovery — entities appear automatically in your HA instance (no manual YAML). Temperatures are published in °F.

### Camera controls

All controls are in the **🎙️ Talk & sounds** card:

- **🔊 Speaker volume** — drag the slider (0–100 %). The current device volume is read back from the camera on connect and shown next to the slider.
- **💡 Status light** — toggle the camera's blue indicator LED on or off.
- **ℹ️ Camera info** — reads model, vendor, and firmware version from the camera in real time.

### Vitals / Smart Sock

Open the **🩺 Sensors & vitals** card and click **Probe my devices**. The bridge pulls heart rate, oxygen, and other Smart Sock vitals from Owlet's cloud and shows them alongside the camera's room sensors. Once probed, readings refresh automatically.

---

## How we built this — the findings

Owlet ships no public API or RTSP. Getting here meant reverse‑engineering the Owlet Android app and the ThroughTek libraries it bundles.

### 1 · The camera is Kalay, the sock is Ayla

Owlet's **Smart Sock** lives on the Ayla IoT cloud; the **Cam** is a ThroughTek Kalay device that isn't in the Ayla device list at all. The login chain that yields the camera's P2P credentials:

| Step | Endpoint | Returns |
|---|---|---|
| Firebase `verifyPassword` | `identitytoolkit` (per‑region key, with `X‑Android‑Package`/`X‑Android‑Cert`) | Firebase `idToken` |
| Ayla SSO mini token | `ayla‑sso.owletdata.com/mini/` | mini token |
| Ayla `token_sign_in` | `…aylanetworks.com/api/v1/token_sign_in` (`provider: owl_id`) | Ayla token |
| **Camera key (KMS)** | **`camera‑kms.owletdata.com/kms/{DSN}`** (Firebase `idToken`) | **`tutkid` (UID), `authKey`, `password`** |

### 2 · Running Android `.so` libraries on x86‑64 without Android

ThroughTek's `.so` files are linked to Android's **Bionic** libc, so glibc refuses them. Instead of emulating Android, the image is built `FROM termux/termux-docker:x86_64` — a Bionic userspace — so the libraries `dlopen` cleanly on the normal Unraid kernel.

### 3 · The exact TUTK call sequence (recovered by decompiling the app)

The newer Kalay SDK is **licensed** and **region‑locked** and hangs forever if you skip either. Decompiling `com.owlet.tutk.AndroidTutkSdk` gave the precise order:

```
TUTK_SDK_Set_License_Key(<key baked into the app>)
IOTC_Set_LanSearchPort(63616) ; IOTC_Setup_Session_Alive_Timeout(20)
TUTK_SDK_Set_Region(3)                  # REGION_US
IOTC_Initialize2(0) ; avInitialize(512)
IOTC_Connect_ByUIDEx(uid, sid, &St_IOTCConnectInput)
avClientStartEx(&St_AVClientStartInConfig, &out)     # DTLS AV login
avSendIOCtrl(511 = IOTYPE_USER_IPCAM_START, …)
loop avRecvFrameData2() → H.264
```

### 4 · The struct ABI — read straight out of the libraries

The connect/AV‑login calls take C structs whose layout jadx can't recover (it alphabetises Java fields). So the offsets were **disassembled directly** from the Owlet `libIOTCAPIs.so` / `libAVAPIs.so`. Two details made or broke the whole thing:

- **`St_IOTCConnectInput` (160 bytes):** the real `IOTC_Connect_ByUIDEx` first does `cmp [struct], 0xA0` — the struct **must begin with its own size, `160`**, or it returns `-46` instantly. The AuthKey is exactly **8 chars** at offset `0x08`.
- **`St_AVClientStartInConfig` (64 bytes):** same trick — a leading `structSize` of `64`, with account/password as pointers.

### 5 · The camera demands DTLS at the AV layer

The simple `avClientStart2` login is rejected with `‑20049` (a DTLS‑class error). The Dream Duo requires the encrypted **`avClientStartEx`** login the app uses — security mode **Auto** negotiates DTLS and connects on the first try.

### 6 · Two‑way audio — the AV server model

Sending audio on the video client channel is silently dropped. The app opens a **separate AV server** channel (`avServStart2`) on the live session, which the camera dials back into after receiving the `SPEAKERSTART` IOCTL. The critical step that was missing from every prior attempt: the app calls `avServExit(session, chan)` to clear stale AV state on the free channel **before** calling `avServStart2` — without it, `avServStart2` returns `‑20027 (AV_ER_IOTC_CHANNEL_IN_USED)` every time. Recovered from `i1/m.smali` in the decompiled APK.

---

## Configuration reference

Everything's set in the web UI; these env vars override or tune it:

| Var | Default | Meaning |
|---|---|---|
| `OWLET_UI_USER` / `OWLET_UI_PASS` | `admin` / *(generated)* | control‑panel login. Auth is **always on**; with no password set, a random one is written to `/config/secrets/ui.json` (0600) and only that path is logged |
| `OWLET_REFRESH_KMS` | `1` | `0` = never log in to the Owlet cloud automatically; streams use only the saved camera key |
| `OWLET_LOGIN_BACKOFF` / `OWLET_LOGIN_BACKOFF_MAX` / `OWLET_LOGIN_MAX_ATTEMPTS` | `60` / `21600` / `5` | automatic key refresh is rare by design: the saved key is used first; a refresh (one Firebase password login + KMS fetch) only follows a connect / AV‑login failure, at most **once per stream start**, with account‑wide exponential backoff (60 s, 120 s, 240 s …, max 6 h). After **5** attempts without a session that delivers video, automatic logins stop until a manual *Connect / refresh key* succeeds. State: `/config/secrets/login-guard.json` |
| `OWLET_FORGET_ACCOUNT` | `0` | `1` = once every camera has a saved key, blank the Owlet email/password in `/config/owlet.yaml` (also settable as `"forget_account": "1"` there). No cloud logins can happen afterwards; to re‑provision, unset it and re‑enter the account |
| `OWLET_TALK` | `0` | `1` enables talk‑back and streamed sound playback into the nursery speaker (`/api/talk`, `/api/play`, sound uploads) |
| `GO2RTC_USER` / `GO2RTC_PASS` | `owlet` / *(generated)* | credentials go2rtc requires on RTSP and its API for every non‑loopback client (`api.username`/`api.password`, `rtsp.username`/`rtsp.password`). Unset password = random one kept in `/config/secrets/go2rtc.json` (0600) |
| `GO2RTC_API_LISTEN` | `127.0.0.1:1984` | go2rtc API bind address. Keep it on loopback; `:1984` only if you publish it deliberately |
| `GO2RTC_RTSP_LISTEN` | `:8554` | RTSP bind address inside the container (reached by Frigate over the shared Docker network) |
| `PUBLIC_RTSP_PORT` | `8554` | the port shown in the UI's copy URL |
| `OWLET_KEEPALIVE` | `1` | keep one camera session warm 24/7 (auto‑reconnect). **Leave on.** |
| `OWLET_SESSION_CHECK` | `0` | experimental: log the TUTK session mode (P2P/relay) after each connect — crashed the stream process on the reference camera's lib build, keep off unless debugging |
| `OWLET_STALL_TIMEOUT` | `30` | seconds without a video frame before the stream process unblocks/relaunches itself (see [Self‑healing](native-bridge/docs/self-heal.md)) |
| `OWLET_WATCHDOG` | `1` | container watchdog: restart a dead camera's stream process, then (rate‑limited) the container |
| `OWLET_WATCHDOG_STALL` / `_PRODUCER_RESTARTS` / `_COOLDOWN` | `120` / `2` / `300` | seconds between watchdog actions · stream restarts before a container restart · minimum seconds between container restarts (doubles each time, resets after 15 min healthy) |
| `OWLET_AV_SECURITY_MODE` | *(auto)* | pin `0` Simple / `1` Dtls / `2` Auto |
| `OWLET_REGION_CODE` | `3` | TUTK region (US = 3) |
| `OWLET_IOTYPE_START` | `511` | start‑video IOCTL |
| `OWLET_SENSOR_INTERVAL` | `2` | seconds between GET_REALTIME_DATA polls |
| `OWLET_SPK_VOL` | *(unset)* | initial speaker volume 0–100 % applied at connect |
| `OWLET_TEMP_SCALE` | `1` | divide raw temperature by this — set `10` if temp reads ~10× too high |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **KMS `403 Forbidden`** on diagnose | DSN is wrong — it starts with the **letter `O`** (`OCD…`), not a zero. |
| **`PermissionError` / settings won't save** | `chmod -R 777 /mnt/user/appdata/owlet/config`, restart. |
| **Frigate can't reach the bridge** (`connection refused` / `no route` / `401`) | Both containers must be on the same user‑defined Docker network (`owlet-net`), and the URL must carry `GO2RTC_USER:GO2RTC_PASS@`. |
| **Port conflict with Frigate** | None by default: the bridge publishes only `8088`. |
| **"Open video UI" / copy buttons don't work** | Set the `PUBLIC_*` env vars and pull `:latest` (clipboard is blocked on plain http — the new UI falls back). |
| **Stream pops in and out** | Close the **Owlet app on all phones** — the cam allows one session and they fight. Keep `OWLET_KEEPALIVE` on. Check `docker exec owlet-bridge-native tail -50 /config/tutk.log`. |
| **Video freezes a minute or two after (re)connecting, audio keeps going** | A wedged TUTK session with the stream's main thread blocked. The bridge now fixes this itself in ~30–90 s (`[stall]` lines in `/config/tutk-<camera>.log` show what was blocked); the container watchdog backs that up. See [Self‑healing](native-bridge/docs/self-heal.md) — and open `/api/status` → `watchdog` for live counters. |
| **High CPU in Frigate** | Disable **birdseye** for the Owlet camera — it was decoding the full 1440p feed for the mosaic. |
| **`MISSING …libIOTCAPIs.so`** | Upload your Owlet APK in the **TUTK libraries** card (or drop it in the config folder). |
| **🎤 mic button is greyed out** | The browser requires HTTPS for microphone access. Open the panel on `https://` and accept the self‑signed cert warning. |
| **Talk opens but no sound** | Check the log for `avServStart2` return code. If it shows `‑20027` update to the latest image. |
| **Temperature reads ~10× too high** | Set `OWLET_TEMP_SCALE=10` — some firmware revisions use tenths‑of‑°C. |

---

## FAQ

**Is the video local?** Yes — once connected, frames are peer‑to‑peer over your LAN and never leave your network. Only the one‑time login + camera‑key fetch use Owlet's cloud, and those creds are cached.

**Can an Owlet app update break it?** Not by itself — the bridge uses cached creds + the libraries you extracted, not the live app. Server‑side auth changes or camera firmware *could* eventually require re‑extracting from a newer APK (same maintenance model as docker‑wyze‑bridge). Keep a copy of your working `.apkm`.

**Does it survive a reboot / power loss?** Yes — `--restart unless-stopped` brings the container back and the keepalive reconnects the camera automatically.

**Can I add multiple cameras?** Yes — add each one by DSN in the Cameras card. Each gets its own RTSP stream at `rtsp://<host>:8554/<camera-name>`.

**Why no motion alerts?** The Owlet app delivers motion and sound *push* alerts via Firebase Cloud Messaging — a cloud service, not the local camera link. These can't be reproduced locally. Use the live **sound level** sensor in Home Assistant to build automations instead.

---

## Repo layout

| Path | What |
|---|---|
| [`native-bridge/bridge/`](native-bridge/bridge) | The container: `webapp.py`, `owlet_api.py`, `tutk_client.py`, `lib_extract.py`, `go2rtc.yaml`, `Dockerfile.bionic`, web UI |
| `unraid/owlet-bridge-native.xml` | Unraid template |
| `frigate/owlet.camera.yml` | A camera block for your Frigate config |
| `.github/workflows/build-bridge.yml` | Builds the image to GHCR |

---

## Legal / ethical

This is interoperability with **your own** camera, account, and app — the same basis the Wyze/Owlet community tools and security researchers work on. The proprietary ThroughTek `.so` libraries are **not** redistributed; each user extracts them from the app they installed (the repo `.gitignore` keeps them out). Don't point any of this at devices or accounts that aren't yours.

## Acknowledgments

Built on [go2rtc](https://github.com/AlexxIT/go2rtc), inspired by [docker‑wyze‑bridge](https://github.com/mrlt8/docker-wyze-bridge), and made possible by ThroughTek's Kalay SDK shipped inside the Owlet app.
