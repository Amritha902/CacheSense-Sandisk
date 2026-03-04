"""
CacheSelect - Workload Runner
benchmarks/workload_runner.py

Simulates realistic SSD write workloads and evaluates the adaptive
compression policy engine (BlockEngine).

Demonstrates:
  • Cache warm-up behavior
  • Compression selectivity
  • CPU avoidance on incompressible data
  • Compression ratio improvement
  • Throughput characteristics
  • Deterministic behavior (seeded RNG)

Run with:
    python -m benchmarks.workload_runner
"""

import os
import time
import random
import statistics
import csv
import argparse
from core.block_engine import BlockEngine

# ─── Reproducibility ──────────────────────────────────────────────────────────

random.seed(42)

# ─── Configuration ────────────────────────────────────────────────────────────

BLOCK_SIZE   = 4096
TOTAL_BLOCKS = 10_000

# Workload distribution (must sum to 1.0)
WORKLOAD_DISTRIBUTION = {
    "random":       0.40,   # encrypted / JPEG / MP4   → high entropy, incompressible
    "structured":   0.25,   # JSON / source / DB rows  → medium entropy, compressible
    "repetitive":   0.20,   # logs / metadata          → strong RLD, very compressible
    "archive_like": 0.15,   # compressed archive mix   → medium-high entropy, low gain
}

ENABLE_CSV_EXPORT  = False           # set True to write results/benchmark.csv
WARMUP_BLOCKS      = 500             # separate warm-up phase block count
SHOW_WARMUP_DETAIL = True            # print per-interval stats during warm-up

# ─── Workload Generators ──────────────────────────────────────────────────────

def random_block() -> bytes:
    """
    Simulates encrypted data / JPEG / MP4 frames.
    Full 256-byte alphabet sampled uniformly → entropy ~8.0, RLD ~0.004.
    Incompressible: BlockEngine will select CODEC_RAW and skip compression.
    """
    return os.urandom(BLOCK_SIZE)


