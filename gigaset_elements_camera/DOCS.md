# Gigaset Camera MQTT

This optional add-on connects a Gigaset/Y-cam Gen1 camera to Home Assistant.
The camera continues to work locally when the add-on is stopped or removed.
No cloud service is contacted.

## Configuration

- `camera_ip`: fixed IP address or local DNS name of the camera.
- `camera_mac`: MAC address printed on the camera. It is used to derive the
  stock web password locally when `camera_password` is empty.
- `camera_name`: entity and device name shown in Home Assistant.
- `camera_user`: camera web user, normally `admin`.
- `camera_password`: optional explicit web password.
- `http_token`: shared token expected on motion requests. Use a long random
  value and enter the same value in the camera.
- `motion_hold`: seconds for which the motion entity remains on.
- `snapshot_interval`: seconds between JPEG snapshot updates.
- `mqtt_topic`: MQTT topic root for this camera.

The add-on obtains MQTT host, port and credentials from Home Assistant's MQTT
service automatically. Port `8766` is exposed only so the camera can report a
motion event.

## Camera setup

1. Open **HTTP server** in the camera manager, select a free slot, set the
   server to the Home Assistant host IP and the port to `8766`. Set
   authorization to **No**.
2. Open **Alarm sending**, enable HTTP event delivery, select that server slot,
   choose **Always**, and set the URL to `motion?token=YOUR_TOKEN`.
3. Enable and draw at least one region on the camera's **Motion** page.

Home Assistant discovers a motion binary sensor, availability state and JPEG
snapshot camera through MQTT discovery.
