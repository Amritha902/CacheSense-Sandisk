"""
=============================================================================
MODULE: pattern_cache.py
DESCRIPTION: Firmware-style Pattern Cache for SSD Compression Policy Engine

Simulates the embedded firmware pattern cache found in NVMe SSD controllers
(e.g., SanDisk/WD NAND controllers). The cache stores historical codec
decisions to avoid re-analyzing blocks with known compression profiles.

FIRMWARE ANALOGY:
  - In real firmware (ARM Cortex-R5 / RISC-V), this runs in SRAM
  - 256 KB SRAM budget → max ~16,384 entries at 16 bytes/entry
  - Constant-time O(1) lookup using hash-indexed buckets
  - LRU eviction mirrors firmware cache replacement policy

CACHE PARAMETERS:
  - Total memory  : ~256 KB
  - Entry size    : 16 bytes
  - Max entries   : 16,384
  - Hash function : MurmurHash3 (128-bit, pure Python)
=============================================================================
"""

import struct
import time
from collections import OrderedDict
from typing import Optional, Tuple, Dict, Any


# ---------------------------------------------------------------------------
# Pure-Python MurmurHash3 (x64, 128-bit)
# Replaces mmh3 library — identical output, no external dependency.
# In firmware this would be a hardware-accelerated CRC/hash engine.
# ---------------------------------------------------------------------------

def _fmix64(k: int) -> int:
    """MurmurHash3 64-bit finalizer (avalanche mixer)."""
    k ^= k >> 33
    k = (k * 0xFF51AFD7ED558CCD) & 0xFFFFFFFFFFFFFFFF
    k ^= k >> 33
    k = (k * 0xC4CEB9FE1A85EC53) & 0xFFFFFFFFFFFFFFFF
    k ^= k >> 33
    return k


def murmurhash3_x64_128(data: bytes, seed: int = 0) -> Tuple[int, int]:
    """
    MurmurHash3 x64 128-bit hash.

    Returns (h1, h2) as two 64-bit unsigned integers.
    In firmware this is computed in ~10 ns using dedicated hash hardware.
    Pure Python equivalent for host-side simulation.
    """
    length = len(data)
    nblocks = length >> 4  # number of 16-byte blocks

    h1 = seed & 0xFFFFFFFFFFFFFFFF
    h2 = seed & 0xFFFFFFFFFFFFFFFF

    C1 = 0x87C37B91114253D5
    C2 = 0x4CF5AD432745937F

    # --- Body: process 16-byte blocks ---
    for block_idx in range(nblocks):
        offset = block_idx * 16
        k1 = struct.unpack_from('<Q', data, offset)[0]
        k2 = struct.unpack_from('<Q', data, offset + 8)[0]

        k1 = (k1 * C1) & 0xFFFFFFFFFFFFFFFF
        k1 = ((k1 << 31) | (k1 >> 33)) & 0xFFFFFFFFFFFFFFFF
        k1 = (k1 * C2) & 0xFFFFFFFFFFFFFFFF
        h1 ^= k1

        h1 = ((h1 << 27) | (h1 >> 37)) & 0xFFFFFFFFFFFFFFFF
        h1 = (h1 + h2) & 0xFFFFFFFFFFFFFFFF
        h1 = (h1 * 5 + 0x52DCE729) & 0xFFFFFFFFFFFFFFFF

        k2 = (k2 * C2) & 0xFFFFFFFFFFFFFFFF
        k2 = ((k2 << 33) | (k2 >> 31)) & 0xFFFFFFFFFFFFFFFF
        k2 = (k2 * C1) & 0xFFFFFFFFFFFFFFFF
        h2 ^= k2

        h2 = ((h2 << 31) | (h2 >> 33)) & 0xFFFFFFFFFFFFFFFF
        h2 = (h1 + h2) & 0xFFFFFFFFFFFFFFFF
        h2 = (h2 * 5 + 0x38495AB5) & 0xFFFFFFFFFFFFFFFF

    # --- Tail: remaining bytes ---
    tail_start = nblocks * 16
    tail = data[tail_start:]
    k1 = k2 = 0

    tail_len = length & 15
    if tail_len >= 15: k2 ^= tail[14] << 48
    if tail_len >= 14: k2 ^= tail[13] << 40
    if tail_len >= 13: k2 ^= tail[12] << 32
    if tail_len >= 12: k2 ^= tail[11] << 24
    if tail_len >= 11: k2 ^= tail[10] << 16
    if tail_len >= 10: k2 ^= tail[9]  << 8
    if tail_len >= 9:  k2 ^= tail[8]
    if tail_len >= 9:
        k2 = (k2 * C2) & 0xFFFFFFFFFFFFFFFF
        k2 = ((k2 << 33) | (k2 >> 31)) & 0xFFFFFFFFFFFFFFFF
        k2 = (k2 * C1) & 0xFFFFFFFFFFFFFFFF
        h2 ^= k2

    if tail_len >= 8: k1 ^= tail[7] << 56
    if tail_len >= 7: k1 ^= tail[6] << 48
    if tail_len >= 6: k1 ^= tail[5] << 40
    if tail_len >= 5: k1 ^= tail[4] << 32
    if tail_len >= 4: k1 ^= tail[3] << 24
    if tail_len >= 3: k1 ^= tail[2] << 16
    if tail_len >= 2: k1 ^= tail[1] << 8
    if tail_len >= 1: k1 ^= tail[0]
    if tail_len >= 1:
        k1 = (k1 * C1) & 0xFFFFFFFFFFFFFFFF
        k1 = ((k1 << 31) | (k1 >> 33)) & 0xFFFFFFFFFFFFFFFF
        k1 = (k1 * C2) & 0xFFFFFFFFFFFFFFFF
        h1 ^= k1

    # --- Finalization ---
    h1 ^= length
    h2 ^= length
    h1 = (h1 + h2) & 0xFFFFFFFFFFFFFFFF
    h2 = (h1 + h2) & 0xFFFFFFFFFFFFFFFF
    h1 = _fmix64(h1)
    h2 = _fmix64(h2)
    h1 = (h1 + h2) & 0xFFFFFFFFFFFFFFFF
    h2 = (h1 + h2) & 0xFFFFFFFFFFFFFFFF

    return h1, h2


