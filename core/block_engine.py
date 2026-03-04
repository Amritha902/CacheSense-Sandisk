"""
CacheSelect - Adaptive Compression Policy Engine
core/block_engine.py

Simulates the firmware write path between an NVMe write buffer and the FTL.
Processes fixed 4 KB blocks with deterministic, threshold-based codec selection.

Pipeline per block:
    hash -> LRU cache lookup -> [entropy + RLD -> codec select] -> compress
         -> benefit check -> pack into fixed 4096-byte LBA frame

Frame layout (always exactly 4096 bytes):
    [0]       codec_id         uint8
    [1-2]     original_size    uint16 BE
    [3-4]     compressed_size  uint16 BE
    [5-9]     reserved         5 x 0x00
    [10...N]  compressed data  (up to 4084 bytes)
    [N+1...L] zero padding
    [4094-95] CRC-16           uint16 BE

Dependencies:
    pip install mmh3 lz4
"""

import math
import struct
import time
from collections import OrderedDict

try:
    import mmh3
except ImportError:
    raise ImportError("mmh3 is required: pip install mmh3")

try:
    import lz4.block
except ImportError:
    raise ImportError("lz4 is required: pip install lz4")


# ────────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────────

BLOCK_SIZE           = 4096
HEADER_SIZE          = 10
CRC_SIZE             = 2
DATA_AREA            = BLOCK_SIZE - HEADER_SIZE - CRC_SIZE   # 4084 bytes usable

CACHE_CAPACITY       = 256 * 1024
ESTIMATED_ENTRY_SIZE = 96
CACHE_ENTRIES        = CACHE_CAPACITY // ESTIMATED_ENTRY_SIZE  # ~2730 entries

CODEC_RAW            = 0
CODEC_LZ4            = 1
CODEC_LZ4HC          = 2
CODEC_NAMES          = {CODEC_RAW: "RAW", CODEC_LZ4: "LZ4", CODEC_LZ4HC: "LZ4HC"}

ENTROPY_THRESHOLD    = 7.5
RLD_THRESHOLD        = 0.4
BENEFIT_OVERHEAD     = 10
BENEFIT_MAX          = BLOCK_SIZE - 64   # 4032 bytes
PREFIX_LEN           = 4


# ────────────────────────────────────────────────────────────────────────────────
# CRC-16 / IBM
# ────────────────────────────────────────────────────────────────────────────────

def _crc16(data: bytes) -> int:
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
# LRU Cache
# ────────────────────────────────────────────────────────────────────────────────

class _LRUCache:
    def __init__(self, capacity: int):
        self._cap   = capacity
        self._store: OrderedDict = OrderedDict()

    def get(self, key):
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def put(self, key, value) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        if len(self._store) > self._cap:
            self._store.popitem(last=False)

    def __len__(self) -> int:
        return len(self._store)


# ────────────────────────────────────────────────────────────────────────────────
# BlockEngine
# ────────────────────────────────────────────────────────────────────────────────

