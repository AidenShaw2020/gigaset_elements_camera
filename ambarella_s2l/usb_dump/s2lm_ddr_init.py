import argparse
import hashlib
import struct
import sys
import time
import zipfile

import libusb_package
import usb.core
import usb.util


VID = 0x4255
PID = 0x000A
SOC_PROFILE = "S2Lm"

READ_TYPE = 0xC0
WRITE_TYPE = 0x40
REQUEST = 0x00
TIMEOUT_MS = 1000

WRITE_OPCODE = 0x30000000

ADS_MEMBER = "platform/s2l/s2lm_ddr3.ads"
ADS_SHA256 = "a8204b7da284ff91946eae4c7f5bf6cbba1e1e40494f04429f5b205342ac6ec7"

RAM_TEST_ADDR = 0x00100000
RAM_TEST_WORDS = 16
POLL_TIMEOUT_S = 10.0


def get_backend():
    backend = libusb_package.get_libusb1_backend()
    if backend is None:
        raise RuntimeError("Could not load libusb backend.")
    return backend


def find_device():
    return usb.core.find(
        idVendor=VID,
        idProduct=PID,
        backend=get_backend(),
    )


def read32(dev, address):
    data = dev.ctrl_transfer(
        READ_TYPE,
        REQUEST,
        (address >> 16) & 0xFFFF,
        address & 0xFFFF,
        4,
        timeout=TIMEOUT_MS,
    )

    raw = bytes(data)

    if len(raw) != 4:
        raise RuntimeError(
            "read32(0x%08X): expected 4 bytes, got %d"
            % (address, len(raw))
        )

    return int.from_bytes(raw, "little")


def write32(dev, address, value):
    payload = struct.pack(
        "<III",
        WRITE_OPCODE,
        address & 0xFFFFFFFF,
        value & 0xFFFFFFFF,
    )

    sent = dev.ctrl_transfer(
        WRITE_TYPE,
        REQUEST,
        0,
        0,
        payload,
        timeout=TIMEOUT_MS,
    )

    if sent != len(payload):
        raise RuntimeError(
            "write32(0x%08X): sent %d of %d bytes"
            % (address, sent, len(payload))
        )


def load_ads(zip_path):
    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            raw = zf.read(ADS_MEMBER)
        except KeyError:
            raise RuntimeError(
                "Could not find %s inside %s"
                % (ADS_MEMBER, zip_path)
            )

    digest = hashlib.sha256(raw).hexdigest()

    if digest.lower() != ADS_SHA256.lower():
        raise RuntimeError(
            "Unexpected SHA256 for %s\n"
            "Expected: %s\n"
            "Actual:   %s"
            % (ADS_MEMBER, ADS_SHA256, digest)
        )

    return raw.decode("ascii")


def parse_ads(text):
    ops = []

    for lineno, original in enumerate(text.splitlines(), 1):
        line = original.strip()

        if not line or line.startswith("#"):
            continue

        parts = line.split(None, 1)

        if len(parts) != 2:
            raise RuntimeError(
                "Invalid ADS line %d: %s"
                % (lineno, original)
            )

        command, args = parts
        command = command.lower()

        if command == "write":
            a, v = [int(x.strip(), 0) for x in args.split(",", 1)]
            ops.append(("write", a, v, lineno))

        elif command == "usleep":
            ops.append(("usleep", int(args.strip(), 0), lineno))

        elif command == "poll":
            fields = [int(x.strip(), 0) for x in args.split(",")]

            if len(fields) != 3:
                raise RuntimeError(
                    "Invalid poll line %d: %s"
                    % (lineno, original)
                )

            a, mask, expected = fields
            ops.append(("poll", a, mask, expected, lineno))

        else:
            raise RuntimeError(
                "Unsupported ADS command on line %d: %s"
                % (lineno, command)
            )

    return ops


def print_device(dev):
    print("Device:       %04X:%04X" % (dev.idVendor, dev.idProduct))

    for label, index in [
        ("Manufacturer", dev.iManufacturer),
        ("Product", dev.iProduct),
        ("Serial", dev.iSerialNumber),
    ]:
        try:
            value = usb.util.get_string(dev, index) if index else ""
        except Exception:
            value = "?"

        print("%-12s %s" % (label + ":", value))