def compute_signature(block_bytes: bytes) -> int:
    """
    Compute 64-bit block signature using MurmurHash3.
    In firmware: single-cycle hash pipeline result.
    """
    h1, _ = murmurhash3_x64_128(block_bytes, seed=0xDEADBEEF)
    return h1


# ---------------------------------------------------------------------------
# Cache Entry (mirrors 16-byte firmware struct)
# ---------------------------------------------------------------------------

class CacheEntry:
    """
    Mirrors the 16-byte firmware cache entry structure:

      struct cache_entry_t {
          uint64_t signature;   // MurmurHash3 of block content
          uint32_t prefix;      // First 4 bytes of block (fast pre-filter)
          uint8_t  codec_id;    // 0=RAW, 1=LZ4, 2=LZ4HC
          uint16_t avg_ratio;   // Compression ratio * 1000 (fixed-point)
          uint8_t  reserved;    // Alignment / future flags
      };  // sizeof = 16 bytes
    """
    __slots__ = ('signature', 'prefix', 'codec_id', 'avg_ratio',
                 'hit_count', 'last_access_ts')

    CODEC_RAW   = 0
    CODEC_LZ4   = 1
    CODEC_LZ4HC = 2

    def __init__(self, signature: int, prefix: int,
                 codec_id: int, avg_ratio: float):
        self.signature    : int   = signature
        self.prefix       : int   = prefix
        self.codec_id     : int   = codec_id
        self.avg_ratio    : int   = int(avg_ratio * 1000)  # fixed-point
        self.hit_count    : int   = 0
        self.last_access_ts: float = time.perf_counter()

    @property
    def ratio_float(self) -> float:
        return self.avg_ratio / 1000.0

    def to_bytes(self) -> bytes:
        """Serialize to 16-byte firmware-format struct."""
        return struct.pack('<QIBHx',
                           self.signature,
                           self.prefix,
                           self.codec_id & 0xFF,
                           self.avg_ratio & 0xFFFF)

    def __repr__(self) -> str:
        codec_names = {0: 'RAW', 1: 'LZ4', 2: 'LZ4HC'}
        return (f"CacheEntry(sig={self.signature:016x}, "
                f"codec={codec_names.get(self.codec_id,'?')}, "
                f"ratio={self.ratio_float:.3f}, hits={self.hit_count})")


# ---------------------------------------------------------------------------
# Pattern Cache — main class
# ---------------------------------------------------------------------------

