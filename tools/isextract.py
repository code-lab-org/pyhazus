"""Extract the files embedded in an InstallShield "Setup Launcher" (ISSetupStream) executable.

The Hazus 7.x installers (setup.7.0.exe, Hazus.7.2.patch.exe) are InstallShield launchers whose
payload (the .msi / .msp and setup ini files) is appended to the PE image as an "ISSetupStream".

Stream layout (type 4, unicode launcher):
    header (46 bytes): b"ISSetupStream\\0", uint16 file_count, uint32 type, 26 reserved bytes
    per file:
        uint32 name_len, uint32 flags, 2 reserved, uint32 data_len, 8 reserved,
        uint16 is_unicode, 24 bytes (3 FILETIMEs)                       -> 48 bytes
        name (UTF-16LE, name_len bytes)
        data (data_len bytes)

Data encoding:
    key[i]  = name_ascii[i] ^ MAGIC[i % 4]
    plain   = ~(key[k] ^ nibble_swap(byte)) & 0xFF, with k = (offset % 1024) % len(key)
              (the key position restarts at every 1024-byte block)
    if flags & 4 the decoded bytes are a zlib stream.

Usage:
    python -I tools/isextract.py <installer.exe> --list
    python -I tools/isextract.py <installer.exe> <output_dir>
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

SIGNATURE = b"ISSetupStream\x00"
MAGIC = bytes([0x13, 0x35, 0x86, 0x07])
BLOCK = 1024
CHUNK = BLOCK * 1024  # 1 MiB, a multiple of BLOCK so every chunk starts on a block boundary

FLAG_ENCODED = 0x2
FLAG_COMPRESSED = 0x4

# out = ~(k ^ swap(b)) = swap(b) ^ (~k): nibble-swap via translate, then XOR with a key mask.
_SWAP = bytes(((b << 4) | (b >> 4)) & 0xFF for b in range(256))


@dataclass
class StreamEntry:
    name: str
    flags: int
    size: int
    offset: int


def find_stream(path: Path) -> int:
    """Return the file offset of the ISSetupStream, i.e. the PE overlay start.

    The launcher's own code also contains the signature string, so the stream is located from
    the PE section table (end of the last section's raw data) rather than by searching.
    """
    with path.open("rb") as f:
        head = f.read(4096)
        if head[:2] != b"MZ":
            raise ValueError(f"{path}: not a PE file")
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if head[pe:pe + 4] != b"PE\x00\x00":
            raise ValueError(f"{path}: bad PE signature")
        n_sections, = struct.unpack_from("<H", head, pe + 6)
        opt_size, = struct.unpack_from("<H", head, pe + 20)
        table = pe + 24 + opt_size
        overlay = 0
        for i in range(n_sections):
            raw_size, raw_ptr = struct.unpack_from("<II", head, table + i * 40 + 16)
            overlay = max(overlay, raw_ptr + raw_size)
        f.seek(overlay)
        if f.read(len(SIGNATURE)) != SIGNATURE:
            raise ValueError(f"{path}: no ISSetupStream at overlay offset {overlay}")
    return overlay


def read_entries(path: Path) -> tuple[int, list[StreamEntry]]:
    start = find_stream(path)
    entries: list[StreamEntry] = []
    with path.open("rb") as f:
        f.seek(start + len(SIGNATURE))
        count, stream_type = struct.unpack("<HI", f.read(6))
        if stream_type != 4:
            raise ValueError(f"unsupported ISSetupStream type {stream_type}")
        f.seek(start + 46)
        for _ in range(count):
            name_len, flags, _r1, size, _r2, _uni, _times = struct.unpack("<II2sI8sH24s", f.read(48))
            name = f.read(name_len).decode("utf-16-le")
            entries.append(StreamEntry(name, flags, size, f.tell()))
            f.seek(size, 1)
    return stream_type, entries


def _block_mask(name: str) -> bytes:
    seed = name.encode("ascii")
    key = bytes(seed[i] ^ MAGIC[i % 4] for i in range(len(seed)))
    return bytes((~key[i % len(key)]) & 0xFF for i in range(BLOCK))


def _decode(chunk: bytes, mask: bytes) -> bytes:
    swapped = chunk.translate(_SWAP)
    m = mask[: len(swapped)]
    return (int.from_bytes(swapped, "little") ^ int.from_bytes(m, "little")).to_bytes(len(swapped), "little")


def extract_entry(path: Path, entry: StreamEntry, out_path: Path) -> int:
    """Decode (and inflate) one entry to out_path. Returns the number of bytes written."""
    mask = _block_mask(entry.name) * (CHUNK // BLOCK)
    inflater = zlib.decompressobj() if entry.flags & FLAG_COMPRESSED else None
    written = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as src, out_path.open("wb") as dst:
        src.seek(entry.offset)
        remaining = entry.size
        while remaining:
            chunk = src.read(min(CHUNK, remaining))
            if not chunk:
                raise EOFError(f"{entry.name}: stream truncated")
            remaining -= len(chunk)
            if entry.flags & FLAG_ENCODED:
                chunk = _decode(chunk, mask)
            if inflater is not None:
                chunk = inflater.decompress(chunk)
            dst.write(chunk)
            written += len(chunk)
        if inflater is not None:
            tail = inflater.flush()
            dst.write(tail)
            written += len(tail)
            if not inflater.eof:
                raise ValueError(f"{entry.name}: zlib stream did not terminate")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("installer", type=Path)
    ap.add_argument("output_dir", type=Path, nargs="?")
    ap.add_argument("--list", action="store_true", help="list the embedded files and exit")
    args = ap.parse_args(argv)

    stream_type, entries = read_entries(args.installer)
    if args.list or args.output_dir is None:
        print(f"{args.installer.name}: type {stream_type}, {len(entries)} files")
        for e in entries:
            print(f"  {e.name:<62} {e.size:>14,} bytes  flags=0x{e.flags:x}")
        return 0

    for e in entries:
        target = args.output_dir / e.name
        n = extract_entry(args.installer, e, target)
        print(f"  {e.name:<62} {e.size:>14,} -> {n:>14,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
