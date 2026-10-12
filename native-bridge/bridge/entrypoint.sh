#!/usr/bin/env bash
# Start the control-panel UI (:8088) and go2rtc (:1984/:8554) together.
set -uo pipefail

umask 077   # /config holds credentials: files 0600, dirs 0700
mkdir -p /config

# Seed an empty config so the exec source doesn't fail on first boot.
[ -f /config/owlet.env ] || echo "# owlet-bridge" > /config/owlet.env

echo "[owlet-bridge] control panel : http://<host>:8088"
echo "[owlet-bridge] video UI       : http://<host>:1984"
echo "[owlet-bridge] RTSP           : rtsp://<host>:8554/owlet"

# Control panel in background, go2rtc in foreground (PID 1 supervises stream).
python3 /app/webapp.py &
exec go2rtc -config /app/go2rtc.yaml