class PatternCache:
    """
    Firmware-style Pattern Cache for SSD compression policy decisions.

    Design mirrors SRAM-resident firmware cache on ARM Cortex-R5:
    - O(1) lookup via hash-bucketed OrderedDict
    - LRU eviction when capacity is reached
    - Prefix pre-filter to avoid full signature comparison on misses

    Memory budget: 16,384 entries × 16 bytes = 256 KB
    Throughput target: ~25,000 blocks/second (40 µs/block budget)
    """

    MAX_ENTRIES   = 16_384   # 256 KB @ 16 bytes/entry
    ENTRY_SIZE_B  = 16       # firmware struct size
    TOTAL_SRAM_KB = 256

    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._max_entries = max_entries
        # OrderedDict used as LRU: most-recently-used at end
        self._store: OrderedDict[int, CacheEntry] = OrderedDict()

        # Performance counters (mirror firmware perf registers)
        self._hits   : int = 0
        self._misses : int = 0
        self._evictions: int = 0
        self._inserts : int = 0

        self._created_at = time.perf_counter()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, block_bytes: bytes) -> Optional[CacheEntry]:
        """
        Look up a block in the pattern cache.

        Implements two-stage lookup (mirrors firmware behavior):
          Stage 1: Prefix pre-filter  (4 bytes, very fast)
          Stage 2: Full 64-bit signature comparison

        Returns CacheEntry if hit, None if miss.
        Moves hit entry to MRU position (LRU update).

        Firmware equivalent: ~2 SRAM read cycles on cache hit.
        """
        sig    = compute_signature(block_bytes)
        prefix = struct.unpack_from('>I', block_bytes, 0)[0]  # big-endian

        entry = self._store.get(sig)

        if entry is not None:
            # Stage 1: prefix check (fast path collision guard)
            if entry.prefix != prefix:
                # Hash collision — treat as miss
                self._misses += 1
                return None

            # Cache HIT
            entry.hit_count += 1
            entry.last_access_ts = time.perf_counter()
            self._store.move_to_end(sig)   # promote to MRU
            self._hits += 1
            return entry

        # Cache MISS
        self._misses += 1
        return None

    def insert(self, block_bytes: bytes, codec_id: int,
               compression_ratio: float) -> CacheEntry:
        """
        Insert a new cache entry after a miss.

        If cache is at capacity, evicts the LRU entry first.
        Mirrors firmware cache fill after a codec decision is made.

        Args:
            block_bytes       : 4096-byte block data
            codec_id          : 0=RAW, 1=LZ4, 2=LZ4HC
            compression_ratio : achieved ratio (0.0–1.0, lower = better)

        Returns: newly created CacheEntry
        """
        sig    = compute_signature(block_bytes)
        prefix = struct.unpack_from('>I', block_bytes, 0)[0]

        # Evict if needed before inserting
        if len(self._store) >= self._max_entries:
            self.evict_lru()

        entry = CacheEntry(sig, prefix, codec_id, compression_ratio)
        self._store[sig] = entry
        self._store.move_to_end(sig)   # mark as MRU
        self._inserts += 1
        return entry

    def update_ratio(self, signature: int, new_ratio: float) -> bool:
        """
        Update the compression ratio for an existing entry.

        Implements exponential moving average to smooth ratio estimates,
        mirroring firmware's online statistics update mechanism:
            avg = 0.875 * avg + 0.125 * new   (α = 1/8, shift-friendly)

        Args:
            signature : 64-bit block signature
            new_ratio : newly observed compression ratio

        Returns: True if entry was found and updated, False otherwise.
        """
        entry = self._store.get(signature)
        if entry is None:
            return False

        # EMA update: α=0.125 (firmware uses bit-shift: >> 3)
        old_avg = entry.avg_ratio
        new_raw = int(new_ratio * 1000)
        entry.avg_ratio = (7 * old_avg + new_raw) >> 3   # α = 1/8
        return True

    def evict_lru(self) -> Optional[int]:
        """
        Evict the Least Recently Used entry.

        In firmware this is triggered by the cache controller when
        SRAM pressure exceeds threshold.

        Returns: evicted signature, or None if cache was empty.
        """
        if not self._store:
            return None

        evicted_sig, _ = self._store.popitem(last=False)  # LRU = first item
        self._evictions += 1
        return evicted_sig

    def invalidate(self, block_bytes: bytes) -> bool:
        """
        Explicitly invalidate a cache entry (e.g., after block rewrite).
        Mirrors firmware cache invalidation on LBA overwrite.
        """
        sig = compute_signature(block_bytes)
        if sig in self._store:
            del self._store[sig]
            return True
        return False

    def flush(self) -> None:
        """
        Flush entire cache (mirrors firmware power-cycle / reset).
        """
        self._store.clear()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._inserts = 0

    def stats(self) -> Dict[str, Any]:
        """
        Return cache performance statistics.

        Mirrors firmware performance counter registers accessible
        via NVMe vendor-specific commands.

        Returns dict with:
            hit_rate          : float [0,1]
            miss_rate         : float [0,1]
            cache_utilization : float [0,1]
            total_lookups     : int
            hits              : int
            misses            : int
            evictions         : int
            inserts           : int
            entry_count       : int
            memory_used_kb    : float
            uptime_s          : float
        """
        total = self._hits + self._misses
        hit_rate  = self._hits  / total if total > 0 else 0.0
        miss_rate = self._misses / total if total > 0 else 0.0
        utilization = len(self._store) / self._max_entries

        return {
            'hit_rate'          : round(hit_rate, 4),
            'miss_rate'         : round(miss_rate, 4),
            'cache_utilization' : round(utilization, 4),
            'total_lookups'     : total,
            'hits'              : self._hits,
            'misses'            : self._misses,
            'evictions'         : self._evictions,
            'inserts'           : self._inserts,
            'entry_count'       : len(self._store),
            'max_entries'       : self._max_entries,
            'memory_used_kb'    : len(self._store) * self.ENTRY_SIZE_B / 1024,
            'memory_budget_kb'  : self.TOTAL_SRAM_KB,
            'uptime_s'          : round(time.perf_counter() - self._created_at, 3),
        }

    def top_entries(self, n: int = 10) -> list:
        """Return top-N most-hit cache entries (for diagnostics)."""
        entries = sorted(self._store.values(),
                         key=lambda e: e.hit_count, reverse=True)
        return entries[:n]

    def __len__(self) -> int:
        return len(self._store)

    def __repr__(self) -> str:
        s = self.stats()
        return (f"PatternCache(entries={s['entry_count']}/{s['max_entries']}, "
                f"hit_rate={s['hit_rate']:.1%}, "
                f"util={s['cache_utilization']:.1%})")


