# Gigaset Camera MQTT

This optional add-on connects one or more Gigaset/Y-cam Gen1 cameras to Home Assistant.
The camera continues to work locally when the add-on is stopped or removed.
No cloud service is contacted.

## Configuration

Configure cameras as a list. Each camera becomes a separate Home Assistant
device:

```yaml
cameras:
  - name: Front door
    ip: 192.0.2.10
    mac: 02:00:00:00:00:01
    user: admin
    password: ""
    token: change-this-front-door-token
  - name: Garage
    ip: 192.0.2.11
    mac: 02:00:00:00:00:02
    user: admin
    password: ""
    token: change-this-garage-token
motion_hold: 15
snapshot_interval: 10
telemetry_interval: 60
mqtt_topic: gigaset/camera
```

- `password`: leave empty to derive the stock password locally from the MAC;
  enter the changed web password otherwise.
- `token`: separate secret expected on this camera's motion and proxy URLs.
- `motion_hold`: seconds for which the motion entity remains on.
- `snapshot_interval`: seconds between JPEG snapshot updates.
- `telemetry_interval`: seconds between system/stream information updates.
- `mqtt_topic`: common MQTT topic root; camera-specific suffixes are automatic.

The old single-camera keys remain supported for upgrades, but new installations
should use `cameras`.

The add-on obtains MQTT host, port and credentials from Home Assistant's MQTT
service automatically. Port `8766` is exposed only so the camera can report a
motion event.

## Camera setup

For each camera, copy its exact motion URL from the add-on log. Then:

1. Open **HTTP server** in that camera's manager, select a free slot, set the
   server to the Home Assistant host IP and the port to `8766`. Set
   authorization to **No**.
2. Open **Alarm sending**, enable HTTP event delivery, select that server slot,
   choose **Always**, and enter the path printed by the add-on, for example
   `camera/front_door_902c01/motion?token=YOUR_TOKEN`.
3. Enable and draw at least one region on the camera's **Motion** page.

Home Assistant discovers motion, a JPEG camera, a **Refresh snapshot** button,
last-update time and diagnostic sensors for firmware, uptime, network, storage,
stream resolution/frame rate/bitrate and response time.

## Snapshot refresh and live stream

Press **Refresh snapshot** on the camera device to request and publish a new
JPEG immediately. Periodic refresh continues according to `snapshot_interval`.

MQTT Camera carries JPEG images only; it cannot advertise a true stream. To add
the verified MJPEG stream, create a **Generic Camera** helper/integration using
the two URLs printed in the add-on log:

- Still image URL: `http://HA_IP:8766/camera/CAMERA_ID/snapshot.jpg?token=TOKEN`
- Stream source URL: `http://HA_IP:8766/camera/CAMERA_ID/stream.mjpeg?token=TOKEN`

The add-on authenticates to the old camera, so Home Assistant does not need the
camera's web password. Keep port 8766 on the trusted local network only.
