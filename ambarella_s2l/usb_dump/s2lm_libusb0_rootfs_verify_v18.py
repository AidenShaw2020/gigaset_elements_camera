"""Read back and verify the S2Lm rootfs after a fresh RAM-loader start."""

import argparse
import binascii
import hashlib
import json
from pathlib import Path

import usb.util

import s2lm_libusb0_rootfs_writer_v17 as writer


NAND_SIZE = 0x08000000
LNX_PHYSICAL_START = 0x012A0000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    full, payload = writer.prepare_payload(args.image)
    program_payload = writer.prepare_program_payload(payload)
    full_sha = hashlib.sha256(full).hexdigest()
    payload_sha = hashlib.sha256(payload).hexdigest()
    if args.expected_sha256 and full_sha.lower() != args.expected_sha256.lower():
        raise SystemExit(
            "Artifact SHA256 mismatch: expected %s got %s"
            % (args.expected_sha256, full_sha)
        )

    print("Ambarella S2Lm rootfs NAND readback verifier")
    print("=============================================")
    print("Artifact SHA256: %s" % full_sha)
    print("Payload size:    0x%X" % len(payload))
    print("Payload SHA256:  %s" % payload_sha)

    dev = writer.tool.find_device(writer.tool.PID_BLD)
    if dev is None:
        raise SystemExit("Fresh RAM BLD 4255:0001 not found")
    writer.tool.show_device(dev)
    ep_out, ep_in = writer.transport._bulk_endpoints(dev)

    ptb = writer.get_ptb(dev, ep_out, ep_in)
    meta = writer.lnx_metadata(ptb)
    expected_crc = binascii.crc32(program_payload) & 0xFFFFFFFF
    print(
        "PTB LNX:         len=0x%X crc=0x%08X"
        % (meta["img_len"], meta["crc32"])
    )
    if meta["img_len"] != len(program_payload) or meta["crc32"] != expected_crc:
        raise RuntimeError("PTB LNX metadata does not match the expected payload")
    print("PTB metadata:    OK")

    digest = hashlib.sha256()
    done = 0
    physical = LNX_PHYSICAL_START
    while done < len(payload):
        count = min(writer.RAW_READ_CHUNK, len(payload) - done)
        chunk = writer.read_nand_chunk(
            dev,
            ep_out,
            ep_in,
            physical,
            count,
        )
        expected_chunk = payload[done : done + count]
        if chunk != expected_chunk:
            first_bad = next(
                (
                    index
                    for index in range(0, count, writer.ERASEBLOCK)
                    if chunk[index : index + writer.ERASEBLOCK]
                    != expected_chunk[index : index + writer.ERASEBLOCK]
                ),
                0,
            )
            raise RuntimeError(
                "NAND content mismatch at semantic PEB %d (physical 0x%08X)"
                % (
                    (done + first_bad) // writer.ERASEBLOCK,
                    (physical + first_bad) % NAND_SIZE,
                )
            )
        digest.update(chunk)
        done += count
        print(
            "\rReading contiguous rootfs partition: %6.2f%%"
            % (100.0 * done / len(payload)),
            end="",
            flush=True,
        )

        physical += count
    print()
    nand_sha = digest.hexdigest()
    if nand_sha != payload_sha:
        raise RuntimeError(
            "Wrapped NAND readback SHA256 mismatch: expected %s got %s"
            % (payload_sha, nand_sha)
        )
    print("NAND SHA256:     %s" % nand_sha)
    report = {
        "format": "s2lm-rootfs-verify-v18",
        "artifact": str(args.image),
        "artifact_sha256": full_sha,
        "payload_size": len(payload),
        "payload_sha256": payload_sha,
        "program_payload_size": len(program_payload),
        "program_payload_sha256": hashlib.sha256(program_payload).hexdigest(),
        "nand_readback_sha256": nand_sha,
        "nand_size": NAND_SIZE,
        "lnx_physical_start": LNX_PHYSICAL_START,
        "wraps_at_nand_end": False,
        "verification_method": "exact contiguous UBI byte sequence in camera PART_LNX",
        "ptb_sha256": hashlib.sha256(ptb).hexdigest(),
        "verified": True,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("Report:          %s" % args.report)
    print("ROOTFS_NAND_VERIFY_OK")

    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass


if __name__ == "__main__":
    main()