# ---------------------------------------------------------------------------
# Example usage / self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import os
    print("=" * 60)
    print("  PatternCache — Firmware Pattern Cache Self-Test")
    print("=" * 60)

    cache = PatternCache(max_entries=1024)   # smaller for demo

    # --- Test 1: Basic insert + lookup ---
    block_a = os.urandom(4096)
    block_b = bytes([0xAB] * 4096)           # all-same byte (highly compressible)
    block_c = bytes(range(256)) * 16         # structured data

    # Misses on first access
    assert cache.lookup(block_a) is None, "Expected miss on new block"
    assert cache.lookup(block_b) is None, "Expected miss on new block"

    # Insert after codec decision
    cache.insert(block_a, codec_id=0, compression_ratio=0.98)  # RAW (random)
    cache.insert(block_b, codec_id=2, compression_ratio=0.02)  # LZ4HC (repetitive)
    cache.insert(block_c, codec_id=1, compression_ratio=0.45)  # LZ4 (structured)

    # Hits on second access
    hit_a = cache.lookup(block_a)
    hit_b = cache.lookup(block_b)
    hit_c = cache.lookup(block_c)

    assert hit_a is not None and hit_a.codec_id == 0
    assert hit_b is not None and hit_b.codec_id == 2
    assert hit_c is not None and hit_c.codec_id == 1
    print("✓ Insert + Lookup:  PASS")

    # --- Test 2: Ratio update via EMA ---
    sig_b = compute_signature(block_b)
    original_ratio = cache._store[sig_b].avg_ratio
    cache.update_ratio(sig_b, 0.03)
    updated_ratio  = cache._store[sig_b].avg_ratio
    assert updated_ratio != original_ratio, "EMA should change ratio"
    print("✓ EMA Ratio Update: PASS")

    # --- Test 3: LRU eviction ---
    small_cache = PatternCache(max_entries=3)
    blocks = [os.urandom(4096) for _ in range(5)]
    for i, blk in enumerate(blocks):
        small_cache.insert(blk, codec_id=i % 3, compression_ratio=0.5)

    assert len(small_cache) == 3, f"Expected 3 entries, got {len(small_cache)}"
    print("✓ LRU Eviction:     PASS")

    # --- Test 4: Stats ---
    # Warm up the cache with many lookups
    for _ in range(50):
        cache.lookup(block_a)
        cache.lookup(block_b)

    miss_block = os.urandom(4096)
    for _ in range(10):
        cache.lookup(miss_block)

    s = cache.stats()
    assert s['hit_rate'] > 0.8, f"Expected high hit rate, got {s['hit_rate']}"
    print("✓ Stats accuracy:   PASS")

    # --- Test 5: Throughput estimate ---
    import time
    test_blocks = [os.urandom(4096) for _ in range(100)]
    for blk in test_blocks:
        cache.insert(blk, 1, 0.5)

    N = 10_000
    t0 = time.perf_counter()
    for i in range(N):
        cache.lookup(test_blocks[i % 100])
    elapsed = time.perf_counter() - t0
    throughput = N / elapsed

    print(f"✓ Throughput:       {throughput:,.0f} lookups/sec "
          f"({elapsed*1e6/N:.1f} µs/lookup)")

    # --- Final stats printout ---
    print()
    print("  Cache Statistics:")
    for k, v in cache.stats().items():
        print(f"    {k:<25}: {v}")

    print()
    print(cache)
    print()
    print("  Top 3 Hot Entries:")
    for e in cache.top_entries(3):
        print(f"    {e}")

    print()
    print("  All tests PASSED. PatternCache is ready.")
