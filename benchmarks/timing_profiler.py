"""
=============================================================================
MODULE: timing_profiler.py
DESCRIPTION: Firmware Pipeline Timing Profiler

Measures per-stage latency across the full compression pipeline:
  hashing → cache_lookup → feature_extraction → codec_selection →
  compression → block_packing

Mirrors the performance counter registers in real SSD firmware controllers
that track per-subsystem latency budgets.

FIRMWARE ANALOGY:
  In Cortex-R5 / RISC-V firmware, performance counters (PMU) measure
  cycle counts per subsystem:
    - Hash engine: dedicated HW, ~10 ns
    - Feature extraction: SRAM-based DSP ops, ~15 µs
    - Compression engine: DMA + HW codec, ~50-200 µs
    - Block packing: PCIe DMA descriptor build, ~5 µs

  This Python profiler simulates the same measurements at microsecond
  resolution using time.perf_counter().

USAGE:
    profiler = TimingProfiler()
    
    with profiler.measure('hashing'):
        sig = compute_signature(block)
    
    with profiler.measure('compression'):
        result = compress_block(block, 'LZ4')
    
    profiler.commit()          # record this block's timings
    profiler.export_csv('timings.csv')
=============================================================================
"""

import time
import csv
import os
import json
import math
from contextlib import contextmanager
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Stage Definitions
# ---------------------------------------------------------------------------

PIPELINE_STAGES = [
    'hashing',          # MurmurHash3 computation
    'cache_lookup',     # Pattern cache lookup
    'feature_extract',  # Entropy + RLD analysis
    'codec_select',     # Policy decision
    'compression',      # LZ4/LZ4HC/RAW
    'block_packing',    # Frame assembly + CRC
]

# Firmware timing budget (µs) per stage — from design spec
TIMING_BUDGET_US = {
    'hashing'        : 2.0,
    'cache_lookup'   : 1.0,
    'feature_extract': 15.0,
    'codec_select'   : 1.0,
    'compression'    : 200.0,
    'block_packing'  : 5.0,
}

TOTAL_BUDGET_US = sum(TIMING_BUDGET_US.values())


# ---------------------------------------------------------------------------
# BlockTimings: single-block record
# ---------------------------------------------------------------------------

@dataclass
class BlockTimings:
    """
    Timing record for a single pipeline pass.

    Mirrors firmware per-block perf_record_t written to a ring buffer
    in SRAM for offline analysis.
    """
    block_index     : int
    hashing_us      : float = 0.0
    cache_lookup_us : float = 0.0
    feature_us      : float = 0.0
    codec_select_us : float = 0.0
    compression_us  : float = 0.0
    packing_us      : float = 0.0

    # Context
    codec_used      : str   = ''
    cache_hit       : bool  = False
    compression_ratio: float = 1.0

    @property
    def total_us(self) -> float:
        return (self.hashing_us + self.cache_lookup_us +
                self.feature_us + self.codec_select_us +
                self.compression_us + self.packing_us)

    @property
    def within_budget(self) -> bool:
        return self.total_us <= TOTAL_BUDGET_US

    def to_dict(self) -> Dict[str, Any]:
        return {
            'block_index'      : self.block_index,
            'hashing_us'       : round(self.hashing_us, 3),
            'cache_lookup_us'  : round(self.cache_lookup_us, 3),
            'feature_us'       : round(self.feature_us, 3),
            'codec_select_us'  : round(self.codec_select_us, 3),
            'compression_us'   : round(self.compression_us, 3),
            'packing_us'       : round(self.packing_us, 3),
            'total_us'         : round(self.total_us, 3),
            'codec_used'       : self.codec_used,
            'cache_hit'        : self.cache_hit,
            'compression_ratio': round(self.compression_ratio, 4),
            'within_budget'    : self.within_budget,
        }

    def csv_row(self) -> list:
        d = self.to_dict()
        return [d[k] for k in [
            'block_index', 'hashing_us', 'cache_lookup_us',
            'feature_us', 'codec_select_us', 'compression_us',
            'packing_us', 'total_us', 'codec_used',
            'cache_hit', 'compression_ratio', 'within_budget'
        ]]

    @staticmethod
    def csv_header() -> list:
        return [
            'block_index', 'hashing_us', 'cache_lookup_us',
            'feature_us', 'codec_select_us', 'compression_us',
            'packing_us', 'total_us', 'codec_used',
            'cache_hit', 'compression_ratio', 'within_budget'
        ]


