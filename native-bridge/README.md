# owlet-bridge (native) — the docker-wyze-bridge way for the Owlet Cam

The Owlet Cam runs on **ThroughTek / Kalay (TUTK)** — the same P2P platform the
Wyze cams use. This container talks Kalay directly: **enter your Owlet login in a
web UI, it connects to the camera and serves RTSP / WebRTC / HLS.** No phone, no
emulator, no cloud relay.

> See the [top-level README](../README.md) for the full story — the Owlet auth
> chain, running Android `.so` libraries under Bionic on x86-64, and the TUTK
> connect/DTLS details recovered by disassembly. This file is the per-container
> reference.

```
 Owlet account ──┐
                 ▼
   webapp.py  (control panel :8088)  ── Owlet cloud login (Firebase→Ayla)
        │                                └─ camera key from camera-kms.owletdata.com
        ▼  /config/owlet.env
   tutk_client.py ──Kalay/TUTK P2P, DTLS──▶ Owlet Cam ──H264──▶ go2rtc ──▶ RTSP / WebRTC / HLS
                                                                  :8554      + video UI :1984
```

Everything is configurable in the browser: account credentials, region, camera
DSN, UID/AuthKey, AV settings. **Connect & Diagnose** runs the real Owlet login,
auto-fetches the camera's Kalay credentials from the KMS, and streams every step
to a live log.

---

## Deploy on Unraid

**Prereq (one-time):** the proprietary TUTK `.so` libraries aren't shipped —
extract them from the Owlet APK you own into the libs folder:

```bash
# on any machine with the apk + unzip
native-bridge/bridge/extract-libs.sh /path/to/owlet.apk x86_64
# -> libIOTCAPIs.so, libAVAPIs.so, libTUTKGlobalAPIs.so, libP2PTunnelAPIs.so, libRDTAPIs.so
# put that x86_64/ folder in the host directory mounted at /app/libs
# (Unraid template default: /mnt/user/appdata/owlet-camera/libs/)
```

**Prebuilt image (terminal):**
```bash
docker network create owlet-net   # once; attach Frigate to it too
docker run -d --name owlet-bridge --restart unless-stopped --init --network owlet-net \
  -p 8088:8088 \
  -e OWLET_UI_USER='admin' -e OWLET_UI_PASS='<ui password>' \
  -e GO2RTC_USER='owlet' -e GO2RTC_PASS='<rtsp password>' \
  -v /mnt/user/appdata/owlet-camera/config:/config \
  -v /mnt/user/appdata/owlet-camera/libs:/app/libs \
  ghcr.io/idomp/owlet-bridge-native:latest
```

**Or the Unraid template:** Docker → Add Container → paste the
`unraid/owlet-bridge-native.xml` raw URL → set the libs + config paths → apply.

**Or compose / local build:**
```bash
cd native-bridge/bridge
docker compose up -d --build
```
Upgrading an existing compose install: the default config location moved from
`/mnt/user/appdata/owlet` to `/mnt/user/appdata/owlet-camera`. If you never set
`APPDATA`, either set `APPDATA=/mnt/user/appdata/owlet` to keep the old location,
or stop the service and move `/mnt/user/appdata/owlet/config` into
`/mnt/user/appdata/owlet-camera/` once. Otherwise the bridge starts from an
empty `/config` with new credentials (see the compose file's header).

---

## Use it

1. Open **`https://<host>:8088`**. The bridge serves HTTPS with a self-signed
   certificate it generates on first start, so accept the browser warning once
   (plain HTTP is only a last-resort fallback when no certificate can be made).
   Log in with `OWLET_UI_USER` / `OWLET_UI_PASS` if set; otherwise the user is
   `admin` and the password is generated into `/config/secrets/ui.json` (0600)
   in the host directory mounted at `/config` — the log names the file, never
   the password.
2. Enter your Owlet **email / password**, pick **region**, and put your camera
   **DSN** (e.g. `OCD…`) in the *Camera DSN* field.
3. Click **Connect & Diagnose** — the camera UID / AuthKey / AV password
   auto-fill from the KMS.
4. Click **Save & (re)start stream**.
5. Frigate (on the same Docker network) pulls
   **`rtsp://owlet:<GO2RTC_PASS>@owlet-bridge:8554/owlet`**. go2rtc's own API/UI
   (1984) stays on container loopback and is not published.

---

## Files (`bridge/`)

| File | Role |
|---|---|
| `owlet_api.py` | Owlet login (Firebase→Ayla) + camera-key fetch (KMS) |
| `tutk_client.py` | Kalay/TUTK connect + DTLS AV login + start IOCTL → raw H.264 to stdout |
| `webapp.py` | Control-panel web UI (config, diagnose, logs, findings, status) |
| `templates/`, `static/` | The UI |
| `go2rtc.yaml` | Serves RTSP/WebRTC/HLS + video UI; supervises the TUTK client |
| `extract-libs.sh` | Pull the TUTK `.so` libraries from the Owlet APK |
| `Dockerfile.bionic` | Bionic (Termux) image so the Android libraries load on x86-64 |
| `docker-compose.yml` | Local build / run |

`../probe/kalay-probe.py` is an optional helper that confirms a camera answers
the Kalay LAN search on UDP 63616.

---

## Legal / ethical

Interoperability with **your own** camera, account, and traffic — the same basis
as the Wyze/Owlet community tools. Don't redistribute Owlet's proprietary `.so`
libraries; each user extracts them from the app they installed. Don't point this
at devices or accounts that aren't yours.
