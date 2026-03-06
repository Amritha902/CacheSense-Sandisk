"""
CacheSelect — Adaptive Compression Policy Engine  v3
core/block_engine.py

Firmware write-path simulation: NVMe write buffer → policy engine → FTL.

What changed from v2 (uniqueness-aware policy):

  _select_codec():
    Now accepts uniqueness as a third feature signal.
    Decision tree matches policy_selector.py v2:
      - RAW only when entropy > 7.6 AND uniqueness > 0.85
      - LZ4 when rld > 0.05 (lowered from 0.40)
      - LZ4HC for entropy < 6.5 OR uniqueness < 0.30
      - LZ4HC as safe default otherwise
    This directly reduces false-RAW classifications and steers
    partially-repetitive / structured-but-complex blocks to compression.

  _rld():
    Switched from adjacent-pair comparison to run-savings method.
    Now consistent with feature_analyzer.py definition.
    (Old method over-counted transitions; new method under-counts nothing.)

  _uniqueness():
    New O(N) method using bytearray presence mask — avoids Python set overhead.
    Matches compute_uniqueness() in feature_analyzer.py.

  Feature extraction (cache miss path):
    Runs entropy + uniqueness in a single histogram pass.
    RLD runs as a second sequential pass (order-dependent, unavoidable).
    Total analysis cost unchanged (~15 µs / 4KB at 400 MHz equivalent).

  AdaptivePatternCache:
    Unchanged — EMA + confidence still works correctly.
    Cache key still (hash128, prefix4).

  Telemetry:
    Added uniqueness-related counters: total_uniqueness_sum,
    raw_avoided_by_uniqueness (blocks saved from false-RAW by new logic).

Frame layout (unchanged from v2):
    [0]       codec_id        uint8   (255=SKIP)
    [1-2]     original_size   uint16 BE
    [3-4]     compressed_size uint16 BE
    [5-9]     reserved        5 x 0x00
    [10..N]   payload         up to 4084 bytes
    [N+1..L]  zero padding
    [4094-95] CRC-16/IBM      uint16 BE

Dependencies:
    pip install mmh3 lz4
"""

import math, struct, time
from collections import OrderedDict

try:
    import mmh3
except ImportError:
    raise ImportError("mmh3 required: pip install mmh3")

try:
    import lz4.block
except ImportError:
    raise ImportError("lz4 required: pip install lz4")

# ── Constants ─────────────────────────────────────────────────────────────────
BLOCK_SIZE           = 4096
HEADER_SIZE          = 10
CRC_SIZE             = 2
DATA_AREA            = BLOCK_SIZE - HEADER_SIZE - CRC_SIZE  # 4084

CACHE_CAPACITY       = 256 * 1024
ESTIMATED_ENTRY_SIZE = 112
CACHE_ENTRIES        = CACHE_CAPACITY // ESTIMATED_ENTRY_SIZE  # ~2340

CODEC_SKIP           = 255
CODEC_RAW            = 0
CODEC_LZ4            = 1
CODEC_LZ4HC          = 2
CODEC_NAMES          = {255:"SKIP", 0:"RAW", 1:"LZ4", 2:"LZ4HC"}

# v3 policy thresholds — mirror policy_selector.py v2
ENTROPY_INCOMPRESSIBLE  = 7.6    # was 7.5 [CHANGED]
UNIQUENESS_RANDOM       = 0.85   # NEW — second RAW condition
RLD_THRESHOLD           = 0.05   # was 0.40 [CHANGED — catches partial runs]
ENTROPY_LZ4HC_FALLBACK  = 6.5    # NEW — entropy < this → LZ4HC
UNIQUENESS_LOW          = 0.30   # NEW — uniqueness < this → LZ4HC

BENEFIT_OVERHEAD     = 10
BENEFIT_MAX          = BLOCK_SIZE - 64

PREFIX_LEN           = 4
ZERO_PREFIX          = b"\x00" * PREFIX_LEN

CONFIDENCE_MIN       = 3
RATIO_SKIP_COMPRESS  = 0.88
RATIO_FORCE_COMPRESS = 0.35
EMA_ALPHA            = 0.25

THERMAL_LIMIT        = 6.5
THERMAL_WINDOW       = 100

# ── CRC-16 ───────────────────────────────────────────────────────────────────
def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF

