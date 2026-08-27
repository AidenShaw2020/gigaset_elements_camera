#!/usr/bin/env python3
"""Development entry point for the optional Home Assistant MQTT bridge."""

from pathlib import Path
import runpy


_implementation = Path(__file__).parent / "homeassistant_addon" / "gigaset_camera_mqtt" / "app" / "mqtt_bridge.py"
_namespace = runpy.run_path(str(_implementation))
for _name, _value in _namespace.items():
    if not _name.startswith("__"):
        globals()[_name] = _value


if __name__ == "__main__":
    main()
