ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip \
    && python3 -m venv /opt/gigaset-venv \
    && /opt/gigaset-venv/bin/pip install --no-cache-dir "paho-mqtt==2.1.0"

COPY app /opt/gigaset-camera
COPY rootfs /

RUN chmod 0755 /etc/services.d/gigaset-camera-mqtt/run

LABEL \
    org.opencontainers.image.title="Home Assistant Add-on: Gigaset Camera MQTT" \
    org.opencontainers.image.description="Optional local MQTT bridge for Gigaset/Y-cam Gen1 cameras" \
    org.opencontainers.image.licenses="MIT"