# ── Cache entry ───────────────────────────────────────────────────────────────
class _CacheEntry:
    """
    Mirrors C firmware struct (16 bytes packed):
        uint64_t signature;
        uint8_t  prefix[4];
        uint8_t  codec_id;
        uint8_t  confidence;     // saturates at 255
        uint16_t avg_ratio_fp;   // ratio * 1000, fixed-point
    Python uses float for simulation accuracy.
    """
    __slots__ = ("codec_id", "confidence", "avg_ratio")
    def __init__(self, codec_id):
        self.codec_id   = codec_id
        self.confidence = 1
        self.avg_ratio  = 1.0
    def update(self, ratio):
        self.avg_ratio  = EMA_ALPHA * ratio + (1 - EMA_ALPHA) * self.avg_ratio
        self.confidence = min(255, self.confidence + 1)
    @property
    def trusted(self):
        return self.confidence >= CONFIDENCE_MIN

# ── Adaptive LRU cache ────────────────────────────────────────────────────────
class _AdaptivePatternCache:
    """
    LRU cache: (hash128, prefix4) → _CacheEntry
    Each entry learns per-pattern compression ratio via EMA.
    After CONFIDENCE_MIN hits, overrides codec based on learned history.
    """
    def __init__(self, capacity):
        self._cap   = capacity
        self._store = OrderedDict()
    def get(self, key):
        if key not in self._store: return None
        self._store.move_to_end(key)
        return self._store[key]
    def put(self, key, entry):
        if key in self._store: self._store.move_to_end(key)
        self._store[key] = entry
        if len(self._store) > self._cap:
            self._store.popitem(last=False)
    def __len__(self): return len(self._store)