def preflight(dev):
    print()
    print("Preflight register reads")
    print("------------------------")

    checks = [
        (0xEC170000, "RCT core"),
        (0xEC170050, "USB PHY"),
        (0xEC1700DC, "DDR PLL"),
        (0xDFFE0800, "DRAM control"),
        (0xDFFE0804, "DRAM timing 0"),
        (0xDFFE0818, "DRAM command"),
    ]

    values = {}

    for address, name in checks:
        value = read32(dev, address)
        values[address] = value

        print(
            "%-14s 0x%08X = 0x%08X"
            % (name, address, value)
        )

    if values[0xEC170000] == 0:
        raise RuntimeError(
            "RCT core register returned zero. "
            "USB read32 preflight does not look right."
        )

    return values


def print_plan(ops):
    writes = sum(1 for op in ops if op[0] == "write")
    sleeps = sum(1 for op in ops if op[0] == "usleep")
    polls = sum(1 for op in ops if op[0] == "poll")

    print()
    print("%s DDR3 init plan" % SOC_PROFILE)
    print("-------------------")
    print("ADS:       %s" % ADS_MEMBER)
    print("SHA256:    %s" % ADS_SHA256)
    print("Writes:    %d" % writes)
    print("Sleeps:    %d" % sleeps)
    print("Polls:     %d" % polls)
    print()
    print("This changes ONLY SoC clock/DDR controller registers.")
    print("It does NOT upload a NAND loader.")
    print("It does NOT send NAND erase/program/write commands.")
    print("A power cycle returns the camera to the original state.")


def wait_poll(dev, address, mask, expected, timeout_s):
    start = time.monotonic()
    last = None

    while True:
        value = read32(dev, address)
        last = value

        if (value & mask) == expected:
            return value

        if time.monotonic() - start >= timeout_s:
            raise TimeoutError(
                "poll timeout at 0x%08X: "
                "value=0x%08X mask=0x%08X expected=0x%08X"
                % (address, last, mask, expected)
            )

        time.sleep(0.001)


def apply_ads(dev, ops):
    print()
    print("Applying Ambarella %s DDR3 init" % SOC_PROFILE)
    print("---------------------------------")

    first_write_checked = False

    for index, op in enumerate(ops, 1):
        kind = op[0]

        if kind == "write":
            _, address, value, lineno = op

            print(
                "[%02d/%02d] write  0x%08X <- 0x%08X"
                % (index, len(ops), address, value)
            )

            write32(dev, address, value)

            if not first_write_checked:
                time.sleep(0.01)
                got = read32(dev, address)

                print(
                    "           first-write readback: 0x%08X"
                    % got
                )

                if got != value:
                    raise RuntimeError(
                        "First write did not read back as expected.\n"
                        "Wanted 0x%08X, got 0x%08X.\n"
                        "Aborting before further DDR writes."
                        % (value, got)
                    )

                print("           write32 protocol verified.")
                first_write_checked = True

        elif kind == "usleep":
            _, usec, lineno = op

            print(
                "[%02d/%02d] sleep  %.3f s"
                % (index, len(ops), usec / 1_000_000.0)
            )

            time.sleep(usec / 1_000_000.0)

        elif kind == "poll":
            _, address, mask, expected, lineno = op

            print(
                "[%02d/%02d] poll   0x%08X "
                "& 0x%08X == 0x%08X"
                % (
                    index,
                    len(ops),
                    address,
                    mask,
                    expected,
                )
            )

            value = wait_poll(
                dev,
                address,
                mask,
                expected,
                POLL_TIMEOUT_S,
            )

            print(
                "           done, value=0x%08X"
                % value
            )


