#!/bin/sh
set -eu

PARTITION_START=19529728
PARTITION_END=88735744
PARTITION_SIZE=69206016
PEB_SIZE=131072
LEB_SIZE=126976
MIN_IO=2048
IMAGE_SEQ=879885045
VOLUME_SIZE=63995904

usage()
{
        echo "Usage: $0 FULL_NAND_DUMP OUTPUT_ROOTFS_UBI" >&2
        exit 2
}

[ "$#" -eq 2 ] || usage
DUMP=$1
OUTPUT=$2
SCRIPT_DIR=`CDPATH= cd -- "$(dirname -- "$0")" && pwd`

[ -f "$DUMP" ] || { echo "Input dump not found: $DUMP" >&2; exit 1; }
[ `wc -c < "$DUMP"` -ge "$PARTITION_END" ] || { echo "Input is shorter than the rootfs partition end" >&2; exit 1; }

for tool in python3 ubireader_extract_files mkfs.ubifs ubinize sha256sum
do
        command -v "$tool" >/dev/null 2>&1 || { echo "Missing required tool: $tool" >&2; exit 1; }
done

WORK_DIR=`mktemp -d "${TMPDIR:-/tmp}/gigaset-s2l-rootfs.XXXXXX"`
trap 'rm -rf "$WORK_DIR"' EXIT HUP INT TERM
EXTRACT_DIR=$WORK_DIR/extracted
/bin/mkdir -p "$EXTRACT_DIR"

echo "Extracting the stock rootfs with Linux permissions and symlinks..."
ubireader_extract_files -k -p "$PEB_SIZE" -s "$PARTITION_START" -n "$PARTITION_END" -o "$EXTRACT_DIR" "$DUMP"
ROOTFS=`find "$EXTRACT_DIR" -mindepth 2 -maxdepth 2 -type d -name rootfs | head -n 1`
[ -n "$ROOTFS" ] || { echo "ubi-reader did not extract a rootfs volume" >&2; exit 1; }

python3 "$SCRIPT_DIR/patch_rootfs.py" "$ROOTFS"

echo "Building UBIFS..."
mkfs.ubifs \
        -r "$ROOTFS" \
        -o "$WORK_DIR/rootfs.ubifs" \
        -m "$MIN_IO" \
        -e "$LEB_SIZE" \
        -c 2047 \
        -x lzo \
        -f 8 \
        -k r5 \
        -l 5 \
        -p 1 \
        -j 8388608

cat > "$WORK_DIR/ubinize.ini" <<EOF
[rootfs]
mode=ubi
image=$WORK_DIR/rootfs.ubifs
vol_id=0
vol_type=dynamic
vol_name=rootfs
vol_alignment=1
vol_size=$VOLUME_SIZE
EOF

echo "Building UBI partition image..."
ubinize \
        -o "$WORK_DIR/rootfs.ubi" \
        -p "$PEB_SIZE" \
        -m "$MIN_IO" \
        -s "$MIN_IO" \
        -O "$MIN_IO" \
        -Q "$IMAGE_SEQ" \
        "$WORK_DIR/ubinize.ini"

python3 - "$WORK_DIR/rootfs.ubi" "$OUTPUT" "$PARTITION_SIZE" <<'PY'
import hashlib
import json
import os
import pathlib
import shutil
import sys

source = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])
partition_size = int(sys.argv[3])
image_size = source.stat().st_size
if image_size > partition_size:
    raise SystemExit(f"rebuilt UBI is too large: {image_size} > {partition_size}")

output.parent.mkdir(parents=True, exist_ok=True)
temporary = output.with_name(output.name + ".new")
with source.open("rb") as src, temporary.open("wb") as dst:
    shutil.copyfileobj(src, dst, 1024 * 1024)
    remaining = partition_size - image_size
    block = b"\xff" * (1024 * 1024)
    while remaining:
        chunk = block[: min(remaining, len(block))]
        dst.write(chunk)
        remaining -= len(chunk)
os.replace(temporary, output)

digest = hashlib.sha256()
with output.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
manifest = {
    "artifact": output.name,
    "partition": "rootfs",
    "start": 19529728,
    "end": 88735744,
    "size": partition_size,
    "sha256": digest.hexdigest(),
    "source": "user-supplied NAND dump; contains vendor firmware and must not be redistributed",
}
output.with_name(output.name + ".json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {output} ({partition_size} bytes)")
print(f"SHA-256 {digest.hexdigest()}")
PY

echo "Done. Keep the generated image private; it contains the camera's vendor firmware."
