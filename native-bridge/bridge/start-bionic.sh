#!/data/data/com.termux/files/usr/bin/bash
# Bionic runtime startup: control panel (Flask) + go2rtc. The Owlet TUTK libs
# load here because this is a Bionic (Termux) userspace.
export PATH=/data/data/com.termux/files/usr/bin:/usr/local/bin:/usr/bin:/bin
export LD_LIBRARY_PATH=/app/libs/x86_64:/data/data/com.termux/files/usr/lib

# /config holds the Owlet password, camera keys, MQTT and RTSP credentials:
# everything this container creates is private to its user (files 0600, dirs
# 0700), and anything an older version left group/world-accessible is tightened.
umask 077
chmod -R go-rwx /config 2>/dev/null || true

mkdir -p /config /config/vitals 2>/dev/null
if [ ! -f /config/owlet.env ]; then
  if ! (echo "# owlet-bridge" > /config/owlet.env) 2>/dev/null; then
    echo "[owlet-bridge/bionic] WARN: /config is not writable by this container."
    echo "[owlet-bridge/bionic]   Settings won't persist. The container runs as uid 1000; on Unraid run:"
    echo "[owlet-bridge/bionic]     chown -R 1000:1000 /mnt/user/appdata/owlet && chmod -R u+rwX,go-rwx /mnt/user/appdata/owlet"
    echo "[owlet-bridge/bionic]   (never chmod 777 — the folder holds your Owlet password and camera keys)"
    echo "[owlet-bridge/bionic]   then restart this container."
  fi
fi

echo "[owlet-bridge/bionic] control panel : http://<host>:8088"
echo "[owlet-bridge/bionic] go2rtc API     : container loopback only (GO2RTC_API_LISTEN to change)"
echo "[owlet-bridge/bionic] RTSP           : rtsp://<GO2RTC_USER>:<GO2RTC_PASS>@<container>:8554/<camera> (shared Docker network)"

# Cap the per-camera TUTK logs so they can't slowly fill appdata. tutk_client
# appends to them via go2rtc's exec; keep the tail of any that grew past ~20 MB.
# Run once at boot AND every hour in the background — a flapping camera can grow
# its log fast, so a boot-only trim isn't enough.
_trim_logs() {
  for lf in /config/tutk.log /config/tutk-*.log; do
    [ -f "$lf" ] || continue
    if [ "$(stat -c%s "$lf" 2>/dev/null || echo 0)" -gt 20971520 ]; then
      tail -c 2097152 "$lf" > "$lf.tmp" 2>/dev/null && mv "$lf.tmp" "$lf" 2>/dev/null
      echo "[owlet-bridge/bionic] trimmed oversized $lf"
    fi
  done
}
_trim_logs
( while sleep 3600; do _trim_logs; done ) &

# Auto-provision the proprietary TUTK libs from a dropped Owlet APK (.apk/.apkm)
# if they're not already in the mounted libs folder. No-op once they're present.
python3 -c 'import sys; sys.path.insert(0,"/app"); from lib_extract import provision; ok,m=provision("/app/libs/x86_64",["/config","/app/libs","/apk"]); print("[owlet-bridge/bionic] libs:",m)' 2>&1

# sanity: confirm the TUTK libs load at startup (logs to container output)
python3 - <<'PY' 2>&1 || echo "[owlet-bridge/bionic] WARN: TUTK libs not loadable yet (mount /app/libs)"
import ctypes, os
d = os.environ.get("TUTK_LIB_DIR", "/app/libs/x86_64")
for n in ("libTUTKGlobalAPIs.so","libP2PTunnelAPIs.so","libRDTAPIs.so","libIOTCAPIs.so","libAVAPIs.so"):
    p = os.path.join(d, n)
    if os.path.exists(p):
        ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
print("[owlet-bridge/bionic] TUTK libs load OK")
PY

# Generate the go2rtc config (one stream per saved camera) + per-camera env files.
# Falls back to the baked-in single-camera /app/go2rtc.yaml if /config isn't
# writable (so a permission problem degrades gracefully to the old behaviour).
GO2RTC_CFG=/app/go2rtc.yaml
if python3 /app/render_streams.py; then
  [ -f /config/go2rtc.gen.yaml ] && GO2RTC_CFG=/config/go2rtc.gen.yaml
fi
echo "[owlet-bridge/bionic] go2rtc config : $GO2RTC_CFG"

# Generate a self-signed TLS cert so the control panel runs on HTTPS.
# Browsers block microphone access on plain HTTP (non-localhost). Self-signed is
# fine for LAN use — you'll see a one-time "not secure" warning, click proceed.
# openssl binary is a dep of python/ffmpeg in Termux, so it's always present.
SSL_DIR=/config/ssl
if [ ! -f "$SSL_DIR/cert.pem" ] || [ ! -f "$SSL_DIR/key.pem" ]; then
  mkdir -p "$SSL_DIR"
  OPENSSL_BIN=$(command -v openssl 2>/dev/null || echo "")
  if [ -n "$OPENSSL_BIN" ] && "$OPENSSL_BIN" req -x509 -newkey rsa:2048 \
       -keyout "$SSL_DIR/key.pem" -out "$SSL_DIR/cert.pem" \
       -days 3650 -nodes -subj "/CN=owlet-bridge" 2>/dev/null; then
    echo "[owlet-bridge/bionic] TLS cert generated — control panel will use HTTPS"
  else
    echo "[owlet-bridge/bionic] openssl not found — control panel will use HTTP (mic needs Chrome flag)"
  fi
fi

python3 /app/webapp.py &

# Keep every camera's stream warm 24/7 (one persistent internal viewer each). The
# supervisor re-reads the camera list, so cameras added/removed in the UI are
# picked up without a restart. Set OWLET_KEEPALIVE=0 to disable.
if [ "${OWLET_KEEPALIVE:-1}" = "1" ]; then
  python3 /app/keepalive.py &
fi

# Publish the camera's room sensors (from its own P2P session, no cloud) ->
# snapshot.json (web UI + /api/vitals), the burned-in overlay text and Home
# Assistant (MQTT, if configured). OWLET_SENSOR_PUBLISH=0 disables it.
# The Smart Sock cloud poller inside it is separate and OFF unless
# OWLET_VITALS_POLL=1 -- it WRITES APP_ACTIVE to the sock every cycle; leave it off.
if [ "${OWLET_SENSOR_PUBLISH:-1}" != "0" ]; then
  python3 /app/vitals_poller.py &
fi

# Self-heal a wedged P2P session, cheapest fix first (see watchdog.py): if a
# camera's RTSP serves no frames, first SIGTERM just that camera's tutk_client so
# go2rtc relaunches the one stream with a fresh key (go2rtc, the UI and the other
# cameras stay up); only if that fails twice, kill go2rtc so Docker restarts the
# container — rate-limited with a doubling cooldown so a camera that is truly
# offline can't churn Owlet cloud logins. Set OWLET_WATCHDOG=0 to disable.
if [ "${OWLET_WATCHDOG:-1}" = "1" ]; then
  python3 /app/watchdog.py &
fi

exec go2rtc -config "$GO2RTC_CFG"