def repetitive_block() -> bytes:
    """
    Simulates log lines / metadata / zero-padded sectors.
    Short repeated byte pattern → high RLD (>0.4), low entropy.
    BlockEngine will select LZ4 (fast run-length path).
    """
    pattern = b"\xAB\xCD\xAB\xCD\xAB\xCD\xAB\xCD"
    return (pattern * (BLOCK_SIZE // len(pattern) + 1))[:BLOCK_SIZE]


def structured_block() -> bytes:
    """
    Simulates JSON payloads / source code / database row pages.
    Medium entropy, moderate repetition of field names and punctuation.
    BlockEngine will select LZ4HC (maximize ratio on structured data).
    """
    line = b"key=value;timestamp=1234567890;type=metadata;flag=true;\n"
    buf  = b""
    while len(buf) < BLOCK_SIZE:
        buf += line
    return buf[:BLOCK_SIZE]


def archive_like_block() -> bytes:
    """
    Simulates a compressed archive header mixed with binary payload.
    Starts with a realistic ZIP local-file magic header, then fills with
    moderately high-entropy bytes — low compression benefit expected.
    BlockEngine will likely select RAW or fall back after benefit check.
    """
    # ZIP local file header magic + version/flags area
    zip_header = (
        b"PK\x03\x04"           # local file header signature
        b"\x14\x00"             # version needed
        b"\x00\x00"             # general purpose bit flag
        b"\x08\x00"             # compression method (deflate)
        b"\x00\x00\x00\x00"     # last mod time/date
    )
    # Fill remainder with moderately high-entropy binary (simulates deflated body)
    fill_len   = BLOCK_SIZE - len(zip_header)
    # Build a semi-random but seeded byte string for determinism
    fill_bytes = bytes(random.getrandbits(8) for _ in range(fill_len))
    return zip_header + fill_bytes


# ─── Mixed Workload Selector ──────────────────────────────────────────────────

def _build_thresholds() -> list:
    """Pre-compute cumulative probability thresholds from WORKLOAD_DISTRIBUTION."""
    keys       = list(WORKLOAD_DISTRIBUTION.keys())
    thresholds = []
    cumulative = 0.0
    for k in keys:
        cumulative += WORKLOAD_DISTRIBUTION[k]
        thresholds.append((cumulative, k))
    return thresholds

_THRESHOLDS = _build_thresholds()

_GENERATORS = {
    "random":       random_block,
    "structured":   structured_block,
    "repetitive":   repetitive_block,
    "archive_like": archive_like_block,
}

def generate_mixed_block() -> tuple:
    """
    Select and generate a block according to WORKLOAD_DISTRIBUTION.
    Returns (block_bytes, label_str).
    Deterministic when random.seed() has been called.
    """
    r = random.random()
    for threshold, label in _THRESHOLDS:
        if r < threshold:
            return _GENERATORS[label](), label
    # Fallback (floating point edge case)
    return random_block(), "random"


# ─── Warm-up Phase ────────────────────────────────────────────────────────────

def run_warmup(engine: BlockEngine) -> dict:
    """
    Run WARMUP_BLOCKS blocks through the engine to seed the LRU cache.
    Uses only repetitive + structured blocks (most cache-worthy patterns).
    Returns timing and hit-rate summary.
    """
    print(f"\n  Warm-up phase: {WARMUP_BLOCKS} blocks (repetitive + structured only)")
    print(f"  {'Block':>6}  {'Hit Rate':>9}  {'Entries':>8}")
    print(f"  {'-'*6}  {'-'*9}  {'-'*8}")

    interval     = WARMUP_BLOCKS // 5
    warmup_start = time.perf_counter()

    for i in range(WARMUP_BLOCKS):
        blk = repetitive_block() if i % 2 == 0 else structured_block()
        engine.process_block(blk)

        if SHOW_WARMUP_DETAIL and (i + 1) % interval == 0:
            s = engine.get_stats()
            print(
                f"  {i+1:>6}  "
                f"{s['cache_hit_rate']*100:>8.1f}%  "
                f"{s['cache_entries_used']:>8}"
            )

    warmup_elapsed = time.perf_counter() - warmup_start
    s = engine.get_stats()
    print(f"\n  Warm-up complete in {warmup_elapsed:.3f}s — "
          f"{s['cache_entries_used']} entries cached.\n")
    return {"warmup_time": warmup_elapsed, "warmup_entries": s["cache_entries_used"]}


# ─── Benchmark Execution ──────────────────────────────────────────────────────

def run_benchmark(
    total_blocks: int        = TOTAL_BLOCKS,
    enable_warmup: bool      = True,
    export_csv: bool         = ENABLE_CSV_EXPORT,
    workload_override: str   = None,   # "random" | "structured" | "repetitive" | "archive_like"
) -> dict:
    """
    Main benchmark loop.

    Steps:
      1. Instantiate BlockEngine
      2. Optional warm-up phase
      3. Main loop: generate → process → record
      4. Compute metrics from engine.get_stats() + timing
      5. Print structured report
      6. Optional CSV export
    """

    print("=" * 54)
    print("  CacheSelect Workload Simulation")
    print("=" * 54)

    if workload_override:
        if workload_override not in _GENERATORS:
            raise ValueError(f"Unknown workload: {workload_override!r}. "
                             f"Choose from {list(_GENERATORS)}")
        print(f"\n  Mode     : PURE {workload_override.upper()}")
    else:
        dist_str = "  ".join(
            f"{k}={int(v*100)}%" for k, v in WORKLOAD_DISTRIBUTION.items()
        )
        print(f"\n  Mode     : MIXED  ({dist_str})")

    print(f"  Blocks   : {total_blocks:,}")
    print(f"  Warm-up  : {'enabled' if enable_warmup else 'disabled'}")

    # ── 1. Instantiate engine ─────────────────────────────────────────────────
    engine    = BlockEngine()
    warmup_md = {}

    # ── 2. Warm-up ────────────────────────────────────────────────────────────
    if enable_warmup:
        warmup_md = run_warmup(engine)

    # ── 3. Main benchmark loop ────────────────────────────────────────────────
    per_block_times  = []
    workload_counts  = {k: 0 for k in _GENERATORS}
    csv_rows         = []

    t_start = time.perf_counter()

    for i in range(total_blocks):
        if workload_override:
            blk, label = _GENERATORS[workload_override](), workload_override
        else:
            blk, label = generate_mixed_block()

        workload_counts[label] += 1

        t0     = time.perf_counter()
        result = engine.process_block(blk)
        t_blk  = time.perf_counter() - t0

        per_block_times.append(t_blk)

        if export_csv:
            csv_rows.append({
                "block_index":    i,
                "workload_type":  label,
                "codec_used":     result["codec_used"],
                "compressed_size": result["compressed_size"],
                "cache_hit":      int(result["cache_hit"]),
                "entropy":        f"{result['entropy']:.4f}" if result["entropy"] is not None else "",
                "rld":            f"{result['rld']:.4f}"     if result["rld"]     is not None else "",
                "block_time_us":  f"{t_blk * 1e6:.2f}",
            })

    t_end          = time.perf_counter()
    total_time     = t_end - t_start

    # ── 4. Compute metrics ────────────────────────────────────────────────────
    stats            = engine.get_stats()
    blocks_per_sec   = total_blocks / total_time if total_time > 0 else 0.0
    throughput_mbs   = (total_blocks * BLOCK_SIZE) / (total_time * 1024 * 1024) if total_time > 0 else 0.0

    p50_us = statistics.median(per_block_times) * 1e6
    p99_us = sorted(per_block_times)[int(len(per_block_times) * 0.99)] * 1e6
    avg_us = statistics.mean(per_block_times) * 1e6

    raw_pct        = (stats["total_raw_blocks"]        / total_blocks * 100) if total_blocks else 0
    compressed_pct = (stats["total_compressed_blocks"] / total_blocks * 100) if total_blocks else 0

    # ── 5. Print report ───────────────────────────────────────────────────────
    print()
    print("=" * 54)
    print("  --- Performance Summary ---")
    print("=" * 54)
    print(f"  Total Blocks Processed   : {stats['total_blocks_processed']:>10,}")
    print(f"  Total Time (seconds)     : {total_time:>10.3f}")
    print(f"  Blocks per Second        : {blocks_per_sec:>10,.0f}")
    print(f"  Throughput (MB/s)        : {throughput_mbs:>10.2f}")
    print(f"  Avg Latency per Block    : {avg_us:>10.2f} µs")
    print(f"  P50 Latency              : {p50_us:>10.2f} µs")
    print(f"  P99 Latency              : {p99_us:>10.2f} µs")

    print()
    print("=" * 54)
    print("  --- Compression Metrics ---")
    print("=" * 54)
    print(f"  Cache Hit Rate           : {stats['cache_hit_rate']*100:>9.2f} %")
    print(f"  RAW Blocks               : {stats['total_raw_blocks']:>10,}  ({raw_pct:.1f}%)")
    print(f"  Compressed Blocks        : {stats['total_compressed_blocks']:>10,}  ({compressed_pct:.1f}%)")
    print(f"  Average Compression Ratio: {stats['average_compression_ratio']:>10.4f}  (1.0 = no compression)")
    print(f"  Average Entropy (misses) : {stats['average_entropy']:>10.4f}  / 8.0")

    print()
    print("=" * 54)
    print("  --- Cache Usage ---")
    print("=" * 54)
    print(f"  Cache Entries Used       : {stats['cache_entries_used']:>10,}")
    print(f"  Cache Capacity           : {stats['cache_capacity_entries']:>10,}")
    fill_pct = stats['cache_entries_used'] / stats['cache_capacity_entries'] * 100
    print(f"  Cache Fill               : {fill_pct:>9.1f} %")

    if enable_warmup and warmup_md:
        print(f"  Warm-up Entries Seeded   : {warmup_md['warmup_entries']:>10,}")
        print(f"  Warm-up Time             : {warmup_md['warmup_time']:>9.3f}s")

    print()
    print("=" * 54)
    print("  --- Workload Breakdown ---")
    print("=" * 54)
    for wtype, count in workload_counts.items():
        pct = count / total_blocks * 100 if total_blocks else 0
        bar = "█" * int(pct / 2)
        print(f"  {wtype:<14} : {count:>6,}  ({pct:5.1f}%)  {bar}")

    print()

    # ── 6. CSV export ─────────────────────────────────────────────────────────
    if export_csv and csv_rows:
        import pathlib
        out_dir  = pathlib.Path("results")
        out_dir.mkdir(exist_ok=True)
        csv_path = out_dir / "benchmark.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_rows[0].keys())
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"  CSV exported → {csv_path}  ({len(csv_rows)} rows)")
        print()

    return {
        "total_time":          total_time,
        "blocks_per_sec":      blocks_per_sec,
        "throughput_mbs":      throughput_mbs,
        "stats":               stats,
        "workload_counts":     workload_counts,
        "per_block_times":     per_block_times,
    }


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def _parse_args():
    parser = argparse.ArgumentParser(
        description="CacheSelect Workload Runner — SSD compression policy benchmark"
    )
    parser.add_argument(
        "--blocks", type=int, default=TOTAL_BLOCKS,
        help=f"Total blocks to process (default: {TOTAL_BLOCKS})"
    )
    parser.add_argument(
        "--no-warmup", action="store_true",
        help="Skip the warm-up phase"
    )
    parser.add_argument(
        "--workload", choices=list(_GENERATORS), default=None,
        help="Run a single pure workload type instead of mixed"
    )
    parser.add_argument(
        "--csv", action="store_true",
        help="Export per-block results to results/benchmark.csv"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_benchmark(
        total_blocks     = args.blocks,
        enable_warmup    = not args.no_warmup,
        export_csv       = args.csv,
        workload_override= args.workload,
    )
