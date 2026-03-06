"""
CacheSelect — Real Dataset Benchmark
benchmarks/real_data_test.py

Tests the engine against real files on your system.
No synthetic data. No made-up numbers.

Run:
    python benchmarks/real_data_test.py
"""

import os, sys, time, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.block_engine import BlockEngine, BLOCK_SIZE
import lz4.block

SEP = "=" * 62

def find_real_files():
    """Find real files on this system to test against."""
    candidates = []

    search_dirs = [
        ".",                          # this project itself
        os.path.expanduser("~"),
        "/usr/lib/python3",
        "/etc",
        "/usr/share/doc",
    ]
    extensions = {".py", ".txt", ".md", ".json", ".c", ".h", ".log", ".csv"}

    for d in search_dirs:
        if not os.path.exists(d):
            continue
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in files:
                if any(f.endswith(e) for e in extensions):
                    path = os.path.join(root, f)
                    try:
                        size = os.path.getsize(path)
                        if 1024 < size < 5 * 1024 * 1024:  # 1KB–5MB
                            candidates.append((path, size))
                    except OSError:
                        pass
            if len(candidates) >= 200:
                break
        if len(candidates) >= 200:
            break

    # Sort by size, pick diverse set
    candidates.sort(key=lambda x: x[1])
    step = max(1, len(candidates) // 30)
    return [c[0] for c in candidates[::step]][:30]


def process_file(path, engine):
    """Run one real file through CacheSelect and static LZ4. Return stats."""
    with open(path, "rb") as f:
        data = f.read()

    filename = os.path.basename(path)
    ext      = os.path.splitext(path)[1] or "other"
    total_blocks = max(1, (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE)

    cs_bytes  = 0
    lz4_bytes = 0
    cs_raw    = 0
    cs_comp   = 0
    cs_time   = 0.0
    lz4_time  = 0.0

    pos = 0
    while pos < len(data):
        chunk = data[pos:pos + BLOCK_SIZE]
        if len(chunk) < BLOCK_SIZE:
            chunk = chunk + b"\x00" * (BLOCK_SIZE - len(chunk))
        pos += BLOCK_SIZE

        # CacheSelect
        t0 = time.perf_counter()
        r  = engine.process_block(chunk)
        cs_time += time.perf_counter() - t0
        cs_bytes += r["compressed_size"]
        if r["codec_used"] == "RAW": cs_raw  += 1
        else:                        cs_comp += 1

        # Static LZ4
        t0 = time.perf_counter()
        try:
            comp = lz4.block.compress(chunk, store_size=False)
            lz4_bytes += len(comp) if len(comp) < BLOCK_SIZE else BLOCK_SIZE
        except Exception:
            lz4_bytes += BLOCK_SIZE
        lz4_time += time.perf_counter() - t0

    cs_ratio  = cs_bytes  / (total_blocks * BLOCK_SIZE)
    lz4_ratio = lz4_bytes / (total_blocks * BLOCK_SIZE)
    cpu_saved = (lz4_time - cs_time) / lz4_time * 100 if lz4_time > 0 else 0

    return {
        "filename":   filename[:28],
        "ext":        ext,
        "blocks":     total_blocks,
        "cs_ratio":   cs_ratio,
        "lz4_ratio":  lz4_ratio,
        "ratio_delta": lz4_ratio - cs_ratio,
        "cpu_saved":  cpu_saved,
        "cs_raw_pct": cs_raw / total_blocks * 100,
    }


def run():
    print(SEP)
    print("  CacheSelect — Real File Dataset Benchmark")
    print(SEP)

    files = find_real_files()
    if not files:
        print("  No files found. Run from project root.")
        return

    print(f"  Found {len(files)} real files to test")
    print()

    engine  = BlockEngine()
    results = []

    for path in files:
        try:
            r = process_file(path, engine)
            results.append(r)
        except Exception:
            pass

    if not results:
        print("  Could not read any files.")
        return

    # Print table
    print(f"  {'File':<30} {'Ext':<6} {'Blks':>5} {'CS':>7} {'LZ4':>7} {'Better':>8} {'CPU saved':>10}")
    print(f"  {'-'*30} {'-'*6} {'-'*5} {'-'*7} {'-'*7} {'-'*8} {'-'*10}")

    for r in results:
        better = "CS ✓" if r["cs_ratio"] <= r["lz4_ratio"] else "LZ4"
        print(
            f"  {r['filename']:<30} {r['ext']:<6} {r['blocks']:>5} "
            f"  {r['cs_ratio']:.3f}  {r['lz4_ratio']:.3f} "
            f"  {better:>8}  {r['cpu_saved']:>+8.1f}%"
        )

    # Summary
    print()
    avg_cs      = sum(r["cs_ratio"]  for r in results) / len(results)
    avg_lz4     = sum(r["lz4_ratio"] for r in results) / len(results)
    avg_cpu     = sum(r["cpu_saved"] for r in results) / len(results)
    cs_wins     = sum(1 for r in results if r["cs_ratio"] <= r["lz4_ratio"])

    print(SEP)
    print(f"  Files tested          : {len(results)}")
    print(f"  Avg compression ratio : CacheSelect={avg_cs:.4f}  StaticLZ4={avg_lz4:.4f}")
    print(f"  CacheSelect better on : {cs_wins}/{len(results)} files")
    print(f"  Avg CPU time saved    : {avg_cpu:+.1f}%")
    print()

    es = engine.get_stats()
    print(f"  Engine stats (across all files):")
    print(f"    Blocks processed : {es['total_blocks_processed']}")
    print(f"    Cache hit rate   : {es['cache_hit_rate']*100:.1f}%")
    print(f"    Avg entropy      : {es['average_entropy']:.4f} / 8.0")
    print(f"    RAW blocks       : {engine.total_raw_blocks} ({engine.total_raw_blocks/max(1,es['total_blocks_processed'])*100:.1f}%)")
    print(SEP)
    print()
    print("  NOTE: Ratio < 1.0 = compressed. Lower is better.")
    print("  CacheSelect advantage: skips compression on high-entropy")
    print("  blocks entirely — saving CPU even when ratio matches LZ4.")

if __name__ == "__main__":
    run()