# ── BlockEngine ───────────────────────────────────────────────────────────────
class BlockEngine:
    def __init__(self):
        self._cache = _AdaptivePatternCache(CACHE_ENTRIES)
        self.total_blocks_processed     = 0
        self.total_cache_hits           = 0
        self.total_cache_misses         = 0
        self.total_raw_blocks           = 0
        self.total_compressed_blocks    = 0
        self.total_skip_blocks          = 0
        self.adaptive_overrides         = 0
        self.adaptive_skip_compress     = 0
        self.adaptive_force_compress    = 0
        self.raw_avoided_by_uniqueness  = 0   # [NEW] — blocks rescued from false-RAW
        self.logical_bytes_written      = 0
        self.physical_bytes_written     = 0
        self._entropy_sum               = 0.0
        self._entropy_count             = 0
        self._uniqueness_sum            = 0.0  # [NEW]
        self._ratio_sum                 = 0.0
        self._thermal_throttle          = False
        self._entropy_window            = []

    def process_block(self, block: bytes) -> dict:
        if len(block) != BLOCK_SIZE:
            raise ValueError(f"Expected {BLOCK_SIZE} bytes, got {len(block)}")
        t_total = time.perf_counter()

        # 1. Zero-block detection (TRIM equivalent)
        t0 = time.perf_counter()
        zero_block = (block[:PREFIX_LEN] == ZERO_PREFIX and
                      block == b"\x00" * BLOCK_SIZE)
        t_zero = time.perf_counter() - t0
        if zero_block:
            self.total_skip_blocks      += 1
            self.total_blocks_processed += 1
            self.logical_bytes_written  += BLOCK_SIZE
            self.physical_bytes_written += BLOCK_SIZE
            return {
                "packed_block":    self._pack_skip(),
                "codec_used":      "SKIP",
                "entropy":         0.0, "rld": 1.0, "uniqueness": 0.004,
                "compressed_size": 0,
                "cache_hit":       False, "zero_block": True,
                "waf_current":     self._waf(),
                "timing": {"zero_check_time": t_zero, "hash_time": 0.0,
                           "cache_time": 0.0, "feature_time": 0.0,
                           "compression_time": 0.0,
                           "total_time": time.perf_counter() - t_total},
            }

        # 2. Hash
        t0 = time.perf_counter()
        h1, h2 = mmh3.hash64(block, seed=42, signed=False)
        sig = (h1 << 64) | h2
        pfx = bytes(block[:PREFIX_LEN])
        t_hash = time.perf_counter() - t0

        # 3. Adaptive cache lookup
        t0 = time.perf_counter()
        entry = self._cache.get((sig, pfx))
        t_cache = time.perf_counter() - t0

        cache_hit   = entry is not None
        entropy     = rld = uniqueness = None
        t_feature   = 0.0
        force_raw   = skip_benefit = False

        if cache_hit:
            self.total_cache_hits += 1
            codec_id = entry.codec_id
            if entry.trusted:
                if entry.avg_ratio > RATIO_SKIP_COMPRESS:
                    codec_id = CODEC_RAW; force_raw = True
                    self.adaptive_overrides      += 1
                    self.adaptive_skip_compress  += 1
                elif entry.avg_ratio < RATIO_FORCE_COMPRESS:
                    skip_benefit = True
                    self.adaptive_overrides      += 1
                    self.adaptive_force_compress += 1
        else:
            # 4. Feature extraction (cache miss only)
            self.total_cache_misses += 1
            t0 = time.perf_counter()
            entropy, uniqueness = self._entropy_and_uniqueness(block)  # single histogram pass
            rld                 = self._rld(block)                      # separate sequential pass
            t_feature = time.perf_counter() - t0

            self._update_thermal(entropy)
            codec_id = self._select_codec(entropy, rld, uniqueness)
            entry    = _CacheEntry(codec_id)
            self._cache.put((sig, pfx), entry)

            self._entropy_sum    += entropy
            self._uniqueness_sum += uniqueness   # [NEW]
            self._entropy_count  += 1

        # 5. Compress
        t0 = time.perf_counter()
        if codec_id == CODEC_RAW or force_raw:
            comp_data, comp_size, actual_ratio = block, BLOCK_SIZE, 1.0
        else:
            comp_data, comp_size = self._compress(block, codec_id)
            actual_ratio = comp_size / BLOCK_SIZE
        t_comp = time.perf_counter() - t0

        # 6. Benefit check
        if codec_id != CODEC_RAW and not force_raw and not skip_benefit:
            if (comp_size + BENEFIT_OVERHEAD) >= BENEFIT_MAX:
                codec_id = CODEC_RAW
                comp_data, comp_size, actual_ratio = block, BLOCK_SIZE, 1.0

        # 7. Update cache entry ratio history
        if entry is not None and not force_raw:
            entry.update(actual_ratio)
            entry.codec_id = codec_id

        # 8. Pack
        packed = self._pack(codec_id, BLOCK_SIZE, comp_data)

        # 9. Stats
        self.total_blocks_processed  += 1
        if codec_id == CODEC_RAW: self.total_raw_blocks        += 1
        else:                     self.total_compressed_blocks += 1
        self.logical_bytes_written   += BLOCK_SIZE
        self.physical_bytes_written  += comp_size
        self._ratio_sum              += actual_ratio

        return {
            "packed_block":    packed,
            "codec_used":      CODEC_NAMES.get(codec_id, "RAW"),
            "entropy":         entropy,
            "rld":             rld,
            "uniqueness":      uniqueness,   # [NEW]
            "compressed_size": comp_size,
            "cache_hit":       cache_hit,
            "zero_block":      False,
            "waf_current":     self._waf(),
            "timing": {"zero_check_time": t_zero, "hash_time": t_hash,
                       "cache_time": t_cache, "feature_time": t_feature,
                       "compression_time": t_comp,
                       "total_time": time.perf_counter() - t_total},
        }

    def get_stats(self) -> dict:
        n = max(1, self.total_blocks_processed)
        total = self.total_cache_hits + self.total_cache_misses
        return {
            "total_blocks_processed":    self.total_blocks_processed,
            "total_cache_hits":          self.total_cache_hits,
            "total_cache_misses":        self.total_cache_misses,
            "total_raw_blocks":          self.total_raw_blocks,
            "total_compressed_blocks":   self.total_compressed_blocks,
            "total_skip_blocks":         self.total_skip_blocks,
            "cache_hit_rate":            (self.total_cache_hits / total) if total else 0.0,
            "average_entropy":           self._entropy_sum / self._entropy_count
                                         if self._entropy_count else 0.0,
            "average_uniqueness":        self._uniqueness_sum / self._entropy_count   # [NEW]
                                         if self._entropy_count else 0.0,
            "average_compression_ratio": self._ratio_sum / n,
            "logical_bytes_written":     self.logical_bytes_written,
            "physical_bytes_written":    self.physical_bytes_written,
            "waf":                       self._waf(),
            "space_saving_pct":          self._space_saving(),
            "adaptive_overrides":        self.adaptive_overrides,
            "adaptive_skip_compress":    self.adaptive_skip_compress,
            "adaptive_force_compress":   self.adaptive_force_compress,
            "raw_avoided_by_uniqueness": self.raw_avoided_by_uniqueness,   # [NEW]
            "cache_entries_used":        len(self._cache),
            "cache_capacity_entries":    CACHE_ENTRIES,
            "thermal_throttle_active":   self._thermal_throttle,
        }

    # ── Internal feature methods ──────────────────────────────────────────────

    def _entropy_and_uniqueness(self, block: bytes):
        """
        Compute Shannon entropy AND uniqueness in a single histogram pass.
        [CHANGED from v2 which had two separate methods]

        Returns (entropy, uniqueness) tuple.
        """
        hist = [0] * 256
        for b in block:
            hist[b] += 1

        inv_n     = 1.0 / BLOCK_SIZE
        entropy   = 0.0
        nonzero   = 0
        for count in hist:
            if count > 0:
                p = count * inv_n
                entropy -= p * math.log2(p)
                nonzero += 1

        uniqueness = nonzero / 256.0
        return round(entropy, 6), round(uniqueness, 6)

    def _rld(self, block: bytes) -> float:
        """
        Compute Run-Length Density using run-savings method.
        [CHANGED from v2 — now consistent with feature_analyzer.py]

        Old v2 method counted adjacent-equal pairs / (N-1), which
        conflated single pairs with long runs. New method counts
        true compression savings (run_len - 1 per run ≥ 2).

        Returns RLD in [0.0, 1.0].
        """
        n = BLOCK_SIZE
        if n < 2:
            return 0.0
        total_savings = 0
        cur           = block[0]
        run           = 1
        for i in range(1, n):
            b = block[i]
            if b == cur:
                run += 1
            else:
                if run >= 2:
                    total_savings += run - 1
                cur = b
                run = 1
        if run >= 2:
            total_savings += run - 1
        return round(total_savings / n, 6)

    def _select_codec(self, entropy: float, rld: float,
                      uniqueness: float) -> int:
        """
        Three-signal policy decision tree.
        [CHANGED — now uses uniqueness as third dimension]

        Mirrors policy_selector.py v2 decision tree exactly.
        """
        # Gate 1: Truly incompressible — entropy AND uniqueness both high
        if entropy > ENTROPY_INCOMPRESSIBLE and uniqueness > UNIQUENESS_RANDOM:
            return CODEC_RAW

        # Track blocks that OLD policy would have sent RAW but we now compress
        # (entropy high but uniqueness saved it)
        if entropy > ENTROPY_INCOMPRESSIBLE and uniqueness <= UNIQUENESS_RANDOM:
            self.raw_avoided_by_uniqueness += 1   # [NEW telemetry]

        # Gate 2: Run-length dominated (lowered threshold catches partial runs)
        if rld > RLD_THRESHOLD:
            return CODEC_LZ4

        # Thermal throttle: cap at LZ4 to reduce CPU heat under sustained load
        if self._thermal_throttle:
            return CODEC_LZ4

        # Gate 3: LZ4HC fast-path for clearly structured / low-diversity data
        if entropy < ENTROPY_LZ4HC_FALLBACK or uniqueness < UNIQUENESS_LOW:
            return CODEC_LZ4HC

        # Default: LZ4HC for everything else (moderate structured data)
        return CODEC_LZ4HC

    def _compress(self, block: bytes, codec_id: int):
        try:
            mode = "default" if codec_id == CODEC_LZ4 else "high_compression"
            c = lz4.block.compress(block, store_size=False, mode=mode)
            return c, len(c)
        except Exception:
            return block, len(block)

    def _pack(self, codec_id: int, original_size: int, comp_data: bytes) -> bytes:
        if codec_id == CODEC_RAW:
            comp_data = comp_data[:DATA_AREA]
        cs   = len(comp_data)
        hdr  = struct.pack(">BHH5s", codec_id, original_size & 0xFFFF,
                           cs & 0xFFFF, b"\x00" * 5)
        body = hdr + comp_data + b"\x00" * (DATA_AREA - cs)
        return body + struct.pack(">H", _crc16(body))

    def _pack_skip(self) -> bytes:
        hdr  = struct.pack(">BHH5s", CODEC_SKIP, 0, 0, b"\x00" * 5)
        body = hdr + b"\x00" * DATA_AREA
        return body + struct.pack(">H", _crc16(body))

    def _waf(self) -> float:
        if self.logical_bytes_written == 0:
            return 0.0
        return self.physical_bytes_written / self.logical_bytes_written

    def _space_saving(self) -> float:
        if self.logical_bytes_written == 0:
            return 0.0
        return ((self.logical_bytes_written - self.physical_bytes_written) /
                self.logical_bytes_written * 100)

    def _update_thermal(self, entropy: float):
        self._entropy_window.append(entropy)
        if len(self._entropy_window) > THERMAL_WINDOW:
            self._entropy_window.pop(0)
        if len(self._entropy_window) >= 20:
            avg = sum(self._entropy_window) / len(self._entropy_window)
            self._thermal_throttle = avg > THERMAL_LIMIT