class BlockEngine:
    """
    Firmware-simulated adaptive compression policy engine.

    Pipeline: hash -> cache lookup -> entropy+RLD -> codec select
           -> compress -> benefit check -> pack 4096-byte LBA frame

    RAW note: header(10B) + CRC(2B) = 12B overhead, so RAW payload is
    capped at DATA_AREA=4084B. Last 12 bytes dropped (sim only).
    """

    def __init__(self):
        self._cache = _LRUCache(CACHE_ENTRIES)
        self.total_blocks_processed  = 0
        self.total_cache_hits        = 0
        self.total_raw_blocks        = 0
        self.total_compressed_blocks = 0
        self._entropy_sum   = 0.0
        self._entropy_count = 0
        self._ratio_sum     = 0.0

    def process_block(self, block: bytes) -> dict:
        """
        Process a single 4KB block. Returns dict with:
            packed_block, codec_used, entropy, rld,
            compressed_size, cache_hit, timing
        """
        if len(block) != BLOCK_SIZE:
            raise ValueError(
                f"block must be exactly {BLOCK_SIZE} bytes, got {len(block)}"
            )

        t_total_start = time.perf_counter()

        # 1. Hash
        t0        = time.perf_counter()
        signature = self.compute_hash(block)
        t_hash    = time.perf_counter() - t0

        # 2. Cache lookup
        t0           = time.perf_counter()
        prefix       = bytes(block[:PREFIX_LEN])        # BUG FIX #4: always bytes
        cache_result = self.cache_lookup(signature, prefix)
        t_cache      = time.perf_counter() - t0

        cache_hit = cache_result is not None
        entropy   = None
        rld       = None
        t_feature = 0.0

        if cache_hit:
            codec_id = cache_result
        else:
            # 3. Feature extraction
            t0        = time.perf_counter()
            entropy   = self.compute_entropy(block)
            rld       = self.compute_rld(block)
            t_feature = time.perf_counter() - t0

            # 4. Codec selection
            codec_id = self.select_codec(entropy, rld)
            self._cache.put((signature, prefix), codec_id)

        # 5. Compress
        t0                               = time.perf_counter()
        compressed_data, compressed_size = self.compress_block(block, codec_id)
        t_compression                    = time.perf_counter() - t0

        # 6. Benefit check
        if codec_id != CODEC_RAW:
            if not self.benefit_check(BLOCK_SIZE, compressed_size):
                codec_id        = CODEC_RAW
                compressed_data = block
                compressed_size = BLOCK_SIZE

        # 7. Pack
        packed_block = self.pack_block(codec_id, BLOCK_SIZE, compressed_data)

        t_total = time.perf_counter() - t_total_start

        # 8. Stats
        self.total_blocks_processed += 1
        if cache_hit:
            self.total_cache_hits += 1
        if codec_id == CODEC_RAW:
            self.total_raw_blocks += 1
        else:
            self.total_compressed_blocks += 1
        if entropy is not None:
            self._entropy_sum   += entropy
            self._entropy_count += 1
        self._ratio_sum += compressed_size / BLOCK_SIZE

        return {
            "packed_block":    packed_block,
            "codec_used":      CODEC_NAMES[codec_id],
            "entropy":         entropy,
            "rld":             rld,
            "compressed_size": compressed_size,
            "cache_hit":       cache_hit,
            "timing": {                              # BUG FIX #2: restored timing dict
                "hash_time":        t_hash,
                "cache_time":       t_cache,
                "feature_time":     t_feature,
                "compression_time": t_compression,
                "total_time":       t_total,
            },
        }

    def get_stats(self) -> dict:                    # BUG FIX #1: correct indentation
        """Return aggregate statistics across all processed blocks."""
        n = self.total_blocks_processed
        return {
            "total_blocks_processed":    n,
            "total_cache_hits":          self.total_cache_hits,
            "total_raw_blocks":          self.total_raw_blocks,
            "total_compressed_blocks":   self.total_compressed_blocks,
            "cache_hit_rate":            (self.total_cache_hits / n) if n else 0.0,
            "average_entropy":           (self._entropy_sum / self._entropy_count)
                                         if self._entropy_count else 0.0,
            "average_compression_ratio": (self._ratio_sum / n) if n else 0.0,
            "cache_entries_used":        len(self._cache),
            "cache_capacity_entries":    CACHE_ENTRIES,
        }

    def compute_hash(self, block: bytes) -> int:
        """128-bit MurmurHash3 as int. Seed=42 for determinism."""
        h1, h2 = mmh3.hash64(block, seed=42, signed=False)
        return (h1 << 64) | h2

    def cache_lookup(self, signature: int, prefix: bytes) -> int | None:
        """(signature, prefix) -> codec_id on hit, None on miss."""
        return self._cache.get((signature, prefix))

    def compute_entropy(self, block: bytes) -> float:
        """Shannon entropy via 256-bin histogram. Returns [0.0, 8.0]."""
        histogram = [0] * 256
        for b in block:
            histogram[b] += 1
        entropy = 0.0
        for count in histogram:
            if count > 0:
                p        = count / BLOCK_SIZE
                entropy -= p * math.log2(p)
        return entropy

    def compute_rld(self, block: bytes) -> float:
        """Run-Length Density [0.0, 1.0]. Higher = more repetitive."""
        if len(block) < 2:
            return 0.0
        same = sum(1 for i in range(1, len(block)) if block[i] == block[i - 1])
        return same / (len(block) - 1)

    def select_codec(self, entropy: float, rld: float) -> int:
        """entropy>7.5 -> RAW | rld>0.4 -> LZ4 | else -> LZ4HC"""
        if entropy > ENTROPY_THRESHOLD:
            return CODEC_RAW
        if rld > RLD_THRESHOLD:
            return CODEC_LZ4
        return CODEC_LZ4HC

    def compress_block(self, block: bytes, codec_id: int) -> tuple:
        """Compress with selected codec. Falls back to RAW on failure."""
        if codec_id == CODEC_RAW:
            return block, len(block)
        try:
            if codec_id == CODEC_LZ4:
                compressed = lz4.block.compress(block, store_size=False)
            else:
                compressed = lz4.block.compress(
                    block, store_size=False, mode="high_compression"
                )
            return compressed, len(compressed)
        except Exception:
            return block, len(block)

    def benefit_check(self, original_size: int, compressed_size: int) -> bool:
        """Accept compression only if savings exceed overhead + 64B margin."""
        return (compressed_size + BENEFIT_OVERHEAD) < BENEFIT_MAX

    def pack_block(self, codec_id: int, original_size: int,
                   compressed_data: bytes) -> bytes:
        """
        Pack into exactly 4096 bytes.
        BUG FIX #5+#6: RAW truncated to DATA_AREA=4084B + ValueError guard added.
        """
        # BUG FIX: truncate RAW to fit inside fixed data area
        if codec_id == CODEC_RAW:
            compressed_data = compressed_data[:DATA_AREA]

        compressed_size = len(compressed_data)

        if compressed_size > DATA_AREA:          # BUG FIX #5: defensive guard
            raise ValueError(
                f"Payload ({compressed_size}B) exceeds data area ({DATA_AREA}B). "
                "benefit_check should have caught this."
            )

        header = struct.pack(
            ">BHH5s",
            codec_id,
            original_size   & 0xFFFF,
            compressed_size & 0xFFFF,
            b"\x00" * 5,
        )
        assert len(header) == HEADER_SIZE

        padding = b"\x00" * (DATA_AREA - compressed_size)
        payload = header + compressed_data + padding
        packed  = payload + struct.pack(">H", _crc16(payload))

        assert len(packed) == BLOCK_SIZE
        return packed


