# Owlet Bridge — app integration guide

Everything your native app needs to talk to the bridge directly (instead of
through Frigate) for **sub-second WebRTC video** plus **all the sock + camera
data**. `<host>` below is your bridge, e.g. `<bridge-lan-ip>`.

| Service | Port | Default | What |
|---|---|---|---|
| Bridge UI + REST | `8088` | **published**, HTTPS (self-signed) + Basic auth | `/api/vitals`, `/api/cameras`, talk, sounds, snapshots |
| go2rtc API | `1984` | container loopback only, not published | WebRTC/MSE/HLS, frames, stream list |
| RTSP | `8554` | not published; auth required | `rtsp://<user>:<password>@<container-name>:8554/<camera>` from containers on the same Docker network |
| WebRTC media (UDP/TCP) | `8555` | not published | ICE — must be reachable for sub-second |

### Defaults and how to opt in

Out of the box only the control panel is reachable from outside the container.
Everything else is an explicit opt-in:

- **Control panel (`8088`)** always requires HTTP Basic auth: `OWLET_UI_USER` /
  `OWLET_UI_PASS`, or user `admin` with the password generated into
  `/config/secrets/ui.json`. Send `Authorization: Basic <base64(user:password)>`
  on every request. It serves HTTPS with a self-signed certificate
  (`/config/ssl/cert.pem`): trust or pin that certificate in your app, or
  replace it with your own.
- **go2rtc API (`1984`, WebRTC/MSE/HLS/frames)**: set
  `GO2RTC_API_LISTEN=:1984` and publish `1984` yourself (e.g. `-p 1984:1984`).
  go2rtc then requires `GO2RTC_USER` / `GO2RTC_PASS` (or the generated pair in
  `/config/secrets/go2rtc.json`) as HTTP Basic auth from every non-loopback
  client. That API can rewrite go2rtc's config and run commands, so only
  publish it on a trusted network.
- **WebRTC media (`8555`)**: publish `8555/udp` and `8555/tcp` yourself and set
  `OWLET_WEBRTC_CANDIDATE` (see below).
- **RTSP (`8554`)**: reachable from containers on the same user-defined Docker
  network at the container name; publish `8554` yourself if a host or device
  outside Docker needs it. Credentials are always required.
- **Talk / sound playback / sound upload** answer `403` unless `OWLET_TALK=1`.
- **Smart Sock vitals** only appear with `OWLET_VITALS_POLL=1` (that poller
  writes `APP_ACTIVE` to the sock through Owlet's cloud every cycle, which is
  why it is off). Camera room sensors are published locally regardless.

> Talk to the bridge **directly**, not via Frigate. Frigate restreams our RTSP,
> which adds a hop + its own jitter buffer. Going straight to our go2rtc WebRTC
> (after the opt-in above) is the lowest latency path.

---

## 1. Video — sub-second WebRTC (go2rtc, WHEP)

Each camera publishes two streams:
- `**<camera>**` — clean H.264 copy (no transcode). **Use this for the app.**
- `**<camera>_overlay**` — same feed with the glass HUD burned in (on-demand,
  transcoded). Use only if you want a server-rendered HUD; otherwise draw the
  overlay yourself from `/api/vitals` (crisper, lower latency).

Everything in this section talks to the go2rtc API on `1984`, so it needs the
opt-in above (`GO2RTC_API_LISTEN=:1984`, port published) and HTTP Basic auth
with the go2rtc credentials (`GO2RTC_USER` / `GO2RTC_PASS`).

List streams: `GET http://<host>:1984/api/streams`

### WHEP (recommended for iOS/Swift)
go2rtc speaks **WHEP** (WebRTC-HTTP Egress Protocol) — the modern standard.

```
POST http://<host>:1984/api/webrtc?src=<camera>
Authorization: Basic <base64(GO2RTC_USER:GO2RTC_PASS)>
Content-Type: application/sdp
Body: <your WebRTC offer SDP>
→ 201 Created, body = answer SDP
```

