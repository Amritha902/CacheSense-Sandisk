"""
CacheSelect - FUSE Filesystem
fuse/cachefs.py

Simulates SSD firmware compression behavior as a mountable Linux filesystem.

Architecture:
    User writes to mountpoint/file.txt
        -> Linux VFS -> FUSE -> CacheSelectFS.write()
        -> BlockEngine (entropy analysis + codec selection)
        -> packed 4096-byte LBA frames -> storage/blocks.bin

    User reads from mountpoint/file.txt
        -> Linux VFS -> FUSE -> CacheSelectFS.read()
        -> blocks.bin -> unpack frame -> decompress -> return data

This is a firmware simulation, not a kernel driver.
FUSE lets us intercept VFS calls safely in userspace.

Mount:
    python fuse/cachefs.py mountpoint/

Usage (in another terminal):
    echo "hello world" > mountpoint/test.txt
    cat mountpoint/test.txt

Dependencies:
    pip install fusepy lz4
"""

import os
import json
import errno
import struct
import threading

try:
    import lz4.block
except ImportError:
    raise ImportError("lz4 required: pip install lz4")

try:
    from fuse import FUSE, Operations, FuseOSError
except ImportError:
    raise ImportError("fusepy required: pip install fusepy")

from core.block_engine import BlockEngine


# ────────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────────

BLOCK_SIZE     = 4096
STORAGE_DIR    = "storage"
BLOCKS_FILE    = os.path.join(STORAGE_DIR, "blocks.bin")
METADATA_FILE  = os.path.join(STORAGE_DIR, "metadata.json")

# Codec IDs — must match block_engine.py exactly
CODEC_RAW      = 0
CODEC_LZ4      = 1
CODEC_LZ4HC    = 2

# Frame layout offsets
HDR_CODEC      = 0          # uint8   at byte 0
HDR_ORIG_SIZE  = 1          # uint16 BE at bytes 1-2
HDR_COMP_SIZE  = 3          # uint16 BE at bytes 3-4
HDR_RESERVED   = 5          # 5 zero bytes at 5-9
HDR_DATA_START = 10         # compressed payload starts at byte 10
FRAME_CRC_SIZE = 2          # CRC16 occupies last 2 bytes
DATA_AREA      = BLOCK_SIZE - (HDR_DATA_START + FRAME_CRC_SIZE)  # 4084 bytes


# ────────────────────────────────────────────────────────────────────────────────
# CRC-16 / IBM  (must match block_engine._crc16 exactly)
# ────────────────────────────────────────────────────────────────────────────────

