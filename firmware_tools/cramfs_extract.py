import argparse
import stat
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path


MAGIC = b"\x45\x3d\xcd\x28"
PAGE_SIZE = 4096
INODE_SIZE = 16  # Grain Media variant adds one 32-bit field to cramfs_inode.


@dataclass
class Inode:
    path: str
    mode: int
    uid: int
    gid: int
    size: int
    offset: int


class Cramfs:
    def __init__(self, image: bytes):
        self.image = image
        self.base = image.find(MAGIC)
        if self.base < 0:
            raise ValueError("CRAMFS magic not found")
        self.fs_size = struct.unpack_from("<I", image, self.base + 4)[0]
        self.limit = self.base + self.fs_size
        if self.limit > len(image):
            raise ValueError("truncated CRAMFS image")

    def inode_at(self, position: int, path: str) -> tuple[Inode, int]:
        if position + INODE_SIZE > self.limit:
            raise ValueError(f"inode outside filesystem at 0x{position:X}")
        word0, word1, word2 = struct.unpack_from("<III", self.image, position)
        name_storage = (word2 & 0x3F) << 2
        inode = Inode(
            path=path,
            mode=word0 & 0xFFFF,
            uid=word0 >> 16,
            gid=word1 >> 24,
            size=word1 & 0xFFFFFF,
            offset=(word2 >> 6) << 2,
        )
        return inode, name_storage

    def root(self) -> Inode:
        inode, _ = self.inode_at(self.base + 64, "/")
        return inode

    def children(self, directory: Inode) -> list[Inode]:
        start = self.base + directory.offset
        end = start + directory.size
        if start < self.base or end > self.limit:
            raise ValueError(f"invalid directory range for {directory.path}")

        result = []
        position = start
        while position + INODE_SIZE <= end:
            inode, name_storage = self.inode_at(position, "")
            if name_storage == 0 or position + INODE_SIZE + name_storage > end:
                raise ValueError(f"invalid name at 0x{position:X} in {directory.path}")
            raw_name = self.image[
                position + INODE_SIZE : position + INODE_SIZE + name_storage
            ]
            name = raw_name.split(b"\x00", 1)[0].decode("utf-8", "replace")
            parent = directory.path.rstrip("/")
            inode.path = f"{parent}/{name}" if parent else f"/{name}"
            result.append(inode)
            position += INODE_SIZE + name_storage
        return result

    def walk(self) -> dict[str, Inode]:
        nodes: dict[str, Inode] = {}

        def visit(node: Inode) -> None:
            nodes[node.path] = node
            if stat.S_ISDIR(node.mode):
                try:
                    children = self.children(node)
                except Exception as error:
                    print(f"WALK ERROR {node.path}: {error}")
                    return
                for child in children:
                    visit(child)

        visit(self.root())
        return nodes

    def read_file(self, inode: Inode) -> bytes:
        if stat.S_ISDIR(inode.mode):
            raise ValueError(f"{inode.path} is a directory")
        if inode.size == 0:
            return b""

        block_count = (inode.size + PAGE_SIZE - 1) // PAGE_SIZE
        table = self.base + inode.offset
        data_start = table + block_count * 4
        output = bytearray()

        for block_index in range(block_count):
            end_relative = struct.unpack_from("<I", self.image, table + block_index * 4)[0]
            data_end = self.base + end_relative
            if data_end < data_start or data_end > self.limit:
                raise ValueError(
                    f"bad block pointer in {inode.path}, block {block_index}: 0x{data_end:X}"
                )
            compressed = self.image[data_start:data_end]
            if compressed:
                output.extend(zlib.decompress(compressed))
            else:
                output.extend(b"\x00" * PAGE_SIZE)
            data_start = data_end

        return bytes(output[: inode.size])


def wordswap4(data: bytes) -> bytes:
    if len(data) % 4:
        raise ValueError("input length is not divisible by four")
    output = bytearray(len(data))
    for offset in range(0, len(data), 4):
        output[offset : offset + 4] = data[offset : offset + 4][::-1]
    return bytes(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--raw", action="store_true", help="image already has normal byte order")
    parser.add_argument("--extract-dir", type=Path)
    args = parser.parse_args()

    image = args.image.read_bytes()
    if not args.raw:
        image = wordswap4(image)

    filesystem = Cramfs(image)
    nodes = filesystem.walk()
    print(
        f"filesystem offset=0x{filesystem.base:08X} "
        f"size=0x{filesystem.fs_size:X} nodes={len(nodes)}"
    )

    if args.extract_dir is not None:
        extracted = 0
        failed = 0
        for path, node in nodes.items():
            relative = path.lstrip("/")
            destination = args.extract_dir / relative
            if stat.S_ISDIR(node.mode):
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                content = filesystem.read_file(node)
                destination.write_bytes(content)
                extracted += 1
            except Exception as error:
                failed += 1
                print(f"EXTRACT ERROR {path}: {error}")
        print(f"extracted={extracted} failed={failed} destination={args.extract_dir}")

    for requested in args.paths:
        node = nodes.get(requested)
        if node is None:
            print(f"{requested}: not found")
            continue
        print(
            f"--- {requested} mode=0{node.mode:o} uid={node.uid} "
            f"gid={node.gid} size={node.size} offset=0x{node.offset:X}"
        )
        try:
            content = filesystem.read_file(node)
        except Exception as error:
            print(f"ERROR: {error}")
            continue
        print(content.decode("utf-8", "backslashreplace"))


if __name__ == "__main__":
    main()