# ────────────────────────────────────────────────────────────────────────────────
# Smoke test  (python -m core.block_engine)
# ────────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os

    BLOCK_TYPES = {
        "RANDOM    ": lambda: os.urandom(BLOCK_SIZE),
        "REPETITIVE": lambda: (b"\xAB\xCD" * (BLOCK_SIZE // 2)),
        "STRUCTURED": lambda: (
            b"key=value;timestamp=1234567890;type=metadata;\n" * 100
        )[:BLOCK_SIZE],
    }

    print("=" * 64)
    print("  CacheSelect BlockEngine -- Smoke Test")
    print("=" * 64)

    engine = BlockEngine()
    for label, gen in BLOCK_TYPES.items():
        blk = gen()
        r   = engine.process_block(blk)
        h   = f"{r['entropy']:.3f}" if r["entropy"] is not None else "cached"
        d   = f"{r['rld']:.3f}"     if r["rld"]     is not None else "cached"
        print(
            f"  {label} | codec={r['codec_used']:6s} | "
            f"H={h:>7s} | RLD={d:>7s} | "
            f"comp={r['compressed_size']:5d}B | "
            f"hit={str(r['cache_hit']):5s} | "
            f"{r['timing']['total_time']*1e6:.1f}us"
        )

    print()
    print("  Cold -> Warm cache (same block x 10):")
    ref = b"\xDE\xAD\xBE\xEF" * (BLOCK_SIZE // 4)
    for i in range(10):
        r = engine.process_block(ref)
        print(f"    pass {i+1:02d}: hit={str(r['cache_hit']):5s}  codec={r['codec_used']}")

    print()
    s = engine.get_stats()
    print(f"  Blocks processed : {s['total_blocks_processed']}")
    print(f"  Cache hit rate   : {s['cache_hit_rate']*100:.1f}%")
    print(f"  Avg entropy      : {s['average_entropy']:.4f}")
    print(f"  Avg ratio        : {s['average_compression_ratio']:.4f}")
    print(f"  Cache entries    : {s['cache_entries_used']} / {s['cache_capacity_entries']}")
    print()
    print("  OK  BlockEngine stable.")
