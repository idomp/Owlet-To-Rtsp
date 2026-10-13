# owlet-bridge — native Owlet/Kalay -> RTSP, the wyze-bridge way.
#   control panel (Flask, HTTPS) :8088  |  RTSP :8554 (authenticated)
#   go2rtc API :1984 stays on container loopback unless GO2RTC_API_LISTEN is set.
#
# The proprietary TUTK .so libs are NOT shipped. Extract them from the Owlet APK
# you own (extract-libs.sh) and mount them at /app/libs.
FROM python:3.12-slim

ARG GO2RTC_VERSION=1.9.4

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl ffmpeg libstdc++6 openssl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_amd64" \
    && chmod +x /usr/local/bin/go2rtc

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# The whole bridge directory, so every module the code imports is present
# (.dockerignore keeps tests, caches and any local TUTK .so files out).
COPY . /app/
RUN chmod +x /app/entrypoint.sh

ENV TUTK_LIB_DIR=/app/libs/x86_64 \
    LD_LIBRARY_PATH=/app/libs/x86_64 \
    CONFIG_PATH=/config/owlet.yaml \
    ENV_PATH=/config/owlet.env

VOLUME ["/config"]
EXPOSE 8088 1984 8554 8555
ENTRYPOINT ["/app/entrypoint.sh"]