def ram_test(dev):
    print()
    print("DDR RAM read/write/restore test")
    print("-------------------------------")
    print(
        "Address: 0x%08X, %d words"
        % (RAM_TEST_ADDR, RAM_TEST_WORDS)
    )

    addresses = [
        RAM_TEST_ADDR + i * 4
        for i in range(RAM_TEST_WORDS)
    ]

    original = [read32(dev, a) for a in addresses]

    patterns = [
        0xA5A50000 ^ i ^ (i << 16)
        for i in range(RAM_TEST_WORDS)
    ]

    try:
        for address, value in zip(addresses, patterns):
            write32(dev, address, value)

        actual = [read32(dev, a) for a in addresses]

        mismatches = []

        for i, (wanted, got) in enumerate(zip(patterns, actual)):
            if wanted != got:
                mismatches.append(
                    (
                        addresses[i],
                        wanted,
                        got,
                    )
                )

        if mismatches:
            print("RAM TEST FAILED:")

            for address, wanted, got in mismatches:
                print(
                    "  0x%08X wanted=0x%08X got=0x%08X"
                    % (address, wanted, got)
                )

            raise RuntimeError(
                "DDR RAM did not retain the test pattern."
            )

        print("RAM test pattern verified successfully.")

    finally:
        for address, value in zip(addresses, original):
            try:
                write32(dev, address, value)
            except Exception as exc:
                print(
                    "WARNING: restore failed at 0x%08X: %s"
                    % (address, exc)
                )

    restored = [read32(dev, a) for a in addresses]

    if restored == original:
        print("Original RAM contents restored successfully.")
    else:
        print(
            "WARNING: RAM restore verification did not match."
        )


def postflight(dev):
    print()
    print("Post-init registers")
    print("-------------------")

    for address, name in [
        (0xEC1700DC, "DDR PLL"),
        (0xDFFE0800, "DRAM control"),
        (0xDFFE0804, "DRAM timing 0"),
        (0xDFFE0808, "DRAM timing 1"),
        (0xDFFE0810, "DRAM timing 3"),
        (0xDFFE0820, "DRAM config"),
        (0xDFFE0828, "DRAM config 2"),
    ]:
        value = read32(dev, address)

        print(
            "%-14s 0x%08X = 0x%08X"
            % (name, address, value)
        )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Initialize DDR3 on Ambarella %s/A12 USB BootROM "
            "using the official AmbaUSB ADS file."
        ) % SOC_PROFILE
    )

    parser.add_argument(
        "platform_zip",
        help="Path to platform.zip from AmbaUSB",
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply DDR initialization",
    )

    args = parser.parse_args()

    print("Ambarella %s DDR3 initializer" % SOC_PROFILE)
    print("===============================")
    print()
    print("Target: 4255:000A / A12 USB BootROM")
    print()

    try:
        ads_text = load_ads(args.platform_zip)
        ops = parse_ads(ads_text)

    except Exception as exc:
        print("ERROR loading ADS:", exc)
        sys.exit(1)

    dev = find_device()

    if dev is None:
        print("ERROR: Ambarella 4255:000A not found.")
        sys.exit(2)

    print_device(dev)

    try:
        preflight(dev)

    except Exception as exc:
        print()
        print("PREFLIGHT FAILED:", exc)
        sys.exit(3)

    print_plan(ops)

    if not args.apply:
        print()
        print("DRY RUN ONLY.")
        print()
        print("Nothing was written.")
        print("If the values above look correct, run:")
        print()
        print(
            '  py .\\s2lm_ddr_init.py "%s" --apply'
            % args.platform_zip
        )
        return

    print()
    print("WARNING:")
    print("The next step writes clock/DDR controller registers.")
    print("It still does NOT touch NAND flash.")
    print()

    confirmation = input(
        "Type exactly INIT to continue: "
    ).strip()

    if confirmation != "INIT":
        print("Cancelled. Nothing was written.")
        return

    try:
        apply_ads(dev, ops)
        postflight(dev)
        ram_test(dev)

    except usb.core.USBError as exc:
        print()
        print("USB ERROR:", exc)
        print(
            "Power-cycle the camera back into USB boot mode "
            "before retrying."
        )
        sys.exit(4)

    except Exception as exc:
        print()
        print("ERROR:", exc)
        print(
            "Power-cycle the camera back into USB boot mode. "
            "No NAND operation was performed."
        )
        sys.exit(5)

    print()
    print("======================================")
    print("SUCCESS: %s DDR3 is initialized." % SOC_PROFILE)
    print("DDR read/write test passed.")
    print("No NAND command was sent.")
    print("No NAND loader was uploaded.")
    print("======================================")


if __name__ == "__main__":
    main()
