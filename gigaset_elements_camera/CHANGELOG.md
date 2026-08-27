# Changelog

## 0.3.0

- replace the raw Home Assistant camera-list control with an authenticated
  visual editor using camera cards and an explicit **Add camera** button;
- migrate existing multi-camera and legacy single-camera settings automatically;
- remove the old single-camera fields from the regular Configuration tab.

## 0.2.2

- add Czech and English labels and descriptions for the visual Home Assistant
  configuration editor, including the multi-camera list and its fields.

## 0.2.1

- rename the add-on to **Gigaset elements camera local gateway** while keeping
  its existing slug and user configuration.

## 0.2.0

- support multiple cameras in one add-on instance;
- add a manual and periodic snapshot refresh path;
- discover a Home Assistant snapshot-refresh button and diagnostic sensors;
- publish firmware, uptime, network, storage and stream telemetry;
- proxy authenticated JPEG snapshots and live MJPEG streams;
- retain compatibility with the original single-camera configuration keys.

## 0.1.0

- initial local MQTT motion, availability and snapshot bridge.
