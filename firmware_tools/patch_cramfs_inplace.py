import argparse
import math
import stat
import struct
import sys
import zlib
from pathlib import Path

import cramfs_extract


def patch_file_many(
    image: bytearray,
    fs: cramfs_extract.Cramfs,
    path: str,
    replacements: list[tuple[bytes, bytes]],
) -> bool:
    nodes = fs.walk()
    node = nodes.get(path)
    if node is None:
        raise ValueError(f"{path}: not found")
    if stat.S_ISDIR(node.mode):
        raise ValueError(f"{path}: is directory")

    content = fs.read_file(node)
    patched = content
    for old, new in replacements:
        if len(old) != len(new):
            raise ValueError(f"{path}: old/new lengths differ: {len(old)} != {len(new)}")
        if old not in patched:
            raise ValueError(f"{path}: old bytes not found: {old!r}")
        patched = patched.replace(old, new, 1)
    if len(patched) != node.size:
        raise ValueError(f"{path}: replacement changed file size")

    blocks = max(1, math.ceil(node.size / cramfs_extract.PAGE_SIZE))
    table = fs.base + node.offset
    data_start = table + blocks * 4
    cursor = data_start

    output_chunks = []
    for block_index in range(blocks):
        old_end = fs.base + struct.unpack_from("<I", image, table + block_index * 4)[0]
        plain = patched[
            block_index * cramfs_extract.PAGE_SIZE : (block_index + 1) * cramfs_extract.PAGE_SIZE
        ]
        compressed = zlib.compress(plain, 9)
        old_len = old_end - cursor
        if len(compressed) > old_len:
            raise ValueError(
                f"{path}: block {block_index} compressed grew {old_len}->{len(compressed)}"
            )
        output_chunks.append((cursor, old_end, compressed))
        cursor = old_end

    for start, end, compressed in output_chunks:
        image[start : start + len(compressed)] = compressed
        image[start + len(compressed) : end] = b"\x00" * (end - start - len(compressed))

    print(f"{path}: patched")
    return True


def patch_file(image: bytearray, fs: cramfs_extract.Cramfs, path: str, old: bytes, new: bytes) -> bool:
    return patch_file_many(image, fs, path, [(old, new)])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--passwd-only", action="store_true")
    parser.add_argument("--skip-passwd", action="store_true")
    parser.add_argument("--shell-console", action="store_true")
    parser.add_argument(
        "--telnet",
        action="store_true",
        help="start BusyBox telnetd once at boot; login uses /etc/passwd",
    )
    parser.add_argument(
        "--pty-setup",
        action="store_true",
        help="create writable /dev with ptmx, mount devpts, then start authenticated telnet",
    )
    parser.add_argument(
        "--old-hash",
        default="$1$DsTkjczU$y5E5A5mLXDXdX5OTQrIwU/",
        help="existing root password hash in /etc/passwd",
    )
    parser.add_argument(
        "--new-hash",
        default="$1$DsTkjczU$LZQZT3rcdcYFeZl9EUvbd/",
        help="replacement root password hash (must have the same length)",
    )
    args = parser.parse_args()

    data = bytearray(args.input.read_bytes())
    image_for_fs = bytes(data) if args.raw else cramfs_extract.wordswap4(bytes(data))
    fs = cramfs_extract.Cramfs(image_for_fs)

    if not args.raw:
        raise SystemExit("this patcher currently expects --raw images")

    old_hash = args.old_hash.encode("ascii")
    new_hash = args.new_hash.encode("ascii")
    if not args.skip_passwd:
        patch_file(data, fs, "/etc/passwd", old_hash, new_hash)

    inittab_patches = []
    if args.shell_console:
        old_getty = b"::respawn:/sbin/getty -L ttyS0 115200 vt100"
        new_shell = b"::respawn:-/bin/sh" + b" " * (len(old_getty) - len(b"::respawn:-/bin/sh"))
        inittab_patches.append((old_getty, new_shell))

    if args.telnet:
        inittab_patches.append(
            (b"#::sysinit:/etc/init.d/rcS", b"::once:/sbin/telnetd -p 23")
        )

    if args.pty_setup:
        old_shell = b"::respawn:-/bin/sh" + b" " * 25
        inittab_patches.extend(
            [
                (
                    b"# This is run first except when booting in single-user mode.",
                    b"::wait:/bin/cp -a /dev/* /var/dev" + b" " * 27,
                ),
                (
                    b"::wait:/etc/init.d/rc 3",
                    b"::respawn:-/bin/sh" + b" " * 5,
                ),
                (
                    old_shell,
                    b"::wait:/bin/mount --bind /var/dev /dev" + b" " * 5,
                ),
                (
                    b"# Stuff to do when restarting the init process",
                    b"::wait:/bin/mknod /dev/ptmx c 5 2" + b" " * 13,
                ),
                (
                    b"# Stuff to do before rebooting",
                    b"::wait:/etc/init.d/rc 3" + b" " * 7,
                ),
            ]
        )

        old_dirs = (
            b"mkdir /var/tmp/image/alarmsmtp /var/tmp/image/alarmftp "
            b"/var/tmp/image/periodsmtp /var/tmp/image/periodftp"
        )
        new_dirs = (
            b"cd /var/tmp/image;mkdir alarmsmtp alarmftp periodsmtp periodftp;"
            b"mount -t devpts x /var/pts"
        )
        new_dirs += b" " * (len(old_dirs) - len(new_dirs))
        old_comment = b"# Mount /proc (done here so volume labels can work with fsck)"
        new_comment = b"#" + b" " * (len(old_comment) - 1)
        patch_file_many(
            data,
            fs,
            "/etc/init.d/rc.sysinit",
            [(old_dirs, new_dirs), (old_comment, new_comment)],
        )

    if inittab_patches:
        patch_file_many(data, fs, "/etc/inittab", inittab_patches)

    if not args.passwd_only:
        patch_file(data, fs, "/etc/default/sys.conf", b"watchdog=automatic", b"watchdog=manual   ")
        patch_file(data, fs, "/etc/default/sys.conf", b"ENABLE=disable", b"ENABLE=enable ")

    args.output.write_bytes(data)
    print(f"wrote {args.output} size={len(data)}")


if __name__ == "__main__":
    main()
