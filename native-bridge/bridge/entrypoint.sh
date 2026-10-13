#!/usr/bin/env bash
# Debian image startup: control panel (:8088) + go2rtc. Takes three start-up
# steps from start-bionic.sh: the generated per-camera go2rtc config, the
# panel's TLS certificate and the sensor publisher. (The Bionic script does
# more: log trimming, APK provisioning, the keepalive and the watchdog.)
set -uo pipefail

mkdir -p /config /config/vitals 2>/dev/null
# Seed an empty config so the exec source doesn't fail on first boot.
if [ ! -f /config/owlet.env ]; then
  if ! (echo "# owlet-bridge" > /config/owlet.env) 2>/dev/null; then
    echo "[owlet-bridge] WARN: /config is not writable by this container; settings won't persist."
    echo "[owlet-bridge]   Fix ownership of the host directory mounted at /config."
  fi
fi

# Generate the go2rtc config (one stream per saved camera) + per-camera env
# files, like start-bionic.sh does. Falls back to the baked-in single-camera
# /app/go2rtc.yaml if /config isn't writable.
GO2RTC_CFG=/app/go2rtc.yaml
if python3 /app/render_streams.py; then
  [ -f /config/go2rtc.gen.yaml ] && GO2RTC_CFG=/config/go2rtc.gen.yaml
fi
echo "[owlet-bridge] go2rtc config : $GO2RTC_CFG"

# Self-signed TLS cert so the control panel runs on HTTPS (the browser only
# allows the microphone on a secure origin). Same block as start-bionic.sh;
# the Dockerfile installs the openssl CLI for it.
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
SCHEME=http
[ -f "$SSL_DIR/cert.pem" ] && [ -f "$SSL_DIR/key.pem" ] && SCHEME=https

echo "[owlet-bridge] control panel : $SCHEME://<host>:8088"
echo "[owlet-bridge] video UI       : http://<host>:1984"
echo "[owlet-bridge] RTSP           : rtsp://<host>:8554/<camera>"

# Sock vitals + camera room sensors -> snapshot.json (web UI + /api/vitals),
# the burned-in overlay text and Home Assistant (MQTT, if configured). Same
# switch as start-bionic.sh; set OWLET_VITALS_POLL=0 to disable.
if [ "${OWLET_VITALS_POLL:-1}" = "1" ]; then
  python3 /app/vitals_poller.py &
fi

# Control panel in background, go2rtc in foreground (PID 1 supervises stream).
python3 /app/webapp.py &
exec go2rtc -config "$GO2RTC_CFG"