# ---------------------------------------------------------------------------
# TimingProfiler
# ---------------------------------------------------------------------------

class TimingProfiler:
    """
    Per-stage timing profiler for the SSD compression pipeline.

    Supports two usage patterns:

    Pattern A — Context manager (recommended):
        with profiler.measure('hashing'):
            result = hash_block(block)
        profiler.commit()

    Pattern B — Manual start/stop:
        profiler.start('hashing')
        result = hash_block(block)
        profiler.stop('hashing')
        profiler.commit()
    """

    def __init__(self, history_limit: int = 100_000):
        self._history_limit = history_limit
        self._history       : List[BlockTimings] = []

        # Current-block accumulator (reset on each commit)
        self._current : Dict[str, float] = {s: 0.0 for s in PIPELINE_STAGES}
        self._stage_start_ns : Dict[str, int] = {}
        self._current_meta   : Dict[str, Any] = {}

        # Running statistics
        self._stage_totals  : Dict[str, float] = {s: 0.0 for s in PIPELINE_STAGES}
        self._stage_counts  : Dict[str, int]   = {s: 0   for s in PIPELINE_STAGES}
        self._stage_min     : Dict[str, float] = {s: float('inf') for s in PIPELINE_STAGES}
        self._stage_max     : Dict[str, float] = {s: 0.0 for s in PIPELINE_STAGES}
        self._stage_sq      : Dict[str, float] = {s: 0.0 for s in PIPELINE_STAGES}  # for std dev

        self._block_count   = 0
        self._budget_exceed = 0
        self._start_wall    = time.perf_counter()

    # ------------------------------------------------------------------
    # Context manager interface
    # ------------------------------------------------------------------

    @contextmanager
    def measure(self, stage: str):
        """
        Context manager to measure time spent in a pipeline stage.

        Usage:
            with profiler.measure('compression'):
                result = compress_block(data, 'LZ4')
        """
        t0 = time.perf_counter_ns()
        try:
            yield
        finally:
            elapsed_us = (time.perf_counter_ns() - t0) / 1000.0
            self._record_stage(stage, elapsed_us)

    # ------------------------------------------------------------------
    # Manual interface
    # ------------------------------------------------------------------

    def start(self, stage: str) -> None:
        """Start timing a stage manually."""
        self._stage_start_ns[stage] = time.perf_counter_ns()

    def stop(self, stage: str) -> float:
        """
        Stop timing a stage and record elapsed time.
        Returns elapsed microseconds.
        """
        if stage not in self._stage_start_ns:
            return 0.0
        elapsed_us = (time.perf_counter_ns() -
                      self._stage_start_ns.pop(stage)) / 1000.0
        self._record_stage(stage, elapsed_us)
        return elapsed_us

    def set_metadata(self, **kwargs) -> None:
        """
        Set metadata for the current block (codec, cache_hit, ratio).

        Example:
            profiler.set_metadata(codec_used='LZ4', cache_hit=True,
                                  compression_ratio=0.45)
        """
        self._current_meta.update(kwargs)

    # ------------------------------------------------------------------
    # Block commit
    # ------------------------------------------------------------------

    def commit(self) -> BlockTimings:
        """
        Commit current block timings to history.

        Called once per block after all stages complete.
        Returns the BlockTimings record for this block.
        """
        self._block_count += 1

        bt = BlockTimings(
            block_index      = self._block_count,
            hashing_us       = self._current.get('hashing', 0.0),
            cache_lookup_us  = self._current.get('cache_lookup', 0.0),
            feature_us       = self._current.get('feature_extract', 0.0),
            codec_select_us  = self._current.get('codec_select', 0.0),
            compression_us   = self._current.get('compression', 0.0),
            packing_us       = self._current.get('block_packing', 0.0),
            codec_used       = self._current_meta.get('codec_used', ''),
            cache_hit        = self._current_meta.get('cache_hit', False),
            compression_ratio= self._current_meta.get('compression_ratio', 1.0),
        )

        if not bt.within_budget:
            self._budget_exceed += 1

        # Append to rolling history
        if len(self._history) >= self._history_limit:
            self._history.pop(0)
        self._history.append(bt)

        # Reset for next block
        self._current      = {s: 0.0 for s in PIPELINE_STAGES}
        self._current_meta = {}

        return bt

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """
        Return comprehensive profiler statistics.

        Includes per-stage averages and overall pipeline performance.
        """
        n = max(self._block_count, 1)

        per_stage = {}
        for stage in PIPELINE_STAGES:
            count = self._stage_counts[stage]
            if count == 0:
                per_stage[stage] = {
                    'count': 0, 'avg_us': 0.0, 'min_us': 0.0,
                    'max_us': 0.0, 'std_us': 0.0,
                    'budget_us': TIMING_BUDGET_US[stage],
                    'budget_pct': 0.0
                }
                continue

            avg  = self._stage_totals[stage] / count
            var  = max(0.0, self._stage_sq[stage] / count - avg * avg)
            std  = math.sqrt(var)
            bdgt = TIMING_BUDGET_US[stage]

            per_stage[stage] = {
                'count'     : count,
                'avg_us'    : round(avg, 3),
                'min_us'    : round(self._stage_min[stage], 3),
                'max_us'    : round(self._stage_max[stage], 3),
                'std_us'    : round(std, 3),
                'budget_us' : bdgt,
                'budget_pct': round(avg / bdgt * 100 if bdgt > 0 else 0, 1),
            }

        # Total pipeline latency (sum of averages)
        avg_total = sum(
            per_stage[s]['avg_us'] for s in PIPELINE_STAGES
        )
        throughput = (n / (time.perf_counter() - self._start_wall)
                      if time.perf_counter() - self._start_wall > 0 else 0)

        return {
            'total_blocks'       : self._block_count,
            'budget_exceeded'    : self._budget_exceed,
            'budget_exceed_pct'  : round(self._budget_exceed / n * 100, 2),
            'avg_total_us'       : round(avg_total, 3),
            'total_budget_us'    : TOTAL_BUDGET_US,
            'overall_budget_pct' : round(avg_total / TOTAL_BUDGET_US * 100, 1),
            'throughput_blocks_s': round(throughput, 1),
            'per_stage'          : per_stage,
        }

    def recent_blocks(self, n: int = 10) -> List[Dict]:
        """Return timing records for last N blocks."""
        return [bt.to_dict() for bt in self._history[-n:]]

    def slow_blocks(self, threshold_us: float = 300.0) -> List[Dict]:
        """Return blocks that exceeded the latency threshold."""
        return [bt.to_dict() for bt in self._history
                if bt.total_us > threshold_us]

    def export_csv(self, filepath: str) -> int:
        """
        Export all timing records to CSV file.

        Args:
            filepath: output CSV path

        Returns: number of rows written
        """
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(BlockTimings.csv_header())
            for bt in self._history:
                writer.writerow(bt.csv_row())
        return len(self._history)

    def export_json(self, filepath: str) -> None:
        """Export summary statistics to JSON."""
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(self.stats(), f, indent=2)

    def print_summary(self) -> None:
        """Print formatted pipeline timing summary."""
        s = self.stats()
        print("=" * 65)
        print("  Pipeline Timing Summary")
        print("=" * 65)
        print(f"  Blocks analyzed    : {s['total_blocks']:,}")
        print(f"  Throughput         : {s['throughput_blocks_s']:,.0f} blocks/sec")
        print(f"  Avg total latency  : {s['avg_total_us']:.1f} µs "
              f"(budget: {s['total_budget_us']:.0f} µs → "
              f"{s['overall_budget_pct']:.0f}%)")
        print(f"  Budget exceeded    : {s['budget_exceeded']} blocks "
              f"({s['budget_exceed_pct']:.1f}%)")
        print()
        print(f"  {'Stage':<18} {'Avg µs':>8} {'Min µs':>8} "
              f"{'Max µs':>8} {'Std µs':>8} {'Budget%':>8}")
        print(f"  {'-'*18} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
        for stage, ps in s['per_stage'].items():
            if ps['count'] > 0:
                print(f"  {stage:<18} {ps['avg_us']:>8.2f} "
                      f"{ps['min_us']:>8.2f} {ps['max_us']:>8.2f} "
                      f"{ps['std_us']:>8.2f} {ps['budget_pct']:>7.0f}%")
        print("=" * 65)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_stage(self, stage: str, elapsed_us: float) -> None:
        """Update running statistics for a stage."""
        if stage not in PIPELINE_STAGES:
            return

        self._current[stage]    += elapsed_us
        self._stage_totals[stage] += elapsed_us
        self._stage_counts[stage] += 1
        self._stage_sq[stage]     += elapsed_us * elapsed_us

        if elapsed_us < self._stage_min[stage]:
            self._stage_min[stage] = elapsed_us
        if elapsed_us > self._stage_max[stage]:
            self._stage_max[stage] = elapsed_us

    def __repr__(self) -> str:
        s = self.stats()
        return (f"TimingProfiler(blocks={s['total_blocks']}, "
                f"avg={s['avg_total_us']:.1f}µs, "
                f"tput={s['throughput_blocks_s']:.0f}/s)")


# ---------------------------------------------------------------------------
# Example usage / self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import os
    import random

    print("=" * 60)
    print("  TimingProfiler — Pipeline Profiling Self-Test")
    print("=" * 60)

    profiler = TimingProfiler()

    def fake_stage(min_us: float, max_us: float) -> None:
        """Simulate a pipeline stage taking between min_us and max_us."""
        target = random.uniform(min_us, max_us)
        deadline = time.perf_counter() + target / 1e6
        while time.perf_counter() < deadline:
            pass   # busy wait

    N = 500
    print(f"  Simulating {N} blocks through pipeline...")

    for i in range(N):
        codec = random.choice(['RAW', 'LZ4', 'LZ4HC'])
        hit   = random.random() < 0.6
        ratio = random.uniform(0.2, 1.0)

        with profiler.measure('hashing'):
            fake_stage(0.5, 2.0)

        with profiler.measure('cache_lookup'):
            fake_stage(0.2, 1.5)

        if not hit:
            with profiler.measure('feature_extract'):
                fake_stage(5.0, 20.0)

            with profiler.measure('codec_select'):
                fake_stage(0.3, 1.0)

        with profiler.measure('compression'):
            if codec == 'RAW':
                fake_stage(0.1, 0.5)
            elif codec == 'LZ4':
                fake_stage(20.0, 80.0)
            else:
                fake_stage(60.0, 180.0)

        with profiler.measure('block_packing'):
            fake_stage(1.0, 5.0)

        profiler.set_metadata(codec_used=codec, cache_hit=hit,
                              compression_ratio=ratio)
        profiler.commit()

    # Print summary
    print()
    profiler.print_summary()

    # Test CSV export
    csv_path = '/tmp/timings_test.csv'
    rows = profiler.export_csv(csv_path)
    print(f"\n✓ CSV exported: {rows} rows → {csv_path}")

    # Test JSON export
    json_path = '/tmp/timings_stats.json'
    profiler.export_json(json_path)
    print(f"✓ JSON exported: {json_path}")

    # Recent blocks
    recent = profiler.recent_blocks(3)
    print(f"\n  Last 3 blocks:")
    for r in recent:
        print(f"    Block {r['block_index']:4d}: total={r['total_us']:.1f}µs "
              f"codec={r['codec_used']:6s} hit={r['cache_hit']}")

    print()
    print(profiler)
    print()
    print("  TimingProfiler is ready.")