# ── Smoke test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os
    SEP = "=" * 75
    print(SEP)
    print("  CacheSelect BlockEngine v3 — Smoke Test")
    print(SEP)
    engine = BlockEngine()

    BLOCKS = {
        "RANDOM    ": os.urandom(BLOCK_SIZE),
        "REPETITIVE": b"\xAB\xCD" * (BLOCK_SIZE // 2),
        "STRUCTURED": (b"ts=1234567890;level=INFO;msg=cache_hit;\n" * 110)[:BLOCK_SIZE],
        "ALL ZEROS ": b"\x00" * BLOCK_SIZE,
        "NEAR ZERO ": b"\x00" * 4090 + b"\x01\x02\x03\x04\x05\x06",
        # NEW: high entropy but only 5 distinct byte values — should NOT be RAW
        "FEW VALS  ": bytes([0x10, 0x20, 0x30, 0x40, 0x50] * 819 + [0x10]),
    }

    print(f"  {'Block':<12} {'Codec':<6} {'Entropy':>8} {'Uniq':>6} {'CompSize':>9} {'Zero':>5} {'WAF':>7} {'us':>6}")
    print(f"  {'-'*12} {'-'*6} {'-'*8} {'-'*6} {'-'*9} {'-'*5} {'-'*7} {'-'*6}")
    for label, blk in BLOCKS.items():
        r = engine.process_block(blk)
        h    = f"{r['entropy']:.3f}"    if r["entropy"]    is not None else "cached"
        uniq = f"{r['uniqueness']:.3f}" if r["uniqueness"] is not None else "cached"
        print(f"  {label:<12} {r['codec_used']:<6} {h:>8} {uniq:>6} "
              f"{r['compressed_size']:>8}B "
              f"  {'Y' if r['zero_block'] else 'N':>4}  {r['waf_current']:>6.4f} "
              f"  {r['timing']['total_time']*1e6:>5.0f}")

    print()
    print("  Adaptive learning (same block x15 — watch avg_ratio and overrides):")
    print(f"  {'Pass':<6} {'Hit':>5} {'Codec':<7} {'Conf':>5} {'AvgRatio':>9} {'Override':>9}")
    print(f"  {'-'*6} {'-'*5} {'-'*7} {'-'*5} {'-'*9} {'-'*9}")
    ref  = (b"GET /api/v1/user HTTP/1.1\r\nHost: example.com\r\n\r\n" * 82)[:BLOCK_SIZE]
    h1, h2 = mmh3.hash64(ref, seed=42, signed=False)
    sig  = (h1 << 64) | h2
    for i in range(15):
        r    = engine.process_block(ref)
        e    = engine._cache.get((sig, bytes(ref[:PREFIX_LEN])))
        conf = e.confidence if e else 0
        ratio= f"{e.avg_ratio:.4f}" if e else "—"
        ovr  = ("FORCE" if (e and e.trusted and e.avg_ratio < RATIO_FORCE_COMPRESS) else
                "SKIP"  if (e and e.trusted and e.avg_ratio > RATIO_SKIP_COMPRESS)  else "—")
        print(f"  {i+1:<6} {'HIT' if r['cache_hit'] else 'MISS':>5}  {r['codec_used']:<6} "
              f"{conf:>5} {ratio:>9} {ovr:>9}")

    print()
    s = engine.get_stats()
    print("  ── Engine Stats ──────────────────────────────────────────────────")
    print(f"  Blocks processed      : {s['total_blocks_processed']}")
    print(f"  Cache hit rate        : {s['cache_hit_rate']*100:.1f}%")
    print(f"  Cache used            : {s['cache_entries_used']} / {s['cache_capacity_entries']}")
    print(f"  WAF                   : {s['waf']:.4f}   (< 1.0 = saving space)")
    print(f"  Space saved           : {s['space_saving_pct']:.1f}%")
    print(f"  Logical written       : {s['logical_bytes_written']:,} B")
    print(f"  Physical written      : {s['physical_bytes_written']:,} B")
    print(f"  SKIP blocks (zero)    : {s['total_skip_blocks']}")
    print(f"  RAW blocks            : {s['total_raw_blocks']}")
    print(f"  Compressed blocks     : {s['total_compressed_blocks']}")
    print(f"  Adaptive overrides    : {s['adaptive_overrides']}")
    print(f"    skip compress       : {s['adaptive_skip_compress']}")
    print(f"    force compress      : {s['adaptive_force_compress']}")
    print(f"  RAW avoided (uniq.)   : {s['raw_avoided_by_uniqueness']}  [NEW v3]")
    print(f"  Avg entropy           : {s['average_entropy']:.4f} / 8.0")
    print(f"  Avg uniqueness        : {s['average_uniqueness']:.4f} / 1.0  [NEW v3]")
    print(f"  Thermal throttle      : {s['thermal_throttle_active']}")
    print(SEP)
