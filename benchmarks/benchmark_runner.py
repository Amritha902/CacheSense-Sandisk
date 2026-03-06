"""
=============================================================================
MODULE: benchmark_runner.py
DESCRIPTION: Benchmark Tool for SSD Compression Engine

Simulates realistic SSD write workloads and measures pipeline performance
across four workload categories matching the design document distribution:

  Workload Distribution (mirrors enterprise SSD write mix):
  ┌─────────────────────┬──────────┬──────────────────────────┐
  │ Workload Type       │  Weight  │ Block Characteristics     │
  ├─────────────────────┼──────────┼──────────────────────────┤
  │ Random Data         │   40%    │ High entropy, no patterns │
  │ Structured Logs     │   25%    │ Low-mid entropy, text     │
  │ Repetitive Data     │   20%    │ Low entropy, high RLD     │
  │ Archives/Compressed │   15%    │ Very high entropy (pre-   │
  │                     │          │ compressed data)          │
  └─────────────────────┴──────────┴──────────────────────────┘

OUTPUT METRICS:
  - blocks_processed     : total blocks sent through pipeline
  - throughput_MBps      : raw input throughput in MB/second
  - compression_ratio    : weighted average compression ratio
  - cache_hit_rate       : pattern cache effectiveness
  - avg_latency_ms       : average per-block latency
  - per_workload_stats   : breakdown by workload type

USAGE:
    runner = BenchmarkRunner(output_dir='output/')
    results = runner.run(total_blocks=1000, seed=42)
    runner.print_report(results)
    runner.save_csv(results, 'benchmark_results.csv')
=============================================================================
"""

import os
import sys
import csv
import json
import time
import random
import string
import struct
from typing import Dict, Any, List, Optional, Callable

# --- Path adjustment for direct execution ---
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

from fuse.firmware_pipeline import FirmwarePipeline, PipelineResult, BLOCK_SIZE

# ---------------------------------------------------------------------------
# Workload Definitions
# ---------------------------------------------------------------------------

WORKLOAD_DISTRIBUTION = {
    'random_data'    : 0.40,
    'structured_logs': 0.25,
    'repetitive_data': 0.20,
    'archives'       : 0.15,
}

