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
        -> blocks.bin -> unpack frame -> CRC validate -> decompress -> return data

Virtual file:
    cat mountpoint/.stats    <- live filesystem metrics

Mount:
    python fuse/cachefs.py mountpoint/

Dependencies:
    pip install fusepy lz4 mmh3
"""

import os
import json
import errno
import struct
import time
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

BLOCK_SIZE      = 4096
STORAGE_DIR     = "storage"
BLOCKS_FILE     = os.path.join(STORAGE_DIR, "blocks.bin")
METADATA_FILE   = os.path.join(STORAGE_DIR, "metadata.json")

CODEC_RAW       = 0
CODEC_LZ4       = 1
CODEC_LZ4HC     = 2

HDR_CODEC       = 0        # uint8   byte 0
HDR_ORIG_SIZE   = 1        # uint16 BE bytes 1-2
HDR_COMP_SIZE   = 3        # uint16 BE bytes 3-4
HDR_DATA_START  = 10       # payload starts at byte 10
FRAME_CRC_SIZE  = 2        # CRC16 in last 2 bytes
DATA_AREA       = BLOCK_SIZE - HDR_DATA_START - FRAME_CRC_SIZE  # 4084 bytes

VIRTUAL_STATS   = "/.stats"


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
    """Load metadata.json — returns empty dict if missing or corrupt."""
    if not os.path.exists(METADATA_FILE):
        return {}
    try:
        with open(METADATA_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_metadata(meta: dict) -> None:
    """Atomically persist metadata via a temp-file rename."""
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

    On read, frames are CRC-validated, unpacked, and decompressed transparently.

    Virtual file  /.stats  exposes live instrumentation without a network server.
    All I/O is guarded by a single threading.Lock (FUSE is multi-threaded).
    """

    def __init__(self):
        # ── Storage bootstrap ────────────────────────────────────────────────
        os.makedirs(STORAGE_DIR, exist_ok=True)
        if not os.path.exists(BLOCKS_FILE):
            open(BLOCKS_FILE, "wb").close()
        if not os.path.exists(METADATA_FILE):
            _save_metadata({})

        # ── Engine + lock ────────────────────────────────────────────────────
        self.engine = BlockEngine()
        self.lock   = threading.Lock()

        # ── Instrumentation counters ─────────────────────────────────────────
        self.stats = {
            "total_write_ops":              0,
            "total_read_ops":               0,
            "total_logical_bytes_written":  0,
            "total_physical_bytes_written": 0,
            "total_blocks_written":         0,
            "total_raw_blocks":             0,
            "total_compressed_blocks":      0,
            "total_write_time":             0.0,
            "total_read_time":              0.0,
        }

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def destroy(self, path):
        """Called on unmount — nothing to flush, blocks.bin is always sync'd."""
        pass

    # ── Stats formatter (virtual file content) ───────────────────────────────

    def _format_stats(self) -> str:
        """
        Render a human-readable stats snapshot.
        Called on every read of /.stats so values are always current.
        """
        s        = self.stats
        logical  = s["total_logical_bytes_written"]
        physical = s["total_physical_bytes_written"]

        compression_ratio = (logical / physical) if physical > 0 else 0.0

        avg_write_ms = (
            (s["total_write_time"] / s["total_write_ops"]) * 1000
            if s["total_write_ops"] > 0 else 0.0
        )
        avg_read_ms = (
            (s["total_read_time"] / s["total_read_ops"]) * 1000
            if s["total_read_ops"] > 0 else 0.0
        )

        raw_pct  = (
            s["total_raw_blocks"] / s["total_blocks_written"] * 100
            if s["total_blocks_written"] > 0 else 0.0
        )
        comp_pct = (
            s["total_compressed_blocks"] / s["total_blocks_written"] * 100
            if s["total_blocks_written"] > 0 else 0.0
        )

        engine_stats = self.engine.get_stats()

        return (
            "CacheSelect Filesystem — Live Stats\n"
            "----------------------------------------\n"
            f"Write Ops              : {s['total_write_ops']}\n"
            f"Read Ops               : {s['total_read_ops']}\n"
            "\n"
            f"Logical Bytes Written  : {logical:,}\n"
            f"Physical Bytes Written : {physical:,}\n"
            f"Compression Ratio(L/P) : {compression_ratio:.3f}x\n"
            "\n"
            f"Total Blocks Written   : {s['total_blocks_written']}\n"
            f"  RAW blocks           : {s['total_raw_blocks']}  ({raw_pct:.1f}%)\n"
            f"  Compressed blocks    : {s['total_compressed_blocks']}  ({comp_pct:.1f}%)\n"
            "\n"
            f"Avg Write Latency      : {avg_write_ms:.3f} ms\n"
            f"Avg Read Latency       : {avg_read_ms:.3f} ms\n"
            "\n"
            "-- BlockEngine internals --\n"
            f"Cache Hit Rate         : {engine_stats['cache_hit_rate']*100:.1f}%\n"
            f"Cache Entries Used     : {engine_stats['cache_entries_used']} / "
            f"{engine_stats['cache_capacity_entries']}\n"
            f"Avg Entropy (misses)   : {engine_stats['average_entropy']:.4f} / 8.0\n"
            f"Avg Compression Ratio  : {engine_stats['average_compression_ratio']:.4f}\n"
        )

    # ── Directory / stat operations ──────────────────────────────────────────

    def getattr(self, path, fh=None):
        """
        Return file attributes.
        / -> directory
        /.stats -> virtual read-only file (size computed from current content)
        anything else -> looked up in metadata, ENOENT if missing
        """
        if path == "/":
            return {
                "st_mode":  0o40755,
                "st_nlink": 2,
                "st_size":  0,
                "st_uid":   os.getuid(),
                "st_gid":   os.getgid(),
            }

        # Virtual stats file — size is dynamic (content changes per read)
        if path == VIRTUAL_STATS:
            content = self._format_stats().encode()
            return {
                "st_mode":  0o100444,   # read-only, -r--r--r--
                "st_nlink": 1,
                "st_size":  len(content),
                "st_uid":   os.getuid(),
                "st_gid":   os.getgid(),
            }

        meta = _load_metadata()
        if path not in meta:
            raise FuseOSError(errno.ENOENT)

        return {
            "st_mode":  0o100644,
            "st_nlink": 1,
            "st_size":  meta[path]["size"],
            "st_uid":   os.getuid(),
            "st_gid":   os.getgid(),
        }

    def readdir(self, path, fh):
        """List directory: standard dots + tracked files + virtual .stats."""
        meta  = _load_metadata()
        names = [entry.lstrip("/") for entry in meta.keys()]
        names.append(".stats")          # always expose the virtual stats file
        return [".", ".."] + names

    # ── File creation / open ─────────────────────────────────────────────────

    def create(self, path, mode):
        """Create a new empty file entry in metadata. Returns dummy handle."""
        with self.lock:
            meta = _load_metadata()
            meta[path] = {"size": 0, "blocks": []}
            _save_metadata(meta)
        return 0

    def open(self, path, flags):
        """Validate existence and return dummy file handle."""
        if path == VIRTUAL_STATS:
            return 0
        meta = _load_metadata()
        if path not in meta:
            raise FuseOSError(errno.ENOENT)
        return 0

    def truncate(self, path, length):
        """Update recorded file size (existing blocks are retained)."""
        with self.lock:
            meta = _load_metadata()
            if path not in meta:
                raise FuseOSError(errno.ENOENT)
            meta[path]["size"] = length
            _save_metadata(meta)

    # ── Write path ───────────────────────────────────────────────────────────

    def write(self, path, data: bytes, offset: int, fh) -> int:
        """
        Firmware-simulated write path.

        Each BLOCK_SIZE chunk is:
          1. Zero-padded to exactly 4096 bytes (LBA alignment)
          2. Passed through BlockEngine (entropy -> codec -> compress -> pack)
          3. Appended to blocks.bin as a fixed 4096-byte frame
          4. Registered in metadata[path]["blocks"] as a byte offset

        Instrumentation counters are updated atomically inside the lock.
        """
        start_time = time.perf_counter()                        # INSTRUMENT: start

        with self.lock:
            meta = _load_metadata()
            if path not in meta:
                raise FuseOSError(errno.ENOENT)

            new_offsets = []

            with open(BLOCKS_FILE, "ab") as binfile:
                pos = 0
                while pos < len(data):
                    chunk = data[pos : pos + BLOCK_SIZE]

                    # Zero-pad final partial block to exact LBA size
                    if len(chunk) < BLOCK_SIZE:
                        chunk = chunk + b"\x00" * (BLOCK_SIZE - len(chunk))

                    # ── BlockEngine: analyse + compress + pack ────────────────
                    result = self.engine.process_block(chunk)
                    packed = result["packed_block"]

                    assert len(packed) == BLOCK_SIZE, (
                        f"Frame size mismatch: got {len(packed)}B"
                    )

                    # ── Per-block instrumentation ─────────────────────────────
                    if result["codec_used"] == "RAW":
                        self.stats["total_raw_blocks"]        += 1
                    else:
                        self.stats["total_compressed_blocks"] += 1

                    self.stats["total_blocks_written"]         += 1
                    self.stats["total_physical_bytes_written"] += BLOCK_SIZE

                    # ── Persist frame ─────────────────────────────────────────
                    frame_offset = os.path.getsize(BLOCKS_FILE)
                    binfile.write(packed)
                    new_offsets.append(frame_offset)

                    pos += BLOCK_SIZE

            # Update metadata
            meta[path]["blocks"].extend(new_offsets)
            meta[path]["size"] = offset + len(data)
            _save_metadata(meta)

            # ── Op-level instrumentation ──────────────────────────────────────
            elapsed = time.perf_counter() - start_time          # INSTRUMENT: end
            self.stats["total_write_ops"]             += 1
            self.stats["total_logical_bytes_written"] += len(data)
            self.stats["total_write_time"]            += elapsed

        return len(data)

    # ── Read path ────────────────────────────────────────────────────────────

    def read(self, path: str, size: int, offset: int, fh) -> bytes:
        """
        Transparent read path: CRC validate -> unpack header -> decompress.

        Virtual file /.stats is handled before any block I/O so it never
        touches blocks.bin or metadata.

        Instrumentation is recorded even for .stats reads (op count only).
        """
        # ── Virtual stats file ────────────────────────────────────────────────
        if path == VIRTUAL_STATS:
            data = self._format_stats().encode()
            # Count read op but not latency (no I/O involved)
            self.stats["total_read_ops"] += 1
            return data[offset : offset + size]

        start_time = time.perf_counter()                        # INSTRUMENT: start

        with self.lock:
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

                    # ── Read 4096-byte frame ──────────────────────────────────
                    binfile.seek(blk_offset)
                    packed = binfile.read(BLOCK_SIZE)

                    if len(packed) != BLOCK_SIZE:
                        raise FuseOSError(errno.EIO)

                    # ── CRC-16 integrity check ────────────────────────────────
                    stored_crc = struct.unpack(">H", packed[-2:])[0]
                    calc_crc   = _crc16(packed[:-2])
                    if stored_crc != calc_crc:
                        raise FuseOSError(errno.EIO)

                    # ── Unpack frame header ───────────────────────────────────
                    codec_id  = packed[HDR_CODEC]
                    orig_size = struct.unpack(">H", packed[HDR_ORIG_SIZE : HDR_ORIG_SIZE + 2])[0]
                    comp_size = struct.unpack(">H", packed[HDR_COMP_SIZE : HDR_COMP_SIZE + 2])[0]
                    payload   = packed[HDR_DATA_START : HDR_DATA_START + comp_size]

                    # ── Decompress ────────────────────────────────────────────
                    if codec_id == CODEC_RAW:
                        decompressed = payload

                    elif codec_id in (CODEC_LZ4, CODEC_LZ4HC):
                        try:
                            decompressed = lz4.block.decompress(
                                payload, uncompressed_size=orig_size
                            )
                        except Exception:
                            raise FuseOSError(errno.EIO)

                    else:
                        # Unknown codec — version mismatch or corruption
                        raise FuseOSError(errno.EIO)

                    file_buffer.extend(decompressed)

            # Strip zero-padding from the last block, then slice
            result = bytes(file_buffer)[: file_entry["size"]]

            # ── Op-level instrumentation ──────────────────────────────────────
            elapsed = time.perf_counter() - start_time          # INSTRUMENT: end
            self.stats["total_read_ops"]  += 1
            self.stats["total_read_time"] += elapsed

        return result[offset : offset + size]


# ────────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:   python fuse/cachefs.py <mountpoint>")
        print("Example: mkdir -p mountpoint && python fuse/cachefs.py mountpoint/")
        sys.exit(1)

    mountpoint = sys.argv[1]

    if not os.path.isdir(mountpoint):
        print(f"Error: '{mountpoint}' is not a directory.")
        print(f"  Fix:  mkdir -p {mountpoint}")
        sys.exit(1)

    print(f"  CacheSelectFS  →  {mountpoint}")
    print(f"  Blocks  : {BLOCKS_FILE}")
    print(f"  Meta    : {METADATA_FILE}")
    print(f"  Stats   : cat {mountpoint}/.stats")
    print("  Ctrl+C to unmount.\n")

    FUSE(
        CacheSelectFS(),
        mountpoint,
        foreground=True,
        nothreads=False,
        allow_other=False,
    )
