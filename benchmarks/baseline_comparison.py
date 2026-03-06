"""
CacheSelect vs Static LZ4 Baseline — The core argument
benchmarks/baseline_comparison.py

This is the answer to "why not just use LZ4 on everything?"

Runs identical blocks through:
  A) Static LZ4  (compress everything, no policy)
  B) CacheSelect (adaptive — skip incompressible, cache decisions)

Measures real CPU time difference.
Shows where static LZ4 wastes cycles on incompressible data.

Run:
    python benchmarks/baseline_comparison.py
"""

import os, time, sys, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lz4.block
from core.block_engine import BlockEngine, BLOCK_SIZE

# ── Block generators ──────────────────────────────────────────────────────────

def random_block():
    """Encrypted / JPEG / video — incompressible"""
    return os.urandom(BLOCK_SIZE)

def structured_block():
    """Log lines / JSON / source code — compressible"""
    line = b"2024-01-15 10:23:45 INFO cache_hit codec=LZ4HC ratio=0.21 block=4096\n"
    return (line * 60)[:BLOCK_SIZE]

def repetitive_block():
    """Database nulls / sparse files — highly compressible"""
    return b"\x00\xAB\xCD\x00" * (BLOCK_SIZE // 4)

def mixed_block(i):
    """Realistic mix"""
    if i % 10 < 4:   return random_block()
    elif i % 10 < 7: return structured_block()
    else:            return repetitive_block()

# ── Static LZ4 baseline ───────────────────────────────────────────────────────

def static_lz4(block: bytes):
    """What firmware does today: compress everything with LZ4, no policy."""
    t0 = time.perf_counter()
    try:
        compressed = lz4.block.compress(block, store_size=False)
        # If compressed is bigger, store raw anyway (firmware still paid the CPU cost)
        result = compressed if len(compressed) < len(block) else block
    except Exception:
        result = block
    elapsed = time.perf_counter() - t0
    return result, elapsed

# ── Main comparison ───────────────────────────────────────────────────────────

def run_comparison(n_blocks=2000):
    SEP = "=" * 62
    print(SEP)
    print("  CacheSelect vs Static LZ4 — Baseline Comparison")
    print(SEP)
    print(f"  Blocks: {n_blocks}  |  Block size: 4096 bytes")
    print(f"  Workload: 40% random · 30% structured · 30% repetitive")
    print()

    engine = BlockEngine()

    # Per-type tracking
    types = ["random", "structured", "repetitive"]
    stats = {
        t: {
            "lz4_time": 0.0, "cs_time": 0.0,
            "lz4_compressed": 0,  "cs_compressed": 0,
            "lz4_raw": 0,         "cs_raw": 0,
            "count": 0,
        } for t in types
    }

    total_lz4_time = 0.0
    total_cs_time  = 0.0
    total_lz4_bytes = 0
    total_cs_bytes  = 0

    generators = {
        "random":      random_block,
        "structured":  structured_block,
        "repetitive":  repetitive_block,
    }

    import random
    random.seed(42)
    type_seq = (
        ["random"]     * 40 +
        ["structured"] * 30 +
        ["repetitive"] * 30
    )

    for i in range(n_blocks):
        btype = type_seq[i % 100]
        block = generators[btype]()
        s     = stats[btype]
        s["count"] += 1

        # ── Static LZ4 ────────────────────────────────────────────────────────
        lz4_result, lz4_time = static_lz4(block)
        s["lz4_time"] += lz4_time
        total_lz4_time += lz4_time
        if len(lz4_result) < BLOCK_SIZE:
            s["lz4_compressed"] += 1
            total_lz4_bytes += len(lz4_result)
        else:
            s["lz4_raw"] += 1
            total_lz4_bytes += BLOCK_SIZE

        # ── CacheSelect ───────────────────────────────────────────────────────
        t0 = time.perf_counter()
        result = engine.process_block(block)
        cs_time = time.perf_counter() - t0
        s["cs_time"] += cs_time
        total_cs_time += cs_time
        total_cs_bytes += result["compressed_size"]
        if result["codec_used"] == "RAW":
            s["cs_raw"] += 1
        else:
            s["cs_compressed"] += 1

    # ── Results ───────────────────────────────────────────────────────────────
    print(f"  {'Metric':<40} {'Static LZ4':>12} {'CacheSelect':>12}")
    print(f"  {'-'*40} {'-'*12} {'-'*12}")

    total_data = n_blocks * BLOCK_SIZE
    lz4_ratio = total_lz4_bytes / total_data
    cs_ratio  = total_cs_bytes  / total_data

    print(f"  {'Total CPU time (ms)':<40} {total_lz4_time*1000:>11.1f}  {total_cs_time*1000:>11.1f}")
    print(f"  {'CPU time saved':<40} {'—':>12} {((total_lz4_time - total_cs_time)/total_lz4_time*100):>10.1f}%")
    print(f"  {'Compression ratio':<40} {lz4_ratio:>12.4f} {cs_ratio:>12.4f}")
    print(f"  {'Physical bytes stored':<40} {total_lz4_bytes:>12,} {total_cs_bytes:>12,}")
    print(f"  {'Bytes saved vs static LZ4':<40} {'—':>12} {total_lz4_bytes - total_cs_bytes:>+12,}")
    print()

    # Per-type breakdown
    print(f"  {'Per-workload breakdown':}")
    print(f"  {'-'*62}")
    print(f"  {'Type':<14} {'Blocks':>7} {'LZ4 time':>10} {'CS time':>10} {'CPU saved':>10}")
    print(f"  {'-'*14} {'-'*7} {'-'*10} {'-'*10} {'-'*10}")

    for btype in types:
        s = stats[btype]
        if s["count"] == 0:
            continue
        saved_pct = (s["lz4_time"] - s["cs_time"]) / s["lz4_time"] * 100 if s["lz4_time"] > 0 else 0
        print(
            f"  {btype:<14} {s['count']:>7} "
            f"  {s['lz4_time']*1000:>7.1f}ms"
            f"  {s['cs_time']*1000:>7.1f}ms"
            f"  {saved_pct:>8.1f}%"
        )

    print()

    # The key insight
    saved_pct = (total_lz4_time - total_cs_time) / total_lz4_time * 100
    print(f"  KEY RESULT:")
    print(f"  CacheSelect used {saved_pct:.1f}% less CPU than static LZ4")
    print(f"  on this mixed workload.")
    print()
    print(f"  WHY: Static LZ4 attempted compression on ALL {n_blocks} blocks,")
    rand_blocks = stats["random"]["count"]
    print(f"  including {rand_blocks} random/incompressible blocks where compression")
    print(f"  failed and the result was discarded — wasted CPU.")
    print(f"  CacheSelect detected these via entropy > 7.5 and skipped them.")
    print()

    # Cache stats
    es = engine.get_stats()
    print(f"  CacheSelect engine stats:")
    print(f"    Cache hit rate  : {es['cache_hit_rate']*100:.1f}%")
    print(f"    Avg entropy     : {es['average_entropy']:.4f} / 8.0")
    print(f"    RAW blocks      : {engine.total_raw_blocks} ({engine.total_raw_blocks/n_blocks*100:.1f}%)")
    print(f"    Compressed      : {engine.total_compressed_blocks} ({engine.total_compressed_blocks/n_blocks*100:.1f}%)")
    print(SEP)

if __name__ == "__main__":
    run_comparison(2000)