def _crc16(data: bytes) -> int:
    """CRC-16/IBM — polynomial 0xA001 (reflected 0x8005)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


# ────────────────────────────────────────────────────────────────────────────────
# Metadata helpers
# ────────────────────────────────────────────────────────────────────────────────

def _load_metadata() -> dict:
    """Load metadata.json, returning empty dict if missing or corrupt."""
    if not os.path.exists(METADATA_FILE):
        return {}
    try:
        with open(METADATA_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_metadata(meta: dict) -> None:
    """Atomically save metadata to metadata.json."""
    tmp = METADATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, METADATA_FILE)


# ────────────────────────────────────────────────────────────────────────────────
# CacheSelectFS
# ────────────────────────────────────────────────────────────────────────────────

class CacheSelectFS(Operations):
    """
    FUSE filesystem that routes every write through the CacheSelect
    BlockEngine, storing compressed 4096-byte LBA frames in blocks.bin.

    On read, frames are unpacked and decompressed transparently.

    Key design points:
      - One threading.Lock guards all metadata + block I/O (FUSE is multi-threaded)
      - CRC-16 is verified on every frame read (integrity check)
      - Codec selection is fully delegated to BlockEngine (no policy here)
      - Metadata lives in metadata.json; raw frames live in blocks.bin
    """

    def __init__(self):
        # Ensure storage directory exists
        os.makedirs(STORAGE_DIR, exist_ok=True)

        # Create blocks.bin if missing
        if not os.path.exists(BLOCKS_FILE):
            open(BLOCKS_FILE, "wb").close()

        # Create metadata.json if missing
        if not os.path.exists(METADATA_FILE):
            _save_metadata({})

        # Compression policy engine — shared across all file operations
        self.engine = BlockEngine()

        # Single lock for all critical sections (metadata + block I/O)
        self._mutex = threading.Lock()

    # ── FUSE lifecycle ───────────────────────────────────────────────────────

    def destroy(self, path):
        """Called on filesystem unmount. Flush nothing — blocks.bin is always flushed."""
        pass

    # ── Directory / stat operations ──────────────────────────────────────────

    def getattr(self, path, fh=None):
        """
        Return file attributes (stat).
        Root "/" returns a directory entry.
        All other paths are looked up in metadata.
        Missing paths raise ENOENT.
        """
        if path == "/":
            return {
                "st_mode":  0o40755,    # directory, rwxr-xr-x
                "st_nlink": 2,
                "st_size":  0,
                "st_uid":   os.getuid(),
                "st_gid":   os.getgid(),
            }

        # Virtual read-only stats file
        if path == "/.stats":
            content = self._format_stats()
            return {
                "st_mode":  0o100444,
                "st_nlink": 1,
                "st_size":  len(content.encode()),
                "st_uid":   os.getuid(),
                "st_gid":   os.getgid(),
            }

        meta = _load_metadata()
        if path not in meta:
            raise FuseOSError(errno.ENOENT)

        return {
            "st_mode":  0o100644,       # regular file, rw-r--r--
            "st_nlink": 1,
            "st_size":  meta[path]["size"],
            "st_uid":   os.getuid(),
            "st_gid":   os.getgid(),
        }

    def readdir(self, path, fh):
        """
        List directory contents.
        Returns standard "." and ".." plus all tracked filenames.
        """
        meta  = _load_metadata()
        names = [entry.lstrip("/") for entry in meta.keys()]
        return [".", "..", ".stats"] + names

    def _format_stats(self) -> str:
        """
        Render live engine stats as a human-readable virtual file.
        Mirrors SSD SMART attribute readout.
        cat mountpoint/.stats
        """
        s   = self.engine.get_stats()
        n   = max(1, s["total_blocks_processed"])
        sep = "-" * 48

        waf_note = "saving space" if s["waf"] < 1.0 else "WARNING: overhead"

        lines = [
            "CacheSelect — Live Firmware Stats",
            f"{'=' * 48}",
            "",
            "── Write Amplification Factor (WAF) ──────",
            f"  WAF                    : {s['waf']:.4f}   ({waf_note})",
            f"  Space saved            : {s['space_saving_pct']:.2f}%",
            f"  Logical bytes written  : {s['logical_bytes_written']:,}",
            f"  Physical bytes written : {s['physical_bytes_written']:,}",
            "",
            "── Block Stats ───────────────────────────",
            f"  Total blocks processed : {s['total_blocks_processed']}",
            f"  SKIP (zero blocks)     : {s['total_skip_blocks']:>6}  ({s['total_skip_blocks']/n*100:.1f}%)",
            f"  RAW  (incompressible)  : {s['total_raw_blocks']:>6}  ({s['total_raw_blocks']/n*100:.1f}%)",
            f"  Compressed             : {s['total_compressed_blocks']:>6}  ({s['total_compressed_blocks']/n*100:.1f}%)",
            "",
            "── PatternCache ──────────────────────────",
            f"  Cache hit rate         : {s['cache_hit_rate']*100:.1f}%",
            f"  Cache entries          : {s['cache_entries_used']} / {s['cache_capacity_entries']}",
            f"  Adaptive overrides     : {s['adaptive_overrides']}",
            f"    Learned incompress.  : {s['adaptive_skip_compress']}",
            f"    Learned compressible : {s['adaptive_force_compress']}",
            "",
            "── Feature Analysis ──────────────────────",
            f"  Avg entropy (misses)   : {s['average_entropy']:.4f} / 8.0",
            f"  Avg compression ratio  : {s['average_compression_ratio']:.4f}",
            f"  Thermal throttle       : {'ACTIVE - capped at LZ4' if s['thermal_throttle_active'] else 'off'}",
            "",
            "── Telemetry ─────────────────────────────",
            f"  Host writes            : {s['logical_bytes_written']/(1024**2):.4f} MB",
            f"  NAND writes            : {s['physical_bytes_written']/(1024**2):.4f} MB",
            f"  Lifetime savings       : {max(0, s['logical_bytes_written']-s['physical_bytes_written'])/(1024**2):.4f} MB",
            "",
        ]
        return "\n".join(lines)

    # ── File creation / open ─────────────────────────────────────────────────

    def create(self, path, mode):
        """
        Create a new empty file.
        Adds a zeroed metadata entry; no blocks are allocated yet.
        Returns 0 as a dummy file handle (FUSE accepts any int).
        """
        with self._mutex:
            meta = _load_metadata()
            meta[path] = {
                "size":   0,
                "blocks": [],
            }
            _save_metadata(meta)
        return 0

    def open(self, path, flags):
        """
        Open an existing file.
        Validates existence; returns a dummy handle.
        """
        meta = _load_metadata()
        if path not in meta:
            raise FuseOSError(errno.ENOENT)
        return 0

    def truncate(self, path, length):
        """
        Resize a file.
        Updates the size field in metadata.
        Existing blocks are retained (simplified — acceptable for demo).
        """
        with self._mutex:
            meta = _load_metadata()
            if path not in meta:
                raise FuseOSError(errno.ENOENT)
            meta[path]["size"] = length
            _save_metadata(meta)

    # ── Write path ───────────────────────────────────────────────────────────

    def write(self, path, data: bytes, offset: int, fh) -> int:
        """
        Core write path — this is the firmware simulation heart.

        For each 4096-byte chunk of data:
          1. Pass to BlockEngine (entropy analysis, codec selection, compression)
          2. Receive a packed 4096-byte LBA frame
          3. Append frame to blocks.bin
          4. Record the byte offset in metadata

        Partial final blocks are zero-padded to exactly 4096 bytes,
        matching real SSD LBA alignment behaviour.

        Thread safety: entire operation is held under self._mutex.
        """
        with self._mutex:
            meta = _load_metadata()

            if path not in meta:
                raise FuseOSError(errno.ENOENT)

            new_block_offsets = []

            with open(BLOCKS_FILE, "ab") as binfile:
                # Walk data in BLOCK_SIZE chunks
                pos = 0
                while pos < len(data):
                    chunk = data[pos : pos + BLOCK_SIZE]

                    # Zero-pad the final partial block to exactly BLOCK_SIZE bytes
                    if len(chunk) < BLOCK_SIZE:
                        chunk = chunk + b"\x00" * (BLOCK_SIZE - len(chunk))

                    # ── BlockEngine: analyse + compress + pack ────────────────
                    result = self.engine.process_block(chunk)
                    packed = result["packed_block"]

                    assert len(packed) == BLOCK_SIZE, (
                        f"BlockEngine returned frame of {len(packed)}B (expected {BLOCK_SIZE}B)"
                    )

                    # Record byte offset before appending
                    frame_offset = os.path.getsize(BLOCKS_FILE)
                    binfile.write(packed)

                    new_block_offsets.append(frame_offset)
                    pos += BLOCK_SIZE

            # Update metadata: append new block offsets + update size
            meta[path]["blocks"].extend(new_block_offsets)
            meta[path]["size"] = offset + len(data)
            _save_metadata(meta)

        return len(data)

    # ── Read path ────────────────────────────────────────────────────────────

    def read(self, path: str, size: int, offset: int, fh) -> bytes:
        """
        Core read path — transparently unpacks and decompresses LBA frames.

        For each stored block offset:
          1. Read exactly 4096 bytes from blocks.bin
          2. Validate CRC-16  (raises EIO on integrity failure)
          3. Unpack header to get codec_id + compressed_size
          4. Extract payload and decompress (LZ4 / LZ4HC / RAW pass-through)
          5. Assemble full file buffer and slice [offset : offset+size]

        Thread safety: entire operation is held under self._mutex.
        """
        # Virtual .stats file
        if path == "/.stats":
            content = self._format_stats().encode()
            return content[offset:offset + size]

        with self._mutex:
            meta = _load_metadata()

            if path not in meta:
                raise FuseOSError(errno.ENOENT)

            file_entry    = meta[path]
            block_offsets = file_entry["blocks"]

            if not block_offsets:
                return b""

            file_buffer = bytearray()

            with open(BLOCKS_FILE, "rb") as binfile:
                for blk_offset in block_offsets:
                    # ── Read raw 4096-byte frame ──────────────────────────────
                    binfile.seek(blk_offset)
                    packed = binfile.read(BLOCK_SIZE)

                    if len(packed) != BLOCK_SIZE:
                        # Truncated frame — storage corruption
                        raise FuseOSError(errno.EIO)

                    # ── CRC-16 integrity check ────────────────────────────────
                    stored_crc = struct.unpack(">H", packed[-2:])[0]
                    calc_crc   = _crc16(packed[:-2])
                    if stored_crc != calc_crc:
                        raise FuseOSError(errno.EIO)

                    # ── Unpack frame header ───────────────────────────────────
                    codec_id  = packed[HDR_CODEC]
                    orig_size = struct.unpack(">H", packed[HDR_ORIG_SIZE  : HDR_ORIG_SIZE  + 2])[0]
                    comp_size = struct.unpack(">H", packed[HDR_COMP_SIZE  : HDR_COMP_SIZE  + 2])[0]

                    # ── Extract compressed payload ────────────────────────────
                    payload = packed[HDR_DATA_START : HDR_DATA_START + comp_size]

                    # ── Decompress according to codec ─────────────────────────
                    if codec_id == CODEC_RAW:
                        # RAW: payload IS the data (up to DATA_AREA bytes)
                        decompressed = payload

                    elif codec_id in (CODEC_LZ4, CODEC_LZ4HC):
                        try:
                            decompressed = lz4.block.decompress(
                                payload, uncompressed_size=orig_size
                            )
                        except Exception:
                            # Decompression failure — treat as I/O error
                            raise FuseOSError(errno.EIO)

                    elif codec_id == 255:
                        # SKIP frame — zero block, return zeros
                        decompressed = b"\x00" * BLOCK_SIZE

                    else:
                        # Unknown codec — storage is corrupt or version mismatch
                        raise FuseOSError(errno.EIO)

                    file_buffer.extend(decompressed)

            # Trim to the requested window and strip zero padding
            result = bytes(file_buffer)
            actual_size = file_entry["size"]
            result = result[:actual_size]          # strip padding from last block
            return result[offset : offset + size]  # slice to requested range


# ────────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python fuse/cachefs.py <mountpoint>")
        print("Example:")
        print("  mkdir -p mountpoint")
        print("  python fuse/cachefs.py mountpoint/")
        sys.exit(1)

    mountpoint = sys.argv[1]

    if not os.path.isdir(mountpoint):
        print(f"Error: mountpoint '{mountpoint}' does not exist or is not a directory.")
        print(f"  Create it with:  mkdir -p {mountpoint}")
        sys.exit(1)

    print(f"CacheSelectFS mounting at: {mountpoint}")
    print(f"Storage: {BLOCKS_FILE}  |  Metadata: {METADATA_FILE}")
    print("Press Ctrl+C to unmount.\n")

    FUSE(
        CacheSelectFS(),
        mountpoint,
        foreground=True,
        nothreads=False,    # allow multi-threaded FUSE (we use a lock)
        allow_other=False,  # restrict to mounting user only
    )