assert abs(sum(WORKLOAD_DISTRIBUTION.values()) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Block Generators
# ---------------------------------------------------------------------------

class BlockGenerators:
    """
    Factory for generating synthetic test blocks of different types.
    Each generator returns exactly 4096 bytes of block data.
    """

    @staticmethod
    def random_data(_seed: int = 0) -> bytes:
        """
        Random data block (40% workload).
        Simulates: encrypted files, already-compressed data, /dev/urandom writes.
        Characteristics: entropy ~7.9-8.0, RLD ~0.01-0.05
        """
        return os.urandom(BLOCK_SIZE)

    @staticmethod
    def structured_log(seed: int = 0) -> bytes:
        """
        Structured log block (25% workload).
        Simulates: application logs, database WAL, audit trails.
        Characteristics: entropy ~4.5-5.5, RLD ~0.05-0.15
        """
        rng = random.Random(seed)
        log_levels  = [b'INFO ', b'DEBUG', b'WARN ', b'ERROR', b'TRACE']
        modules     = [b'StorageEngine', b'WriteBuffer', b'GCManager',
                       b'NVMeController', b'FTL', b'ECC']
        msgs        = [
            b'write_command_received lba=0x{:08X} len=4096',
            b'cache_lookup sig=0x{:016X} result=miss',
            b'gc_cycle started: victim_block=0x{:04X} valid_pages={}',
            b'compression_complete ratio={:.4f} codec={} latency_us={}',
            b'pattern_cache hit sig=0x{:016X} codec=LZ4HC ratio=0.3201',
        ]

        block = bytearray()
        ts_h, ts_m, ts_s, ts_ms = 12, 0, 0, 0
        while len(block) < BLOCK_SIZE:
            ts_ms += rng.randint(1, 50)
            if ts_ms >= 1000:
                ts_ms -= 1000; ts_s += 1
            if ts_s >= 60:
                ts_s = 0; ts_m += 1

            level   = rng.choice(log_levels)
            module  = rng.choice(modules)
            msg_tmpl= rng.choice(msgs)

            try:
                msg = msg_tmpl.format(
                    rng.randint(0, 0xFFFFFFFF),
                    rng.randint(0, 0xFFFFFFFFFFFFFFFF),
                    rng.randint(0, 0xFFFF),
                    rng.randint(0, 255),
                    rng.uniform(0.1, 1.0),
                    rng.choice([b'LZ4', b'LZ4HC', b'RAW']).decode(),
                    rng.randint(10, 500),
                )
            except Exception:
                msg = msg_tmpl.replace(b'{', b'[').replace(b'}', b']').decode()

            line = (f"{ts_h:02d}:{ts_m:02d}:{ts_s:02d}.{ts_ms:03d} "
                    f"[{module.decode()}] {level.decode()} "
                    f"{msg if isinstance(msg, str) else msg.decode()}\n"
                    ).encode('ascii', errors='replace')
            block.extend(line)

        return bytes(block[:BLOCK_SIZE])

    @staticmethod
    def repetitive_data(seed: int = 0) -> bytes:
        """
        Repetitive pattern block (20% workload).
        Simulates: database zero pages, buffer fills, null-padded records.
        Characteristics: entropy ~0.5-3.0, RLD ~0.5-0.99
        """
        rng = random.Random(seed)
        kind = seed % 4

        if kind == 0:
            # All-zero block
            return bytes(BLOCK_SIZE)
        elif kind == 1:
            # Single repeating byte
            b = rng.randint(0, 255)
            return bytes([b] * BLOCK_SIZE)
        elif kind == 2:
            # Short repeating pattern
            plen   = rng.choice([2, 4, 8, 16])
            pat    = bytes([rng.randint(0, 255) for _ in range(plen)])
            return (pat * (BLOCK_SIZE // plen + 1))[:BLOCK_SIZE]
        else:
            # Mostly zeros with sparse data (database null-padded row)
            buf = bytearray(BLOCK_SIZE)
            n_writes = rng.randint(5, 30)
            for _ in range(n_writes):
                offset = rng.randint(0, BLOCK_SIZE - 16)
                length = rng.randint(4, 16)
                for i in range(length):
                    buf[offset + i] = rng.randint(1, 255)
            return bytes(buf)

    @staticmethod
    def archive_data(seed: int = 0) -> bytes:
        """
        Pre-compressed archive block (15% workload).
        Simulates: ZIP, gzip, LZ4 compressed data already on disk.
        These blocks have very high entropy and should NOT be re-compressed.
        Characteristics: entropy ~7.5-8.0, RLD ~0.01-0.03
        """
        # Simulate compressed archive data: random-looking with
        # slight structure at the beginning (archive header)
        rng = random.Random(seed)
        # Fake ZIP local file header
        header = b'PK\x03\x04'   # ZIP magic
        header += struct.pack('<HHHHH', 20, 0, 8, 0, 0)  # version, flags, method, ...
        # Rest: high-entropy pseudo-random data
        random_tail = bytes([rng.randint(0, 255) for _ in range(BLOCK_SIZE - len(header))])
        return (header + random_tail)[:BLOCK_SIZE]


BLOCK_GENERATOR_MAP: Dict[str, Callable] = {
    'random_data'    : BlockGenerators.random_data,
    'structured_logs': BlockGenerators.structured_log,
    'repetitive_data': BlockGenerators.repetitive_data,
    'archives'       : BlockGenerators.archive_data,
}


# ---------------------------------------------------------------------------
# Benchmark Result
# ---------------------------------------------------------------------------

class BenchmarkResult:
    """Complete benchmark run results."""

    def __init__(self):
        self.run_id              : str   = ''
        self.timestamp           : float = time.time()
        self.total_blocks        : int   = 0
        self.total_duration_s    : float = 0.0
        self.throughput_MBps     : float = 0.0
        self.compression_ratio   : float = 1.0
        self.space_saving_pct    : float = 0.0
        self.cache_hit_rate      : float = 0.0
        self.avg_latency_ms      : float = 0.0
        self.p95_latency_ms      : float = 0.0
        self.p99_latency_ms      : float = 0.0
        self.per_workload        : Dict[str, Any] = {}
        self.codec_distribution  : Dict[str, float] = {}
        self.errors              : int   = 0
        self.pipeline_metrics    : Dict  = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            'run_id'             : self.run_id,
            'timestamp'          : self.timestamp,
            'total_blocks'       : self.total_blocks,
            'duration_s'         : round(self.total_duration_s, 3),
            'throughput_MBps'    : round(self.throughput_MBps, 3),
            'compression_ratio'  : round(self.compression_ratio, 4),
            'space_saving_pct'   : round(self.space_saving_pct, 2),
            'cache_hit_rate'     : round(self.cache_hit_rate, 4),
            'avg_latency_ms'     : round(self.avg_latency_ms, 4),
            'p95_latency_ms'     : round(self.p95_latency_ms, 4),
            'p99_latency_ms'     : round(self.p99_latency_ms, 4),
            'errors'             : self.errors,
            'per_workload'       : self.per_workload,
            'codec_distribution' : self.codec_distribution,
        }


# ---------------------------------------------------------------------------
# Benchmark Runner
# ---------------------------------------------------------------------------

class BenchmarkRunner:
    """
    SSD compression engine benchmark driver.

    Generates synthetic workloads, drives the firmware pipeline,
    and measures comprehensive performance metrics.
    """

    def __init__(self,
                 output_dir: str = 'output',
                 verbose    : bool = True):
        self.output_dir = output_dir
        self.verbose    = verbose
        os.makedirs(output_dir, exist_ok=True)

    def _generate_workload(self, total_blocks: int,
                            seed: int = 42) -> List[tuple]:
        """
        Generate a mixed workload according to WORKLOAD_DISTRIBUTION.

        Returns list of (workload_type, block_bytes) tuples in shuffled order.
        """
        rng    = random.Random(seed)
        tasks  = []

        for wtype, fraction in WORKLOAD_DISTRIBUTION.items():
            n_blocks = int(total_blocks * fraction)
            gen      = BLOCK_GENERATOR_MAP[wtype]
            for i in range(n_blocks):
                block_seed = rng.randint(0, 2**31)
                tasks.append((wtype, gen(block_seed)))

        # Shuffle to interleave workload types (realistic mixed write stream)
        rng.shuffle(tasks)

        # Fill remaining blocks with random_data if rounding left a gap
        while len(tasks) < total_blocks:
            tasks.append(('random_data', BlockGenerators.random_data()))

        return tasks[:total_blocks]

    def run(self, total_blocks : int = 1000,
            seed              : int = 42,
            run_id            : str = '') -> BenchmarkResult:
        """
        Execute a full benchmark run.

        Args:
            total_blocks : number of 4KB blocks to process
            seed         : random seed for reproducibility
            run_id       : optional identifier for this run

        Returns: BenchmarkResult with all metrics
        """
        if not run_id:
            run_id = f"bench_{int(time.time())}"

        if self.verbose:
            print()
            print("┌──────────────────────────────────────────────────────┐")
            print("│         SSD Compression Benchmark Runner             │")
            print("├──────────────────────────────────────────────────────┤")
            print(f"│  Run ID       : {run_id:<36} │")
            print(f"│  Total blocks : {total_blocks:<36,} │")
            print(f"│  Total data   : {total_blocks * 4096 / 1024 / 1024:<36.1f} │  MB")
            print(f"│  Seed         : {seed:<36} │")
            print("└──────────────────────────────────────────────────────┘")

        # --- Generate workload ---
        if self.verbose:
            print(f"\n  [1/3] Generating {total_blocks:,} synthetic blocks...")

        tasks = self._generate_workload(total_blocks, seed)

        if self.verbose:
            counts = {}
            for wtype, _ in tasks:
                counts[wtype] = counts.get(wtype, 0) + 1
            for wtype, count in counts.items():
                pct = count / total_blocks * 100
                bar = '█' * int(pct / 3)
                print(f"    {wtype:<20}: {count:5,} blocks ({pct:.1f}%) {bar}")

        # --- Run pipeline ---
        bench_output = os.path.join(self.output_dir, f'{run_id}_blocks.bin')

        if self.verbose:
            print(f"\n  [2/3] Running pipeline on {total_blocks:,} blocks...")
            print(f"  {'─'*54}")

        per_workload_results: Dict[str, List[PipelineResult]] = {
            k: [] for k in WORKLOAD_DISTRIBUTION
        }
        all_latencies = []

        t_start = time.perf_counter()

        with FirmwarePipeline(
            output_dir      = self.output_dir,
            output_filename = f'{run_id}_blocks.bin',
            verbose         = self.verbose,
            log_every_n     = max(1, total_blocks // 10),
        ) as pipeline:

            for i, (wtype, block) in enumerate(tasks):
                result = pipeline.process_block(block, label=wtype[:8])
                per_workload_results[wtype].append(result)
                all_latencies.append(result.total_us)

            total_duration = time.perf_counter() - t_start
            pipeline_metrics = pipeline.get_metrics()

            if self.verbose:
                print(f"\n  [3/3] Computing benchmark metrics...")
            pipeline.print_summary()

        # --- Compute benchmark result ---
        result_obj = BenchmarkResult()
        result_obj.run_id           = run_id
        result_obj.total_blocks     = total_blocks
        result_obj.total_duration_s = total_duration
        result_obj.errors           = pipeline_metrics['pipeline']['total_errors']
        result_obj.pipeline_metrics = pipeline_metrics

        # Throughput
        total_mb = total_blocks * BLOCK_SIZE / 1e6
        result_obj.throughput_MBps = total_mb / max(total_duration, 1e-9)

        # Compression
        comp = pipeline_metrics['compression']
        result_obj.compression_ratio  = comp['overall_ratio']
        result_obj.space_saving_pct   = comp['space_saving_pct']

        # Cache
        result_obj.cache_hit_rate = pipeline_metrics['cache']['hit_rate']

        # Latency stats
        all_latencies.sort()
        n = len(all_latencies)
        result_obj.avg_latency_ms = (sum(all_latencies) / n / 1000) if n > 0 else 0
        result_obj.p95_latency_ms = all_latencies[int(0.95 * n)] / 1000 if n > 0 else 0
        result_obj.p99_latency_ms = all_latencies[int(0.99 * n)] / 1000 if n > 0 else 0

        # Per-workload stats
        for wtype, results in per_workload_results.items():
            if not results:
                continue
            ratios    = [r.ratio for r in results]
            latencies = [r.total_us for r in results]
            hits      = sum(1 for r in results if r.cache_hit)

            result_obj.per_workload[wtype] = {
                'block_count'    : len(results),
                'avg_ratio'      : round(sum(ratios) / len(ratios), 4),
                'avg_latency_us' : round(sum(latencies) / len(latencies), 2),
                'cache_hit_rate' : round(hits / len(results), 4),
                'space_saving_pct': round((1 - sum(ratios)/len(ratios)) * 100, 2),
            }

        # Codec distribution
        result_obj.codec_distribution = pipeline_metrics.get(
            'selector', {}).get('codec_distribution_pct', {})

        return result_obj

    def print_report(self, result: BenchmarkResult) -> None:
        """Print formatted benchmark report."""
        r = result.to_dict()
        print()
        print("╔══════════════════════════════════════════════════════════╗")
        print("║              BENCHMARK REPORT                            ║")
        print(f"║  Run: {r['run_id']:<50} ║")
        print("╠══════════════════════════════════════════════════════════╣")
        print(f"║  Blocks Processed   : {r['total_blocks']:>10,}                        ║")
        print(f"║  Duration           : {r['duration_s']:>10.2f} seconds                  ║")
        print(f"║  Throughput         : {r['throughput_MBps']:>10.2f} MB/sec                  ║")
        print(f"║  Compression Ratio  : {r['compression_ratio']:>10.4f}                        ║")
        print(f"║  Space Saving       : {r['space_saving_pct']:>10.2f}%                        ║")
        print(f"║  Cache Hit Rate     : {r['cache_hit_rate']:>10.2%}                        ║")
        print(f"║  Avg Latency        : {r['avg_latency_ms']:>10.4f} ms/block               ║")
        print(f"║  P95 Latency        : {r['p95_latency_ms']:>10.4f} ms/block               ║")
        print(f"║  P99 Latency        : {r['p99_latency_ms']:>10.4f} ms/block               ║")
        print(f"║  Errors             : {r['errors']:>10,}                        ║")
        print("╠══════════════════════════════════════════════════════════╣")
        print("║  PER-WORKLOAD BREAKDOWN                                  ║")
        for wtype, stats in r['per_workload'].items():
            print(f"║  {wtype:<20}                                       ║")
            print(f"║    Blocks: {stats['block_count']:5,}  "
                  f"Ratio: {stats['avg_ratio']:.4f}  "
                  f"Saving: {stats['space_saving_pct']:+.1f}%  "
                  f"Hit: {stats['cache_hit_rate']:.0%}      ║")
        print("╠══════════════════════════════════════════════════════════╣")
        print("║  CODEC DISTRIBUTION                                      ║")
        for codec, pct in r['codec_distribution'].items():
            if pct > 0:
                bar = '█' * max(1, int(pct / 4))
                print(f"║  {codec:<8} {pct:>6.1f}%  {bar:<30}         ║")
        print("╚══════════════════════════════════════════════════════════╝")

    def save_csv(self, result: BenchmarkResult,
                 filepath: Optional[str] = None) -> str:
        """
        Save benchmark metrics to CSV.

        Args:
            result   : BenchmarkResult from run()
            filepath : output path (auto-generated if None)

        Returns: path to written CSV
        """
        if filepath is None:
            filepath = os.path.join(
                self.output_dir, f'{result.run_id}_metrics.csv')

        rows = []

        # Overall metrics
        d = result.to_dict()
        rows.append(['metric', 'value', 'unit'])
        rows.append(['run_id',             d['run_id'],             ''])
        rows.append(['total_blocks',       d['total_blocks'],       'blocks'])
        rows.append(['duration',           d['duration_s'],         'seconds'])
        rows.append(['throughput',         d['throughput_MBps'],    'MB/sec'])
        rows.append(['compression_ratio',  d['compression_ratio'],  ''])
        rows.append(['space_saving',       d['space_saving_pct'],   '%'])
        rows.append(['cache_hit_rate',     d['cache_hit_rate'],     ''])
        rows.append(['avg_latency',        d['avg_latency_ms'],     'ms'])
        rows.append(['p95_latency',        d['p95_latency_ms'],     'ms'])
        rows.append(['p99_latency',        d['p99_latency_ms'],     'ms'])
        rows.append(['errors',             d['errors'],             'count'])
        rows.append(['', '', ''])

        # Per-workload
        rows.append(['workload', 'blocks', 'avg_ratio', 'saving_pct',
                     'cache_hit_rate', 'avg_latency_us'])
        for wtype, stats in d['per_workload'].items():
            rows.append([
                wtype, stats['block_count'], stats['avg_ratio'],
                stats['space_saving_pct'], stats['cache_hit_rate'],
                stats['avg_latency_us'],
            ])

        # Codec distribution
        rows.append(['', '', ''])
        rows.append(['codec', 'pct', '', '', '', ''])
        for codec, pct in d['codec_distribution'].items():
            rows.append([codec, pct, '', '', '', ''])

        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerows(rows)

        return filepath

    def save_json(self, result: BenchmarkResult,
                  filepath: Optional[str] = None) -> str:
        """Save complete benchmark result as JSON."""
        if filepath is None:
            filepath = os.path.join(
                self.output_dir, f'{result.run_id}_full.json')

        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(result.to_dict(), f, indent=2)
        return filepath


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    OUTPUT_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'output'
    )

    runner = BenchmarkRunner(output_dir=OUTPUT_DIR, verbose=True)

    # Run benchmark with 400 blocks (quick demo)
    result = runner.run(
        total_blocks = 400,
        seed         = 42,
        run_id       = 'sandisk_hackathon_v1',
    )

    # Print full report
    runner.print_report(result)

    # Save results
    csv_path  = runner.save_csv(result)
    json_path = runner.save_json(result)

    print(f"\n  Results saved:")
    print(f"    CSV  → {csv_path}")
    print(f"    JSON → {json_path}")