Swift flow (using Google's `WebRTC` framework / `stasel/WebRTC`):
1. Create `RTCPeerConnection` (recvonly video+audio).
2. `offer(...)` → set local description.
3. POST the offer SDP to the URL above; get the answer SDP.
4. Set remote description = answer. Frames start flowing.

Minimal example:
```swift
var mc = RTCMediaConstraints(mandatoryConstraints:
  ["OfferToReceiveVideo":"true","OfferToReceiveAudio":"true"], optionalConstraints: nil)
pc.offer(for: mc) { sdp, _ in
  pc.setLocalDescription(sdp!) { _ in
    var req = URLRequest(url: URL(string: "http://\(host):1984/api/webrtc?src=owlet")!)
    req.httpMethod = "POST"
    req.setValue("application/sdp", forHTTPHeaderField: "Content-Type")
    let basic = Data("\(g2User):\(g2Pass)".utf8).base64EncodedString()
    req.setValue("Basic \(basic)", forHTTPHeaderField: "Authorization")
    req.httpBody = sdp!.sdp.data(using: .utf8)
    URLSession.shared.dataTask(with: req) { data, _, _ in
      let answer = RTCSessionDescription(type: .answer,
        sdp: String(data: data!, encoding: .utf8)!)
      pc.setRemoteDescription(answer) { _ in }
    }.resume()
  }
}
```

### Getting it actually sub-second
WebRTC needs a reachable ICE candidate. Inside Docker, go2rtc may advertise the
wrong IP. Set the bridge env var:
```
OWLET_WEBRTC_CANDIDATE=<bridge-LAN-ip>:8555
```
and publish host port **8555 (UDP and TCP)** yourself (the shipped deployment
files don't). Then the offer/answer
completes immediately and you get ~0.2–0.5 s glass-to-glass on LAN. (You can pass
multiple comma-separated candidates, e.g. LAN + VPN IP.)

### Fallbacks
- **MSE** (low latency, very reliable, great for a quick WKWebView): 
  `ws://<host>:1984/api/ws?src=<camera>` (go2rtc MSE/WebSocket).
- **HLS** (higher latency, universal): `http://<host>:1984/api/stream.m3u8?src=<camera>`
- **RTSP** (for VLC/ffmpeg): `rtsp://<GO2RTC_USER>:<GO2RTC_PASS>@<container-name>:8554/<camera>`
  from a container on the same Docker network, or
  `rtsp://<GO2RTC_USER>:<GO2RTC_PASS>@<host>:8554/<camera>` if you published `8554`.

---

## 2. Snapshots (poster / thumbnails)
```
GET https://<host>:8088/snapshot/<camera>.jpg           # bridge passthrough (Basic auth, default setup)
GET http://<host>:1984/api/frame.jpeg?src=<camera>      # go2rtc directly (API opt-in + go2rtc auth)
```

---

## 3. Data — `GET https://<host>:8088/api/vitals`

All `8088` calls need the control-panel Basic auth header, e.g.
`curl --cacert cert.pem -u <ui-user>:<ui-password> https://<host>:8088/api/vitals`
(`cert.pem` = a copy of `/config/ssl/cert.pem`; `-k` skips verification for a
quick test only).

The single feed for your overlay. Add `?units=metric` for °C (default is °F/US).

```json
{
  "ts": 1750000000.0,
  "units": "us",
  "devices": [
    {
      "dsn": "X1234...", "name": "sock", "kind": "sock", "model": "Dream Sock",
      "sensors": {
        "heart_rate": 124, "oxygen": 98, "oxygen_avg": 97,
        "skin_temperature": 99, "sleep_state": 8, "movement": 2,
        "battery": 74, "battery_minutes": 410, "signal_strength": -52,
        "base_station_on": 1, "charging": 0
      }
    },
    {
      "dsn": "owlet", "name": "owlet", "kind": "cam", "model": "Owlet Cam",
      "ts": 1750000000.0,
      "sensors": {
        "temperature": 72, "humidity": 48, "noise": 39,
        "brightness": 150, "motion": 0, "sound": 0, "wifi_rssi": -55
      }
    }
  ]
}
```

Poll every 2–5 s. Values update server-side (~15 s sock, ~10 s cam). `kind` is
`sock`, `cam`, or `device`. Match a sock to a camera by your own pairing (e.g.
let the user assign in-app).

### Field reference

**Sock (`kind:"sock"`)**
| key | meaning | unit / values |
|---|---|---|
| `heart_rate` | heart rate | bpm (null while charging/off-foot) |
| `oxygen` | SpO₂ | % (null while charging/off-foot) |
| `oxygen_avg` | 10-min average SpO₂ | % |
| `skin_temperature` | skin temperature | °F (°C with `units=metric`) |
| `sleep_state` | sleep state | `1`=awake, `8`=light, `15`=deep, `0`=unknown |
| `movement` | movement level | integer |
| `battery` | sock battery | % |
| `battery_minutes` | runtime remaining | minutes |
| `signal_strength` | sock RF signal | dBm |
| `base_station_on` | base powered | `0`/`1` |
| `charging` | on charger | `0`/`1` (when `1`, vitals are null) |

**Camera (`kind:"cam"`)** — read live off the TUTK stream
| key | meaning | unit / values |
|---|---|---|
| `temperature` | room temperature | °F (°C with `units=metric`) |
| `humidity` | room humidity | % RH |
| `noise` | room noise | dB |
| `brightness` | room brightness | lux |
| `motion` | motion detected | `0`/`1` |
| `sound` | sound detected | `0`/`1` |
| `wifi_rssi` | camera WiFi | dBm |

> `temperature`, `noise`, `motion`, `sound` come from the video frame metadata
> (instant). `humidity`, `brightness`, `wifi_rssi` come from a sensor poll
> (~10 s).

### Live cameras + their stream URLs
```
GET https://<host>:8088/api/cameras
```
returns each camera with its `name` and stream status, plus `rtsp_host`,
`rtsp_user`, `rtsp_port`, `rtsp_published`, `rtsp_mode` and `api_published`
(true only when `GO2RTC_API_LISTEN` is a non-loopback address). The RTSP
endpoint follows the first that applies:

| `rtsp_mode` | When | `rtsp_host` / `rtsp_port` | `rtsp_published` |
|---|---|---|---|
| `advertised` | `OWLET_ADVERTISE_RTSP=host:port` | exactly that host and port | `true` |
| `published` | `PUBLIC_RTSP_PORT` set | the host name of your request / `PUBLIC_RTSP_PORT` | `true` |
| `network` | neither | the container's hostname / go2rtc's RTSP listen port | `false` |

The RTSP password is never returned.

---

## 4. Two-way audio (talk + sounds)
These answer `403` unless the bridge runs with `OWLET_TALK=1` (`/api/status`
reports it as `talk_enabled`). The camera's built-in lullabies
(`/api/lullaby/<camera>/…`) are not gated.

- **Play a sound out the camera speaker:**
  `POST https://<host>:8088/api/play/<camera>`  body `{"file":"lullaby.mp3"}`
- **Push live audio (walkie-talkie):** `POST https://<host>:8088/api/talk/<camera>`
  with an audio clip (multipart) — re-encoded to the camera's AAC and sent to the
  speaker. `POST /api/talk/<camera>/stop` to stop.
- List/upload/delete sounds: `GET/POST /api/sounds`, `DELETE /api/sounds/<name>`.

(WebRTC bidirectional mic → camera is possible later; today the talk path is the
HTTP endpoint above.)

---

## 5. Home Assistant
Configure in the bridge UI (🏠 Home Assistant card) or via env
(`OWLET_MQTT_HOST`, …). Every cam sensor (and, with `OWLET_VITALS_POLL=1`,
every sock vital) is published with MQTT discovery, so HA auto-creates the entities (temps in °F). Your app can also just
read HA, but `/api/vitals` is the most direct.

---

## TL;DR for the app
1. Video: opt in (`GO2RTC_API_LISTEN=:1984`, publish `1984` and `8555`), then
   `POST http://<host>:1984/api/webrtc?src=<camera>` (WHEP) with go2rtc Basic
   auth + set `OWLET_WEBRTC_CANDIDATE` for sub-second.
2. Data: poll `GET https://<host>:8088/api/vitals` (°F, control-panel Basic
   auth, self-signed cert) and draw your glass HUD.
3. Talk/sounds (`OWLET_TALK=1`): `POST /api/talk/<camera>` and `/api/play/<camera>`.
