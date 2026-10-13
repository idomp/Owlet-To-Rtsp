#!/usr/bin/env bash
# Debian image startup: control panel (:8088) + go2rtc. Mirrors start-bionic.sh
# for the parts that decide what is reachable: /config permissions, the
# generated authenticated go2rtc config and the panel's TLS certificate.
set -uo pipefail

# /config holds the Owlet password, camera keys, MQTT and RTSP credentials:
# everything this container creates is private to its user (files 0600, dirs
# 0700), and anything an older version left group/world-accessible is tightened.
umask 077
chmod -R go-rwx /config 2>/dev/null || true

mkdir -p /config /config/vitals 2>/dev/null
# Seed an empty config so the exec source doesn't fail on first boot.
if [ ! -f /config/owlet.env ]; then
  if ! (echo "# owlet-bridge" > /config/owlet.env) 2>/dev/null; then
    echo "[owlet-bridge] WARN: /config is not writable by this container; settings won't persist."
    echo "[owlet-bridge]   Fix ownership of the host directory mounted at /config, never chmod 777."
  fi
fi

echo "[owlet-bridge] control panel : https://<host>:8088"
echo "[owlet-bridge] go2rtc API     : container loopback only (GO2RTC_API_LISTEN to change)"
echo "[owlet-bridge] RTSP           : rtsp://<GO2RTC_USER>:<GO2RTC_PASS>@<container>:8554/<camera> (shared Docker network)"

# Generate the go2rtc config (one stream per saved camera, API/RTSP credentials)
# + per-camera env files. Falls back to the baked-in loopback-only
# /app/go2rtc.yaml if /config isn't writable.
GO2RTC_CFG=/app/go2rtc.yaml
if python3 /app/render_streams.py; then
  [ -f /config/go2rtc.gen.yaml ] && GO2RTC_CFG=/config/go2rtc.gen.yaml
fi
echo "[owlet-bridge] go2rtc config : $GO2RTC_CFG"

# Self-signed TLS cert so the control panel runs on HTTPS (the browser only
# allows the microphone on a secure origin).
SSL_DIR=/config/ssl
if [ ! -f "$SSL_DIR/cert.pem" ] || [ ! -f "$SSL_DIR/key.pem" ]; then
  mkdir -p "$SSL_DIR"
  OPENSSL_BIN=$(command -v openssl 2>/dev/null || echo "")
  if [ -n "$OPENSSL_BIN" ] && "$OPENSSL_BIN" req -x509 -newkey rsa:2048 \
       -keyout "$SSL_DIR/key.pem" -out "$SSL_DIR/cert.pem" \
       -days 3650 -nodes -subj "/CN=owlet-bridge" 2>/dev/null; then
    echo "[owlet-bridge] TLS cert generated — control panel will use HTTPS"
  else
    echo "[owlet-bridge] openssl not found — control panel will use HTTP (mic needs Chrome flag)"
  fi
fi

# Publish the camera's room sensors (from its own P2P session, no cloud) ->
# snapshot.json (web UI + /api/vitals), the burned-in overlay text and Home
# Assistant (MQTT, if configured). OWLET_SENSOR_PUBLISH=0 disables it. The
# Smart Sock cloud poller inside it stays OFF unless OWLET_VITALS_POLL=1 (it
# writes APP_ACTIVE to the sock every cycle). Same block as start-bionic.sh.
if [ "${OWLET_SENSOR_PUBLISH:-1}" != "0" ]; then
  python3 /app/vitals_poller.py &
fi

# Control panel in background, go2rtc in foreground (PID 1 supervises stream).
python3 /app/webapp.py &
exec go2rtc -config "$GO2RTC_CFG"
