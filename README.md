# Gigaset Elements Camera

![Gigaset Elements Camera community project](docs/assets/gigaset-elements-camera-banner.png)

Open, source-only tooling for keeping discontinued Gigaset/Y-cam cameras useful
on a local network after the vendor cloud shutdown.

Two cameras sold under the Gigaset Elements name use completely different
hardware. Select the model before following any instructions:

| Camera | Platform | Project state | Documentation |
| --- | --- | --- | --- |
| Original camera / Gen1 | GM8126, 8 MiB W25Q64FV SPI flash | **Supported and hardware-validated** | [GM8126/Gen1 guide](docs/cameras/gm8126-gen1.md) |
| S30851-H2531-R101 | Ambarella S2L/S2Lm, 128 MiB NAND | **Research only; recovery and installation are not final** | [S30851-H2531-R101 research notes](docs/cameras/s30851-h2531-r101.md) |

> [!CAUTION]
> Do not use Gen1 MEF/MyLoader images on the Ambarella camera. Do not use the
> experimental Ambarella NAND writers on another camera. The current S2L
> development unit has a damaged NAND bootloader and does not boot normally;
> only its read-only dump route has been validated.

## Repository layout

```text
firmware_tools/                 Gen1 MEF patching and partition tools
payload/                        Gen1 auditable RAM flash-read payload
bootloader_dump*.py             Gen1 non-destructive dump entry points
cloudless_manager.py            Gen1 stock-camera cloud manager
ambarella_s2l/                  Newer-camera research and source prototypes
ambarella_s2l/usb_dump/         Read-only NAND path and experimental recovery work
gigaset_elements_camera/        Optional Home Assistant add-on
docs/cameras/                   Separate hardware guides and support status
docs/assets/                    Project artwork and hardware photographs
tests/                          Host-side unit and safety tests
```

The established Gen1 command paths remain at the repository root for backward
compatibility. Platform-specific instructions are intentionally separated in
`docs/cameras` so that commands cannot be confused between the two models.

## Original GM8126 / Gen1 camera

The complete Gen1 workflow is supported:

- two-pass 8 MiB flash dump without desoldering;
- source-only firmware patch built from the user's own dump;
- local dashboard, live view and motion-zone editor;
- configurable admin/root passwords and physical factory reset;
- vendor cloud disabled by default;
- optional authenticated Telnet;
- stock-camera cloudless manager and Home Assistant gateway.

Hardware validation used two independent W25Q64FV cameras with firmware
`1.10 (build 20140802)`. Start with the
[GM8126/Gen1 guide](docs/cameras/gm8126-gen1.md).

## Newer S30851-H2531-R101 camera

The newer camera has its own Ambarella boot chain, NAND layout, credentials,
RTSP service and USB recovery mechanism. Work completed so far includes:

- identification of the PCB USB-boot service pad;
- a source-only stock-password implementation;
- an Ambarella model profile in the Home Assistant add-on;
- a hardware-validated, read-only 128 MiB NAND acquisition path;
- source prototypes for the stock USB service disk and Wi-Fi fallback.

The normal installation and bootloader restore paths are unfinished. The
research unit currently has damaged BST/BLD data after a generic RAM loader
used an incompatible partition geometry. Photographs, the marked resistor pad,
the exact current state and the recovery plan are in the
[S30851-H2531-R101 research notes](docs/cameras/s30851-h2531-r101.md).

## Home Assistant add-on

Add this repository URL to the Home Assistant add-on store:

```text
https://github.com/AidenShaw2020/gigaset_elements_camera
```

Install **Gigaset elements camera local gateway**. One add-on instance supports
multiple cameras. For the stable Gen1 model it publishes Home Assistant MQTT
discovery for availability, motion, refreshable JPEG snapshots and diagnostic
telemetry, and exposes authenticated snapshot/MJPEG proxy URLs.

An Ambarella S2L model profile is present, including its password algorithm,
stock snapshot endpoint and RTSP-to-MJPEG proxy. Treat that profile as pending
hardware acceptance until it is tested on a normally booting camera. Automatic
Oryx motion configuration is not implemented yet.

See the [add-on documentation](gigaset_elements_camera/DOCS.md).

## Install host dependencies

From the repository root:

```powershell
python -m pip install -r requirements.txt
```

Run the host-side tests with:

```powershell
python -m unittest discover -s tests -v
```

## Safety and legal model

This repository intentionally contains **no vendor firmware, flash/NAND dump,
generated partition image, extracted filesystem, platform archive, BLD/ADS
binary, key, certificate, serial number, MAC address or device configuration**.

Users read their own camera and keep recovery material private. Generated
artifacts are ignored by Git. Never substitute a dump from another camera or
publish device-specific files.

The original web interfaces are HTTP-only. Keep cameras on a trusted or
isolated LAN and choose unique passwords after recovery.

## Related project

For the Gigaset Elements base station and sensors, see
[Gigaset Elements Emulator](https://github.com/AidenShaw2020/gigaset_elements_emulator),
a self-hosted replacement for the discontinued cloud with MQTT and Home
Assistant integration.

Licensed under the MIT License. This community project is independent and is
not affiliated with or endorsed by Gigaset or Y-cam.
