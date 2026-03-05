"""
=============================================================================
MODULE: feature_analyzer.py
DESCRIPTION: Entropy and Run-Length Density Analyzer for SSD Firmware

Simulates the pre-compression block analysis stage found in enterprise SSD
controllers. Before selecting a codec, the firmware characterizes each 4KB
block using two key signals:

  1. Shannon Entropy  — measures information density
                        H > 7.5 bits/byte → incompressible (random data)
                        H < 3.0 bits/byte → highly compressible (text, logs)

  2. Run-Length Density (RLD) — measures byte repetition
                        RLD > 0.4 → good candidate for RLE/LZ4
                        RLD → 1.0 → all bytes identical (zero blocks, etc.)

FIRMWARE ANALOGY:
  - On ARM Cortex-R5: entropy computed via 256-bin histogram in SRAM
  - RLD via single-pass scan with run-counter accumulator
  - Both metrics computed in parallel pipeline stages
  - Total analysis budget: ~15 µs per 4KB block @ 400 MHz

DESIGN:
  - Single-pass RLD to minimize memory bandwidth
  - Histogram-based entropy avoids costly per-byte log() in main loop
  - All arithmetic compatible with fixed-point (Q16.16) if ported to C
=============================================================================
"""

import math
import struct
import time
from typing import Tuple, Dict, Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BLOCK_SIZE        = 4096     # Bytes — standard NVMe 4KB LBA block
HISTOGRAM_BINS    = 256      # One bin per byte value (0x00–0xFF)
LOG2_PRECOMPUTED  = [0.0] + [math.log2(i) for i in range(1, BLOCK_SIZE + 1)]

# Entropy thresholds (matches codec policy thresholds)
ENTROPY_INCOMPRESSIBLE = 7.5   # bits/byte — treat as random
ENTROPY_LOW            = 3.0   # bits/byte — highly compressible

# RLD thresholds
RLD_HIGH = 0.4    # >40% of block is run-length redundancy
RLD_LOW  = 0.05   # <5% run-length structure


# ---------------------------------------------------------------------------
# Core Analysis Functions
# ---------------------------------------------------------------------------

def compute_entropy(block_bytes: bytes) -> float:
    """
    Compute Shannon entropy of a 4KB block.

    Algorithm:
      1. Build 256-bin frequency histogram (single pass, O(N))
      2. Normalize to probability distribution
      3. Compute H = -Σ p(x) · log₂(p(x)) for non-zero bins

    Firmware equivalent:
      - SRAM histogram updated per byte via indexed increment
      - Log computation deferred to post-scan phase
      - On Cortex-R5: ~10 µs for 4096 bytes at 400 MHz

    Args:
        block_bytes: exactly 4096 bytes

    Returns:
        Shannon entropy in bits/byte [0.0, 8.0]
        0.0 = all bytes identical (zero entropy)
        8.0 = uniform distribution (maximum entropy = random data)
    """
    n = len(block_bytes)
    if n == 0:
        return 0.0

    # --- Build 256-bin histogram ---
    # bytearray histogram is faster than dict for dense integer keys
    histogram = [0] * HISTOGRAM_BINS
    for byte in block_bytes:
        histogram[byte] += 1

    # --- Compute H = -Σ p(x) log₂(p(x)) ---
    entropy = 0.0
    inv_n   = 1.0 / n

    for count in histogram:
        if count > 0:
            p = count * inv_n
            # Use precomputed log2 table where possible for speed
            # p * log2(p) = (count/n) * (log2(count) - log2(n))
            entropy -= p * math.log2(p)

    return round(entropy, 6)


def compute_run_length_density(block_bytes: bytes) -> float:
    """
    Compute Run-Length Density (RLD) of a 4KB block.

    RLD measures what fraction of the block consists of repeated byte runs.
    A run is defined as 2+ consecutive identical bytes.

    Algorithm (single-pass):
      - Scan block tracking current byte and run counter
      - When run ≥ 2: accumulate (run_length - 1) as "savings"
        (because run-length encoding saves run_length - 1 bytes)
      - RLD = total_run_savings / block_size

    Firmware equivalent:
      - Implemented as SIMD compare-and-count on Cortex-R5
      - ~5 µs for 4096 bytes

    Args:
        block_bytes: exactly 4096 bytes

    Returns:
        RLD in [0.0, 1.0]
        0.0 = no repetition (every byte differs from next)
        1.0 = entire block is single repeated byte
    """
    n = len(block_bytes)
    if n < 2:
        return 0.0

    total_run_savings = 0
    current_byte      = block_bytes[0]
    run_length        = 1

    for i in range(1, n):
        byte = block_bytes[i]
        if byte == current_byte:
            run_length += 1
        else:
            # End of run — accumulate savings for runs ≥ 2
            if run_length >= 2:
                total_run_savings += run_length - 1
            current_byte = byte
            run_length   = 1

    # Handle last run
    if run_length >= 2:
        total_run_savings += run_length - 1

    rld = total_run_savings / n
    return round(rld, 6)


def compute_byte_variety(block_bytes: bytes) -> int:
    """
    Count number of distinct byte values in block.

    Auxiliary feature used for tie-breaking in codec selection.
    Low variety (< 16 distinct bytes) → very compressible.
    High variety (≈ 256 distinct bytes) → random-looking data.

    Firmware: computed as popcount(histogram_nonzero_mask).
    """
    return len(set(block_bytes))


def compute_zero_density(block_bytes: bytes) -> float:
    """
    Fraction of block that is zero bytes.

    Zero blocks are extremely common in SSDs (unwritten LBAs).
    High zero density → deduplicate instead of compress.
    Firmware: detected via SIMD zero-compare in ~1 µs.
    """
    zero_count = block_bytes.count(0x00)
    return round(zero_count / len(block_bytes), 6)


def analyze_block(block_bytes: bytes) -> Dict[str, Any]:
    """
    Full block feature extraction pipeline.

    Runs all analysis stages and returns a feature vector used by
    the codec policy selector.

    Stages (mirrors firmware pipeline):
      Stage A: Histogram construction   (shared by entropy + variety)
      Stage B: Shannon entropy          (post-histogram)
      Stage C: Run-length density       (single-pass scan)
      Stage D: Zero density             (count query)
      Stage E: Byte variety             (histogram non-zero count)

    Args:
        block_bytes: 4096-byte block

    Returns dict:
        entropy          : float [0.0, 8.0]   — Shannon bits/byte
        run_length_density : float [0.0, 1.0] — repetition fraction
        byte_variety     : int   [1, 256]     — distinct byte values
        zero_density     : float [0.0, 1.0]   — fraction of zero bytes
        is_zero_block    : bool               — True if all zeros
        compression_hint : str                — human-readable hint
        analysis_us      : float              — time taken in microseconds
    """
    t_start = time.perf_counter()

    # Compute all features
    entropy   = compute_entropy(block_bytes)
    rld       = compute_run_length_density(block_bytes)
    variety   = compute_byte_variety(block_bytes)
    zero_den  = compute_zero_density(block_bytes)
    is_zero   = (zero_den > 0.9999)

    # Derive compression hint
    if is_zero:
        hint = "ZERO_BLOCK — deduplicate or skip"
    elif entropy > ENTROPY_INCOMPRESSIBLE:
        hint = "INCOMPRESSIBLE — use RAW codec"
    elif rld > RLD_HIGH:
        hint = "HIGH_RLD — use LZ4 (run-length friendly)"
    elif entropy < ENTROPY_LOW:
        hint = "LOW_ENTROPY — use LZ4HC (deep compression)"
    else:
        hint = "MODERATE — use LZ4 (balanced)"

    elapsed_us = (time.perf_counter() - t_start) * 1e6

    return {
        'entropy'            : entropy,
        'run_length_density' : rld,
        'byte_variety'       : variety,
        'zero_density'       : zero_den,
        'is_zero_block'      : is_zero,
        'compression_hint'   : hint,
        'analysis_us'        : round(elapsed_us, 3),
    }


# ---------------------------------------------------------------------------
# FeatureAnalyzer class (stateful, with running averages)
# ---------------------------------------------------------------------------

class FeatureAnalyzer:
    """
    Stateful feature analyzer with running statistics.

    Wraps the pure functions above and maintains block-level history
    for diagnostics and adaptive policy tuning.
    """

    def __init__(self):
        self._block_count = 0
        self._total_entropy = 0.0
        self._total_rld     = 0.0
        self._total_us      = 0.0
        self._zero_blocks   = 0
        self._high_entropy_blocks = 0
        self._high_rld_blocks     = 0

    def analyze(self, block_bytes: bytes) -> Dict[str, Any]:
        """
        Analyze a 4KB block and update running statistics.

        Returns same structure as analyze_block() plus:
            block_index      : int   — sequence number
            avg_entropy      : float — running average entropy
            avg_rld          : float — running average RLD
        """
        result = analyze_block(block_bytes)

        self._block_count      += 1
        self._total_entropy    += result['entropy']
        self._total_rld        += result['run_length_density']
        self._total_us         += result['analysis_us']

        if result['is_zero_block']:
            self._zero_blocks += 1
        if result['entropy'] > ENTROPY_INCOMPRESSIBLE:
            self._high_entropy_blocks += 1
        if result['run_length_density'] > RLD_HIGH:
            self._high_rld_blocks += 1

        result['block_index'] = self._block_count
        result['avg_entropy'] = round(
            self._total_entropy / self._block_count, 4)
        result['avg_rld']     = round(
            self._total_rld / self._block_count, 4)

        return result

    def stats(self) -> Dict[str, Any]:
        """Return aggregate analyzer statistics."""
        n = max(self._block_count, 1)
        return {
            'blocks_analyzed'       : self._block_count,
            'avg_entropy'           : round(self._total_entropy / n, 4),
            'avg_rld'               : round(self._total_rld / n, 4),
            'avg_analysis_us'       : round(self._total_us / n, 3),
            'zero_block_pct'        : round(self._zero_blocks / n * 100, 2),
            'high_entropy_pct'      : round(self._high_entropy_blocks / n * 100, 2),
            'high_rld_pct'          : round(self._high_rld_blocks / n * 100, 2),
            'estimated_throughput'  : (
                round(n / (self._total_us / 1e6), 0)
                if self._total_us > 0 else 0
            ),
        }

    def reset(self):
        """Reset running statistics."""
        self.__init__()

    def __repr__(self) -> str:
        s = self.stats()
        return (f"FeatureAnalyzer(blocks={s['blocks_analyzed']}, "
                f"avg_H={s['avg_entropy']:.3f}, "
                f"avg_RLD={s['avg_rld']:.3f})")


# ---------------------------------------------------------------------------
# Example usage / self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import os

    print("=" * 60)
    print("  FeatureAnalyzer — Block Analysis Self-Test")
    print("=" * 60)

    analyzer = FeatureAnalyzer()

    # --- Test Block 1: Zero block ---
    zero_block = bytes(4096)
    r = analyzer.analyze(zero_block)
    assert r['is_zero_block'], "Should detect zero block"
    assert r['entropy'] == 0.0, f"Zero block entropy should be 0, got {r['entropy']}"
    assert r['run_length_density'] > 0.99, "Zero block should have near-1.0 RLD"
    print(f"✓ Zero Block:       entropy={r['entropy']:.3f}, "
          f"RLD={r['run_length_density']:.3f}, hint='{r['compression_hint']}'")

    # --- Test Block 2: Random (high entropy) ---
    random_block = os.urandom(4096)
    r = analyzer.analyze(random_block)
    assert r['entropy'] > 7.0, f"Random data should have H>7.0, got {r['entropy']}"
    print(f"✓ Random Block:     entropy={r['entropy']:.3f}, "
          f"RLD={r['run_length_density']:.3f}, hint='{r['compression_hint']}'")

    # --- Test Block 3: Repetitive (high RLD) ---
    repetitive_block = (b'\xAB\xAB\xAB\xAB\xAB\xAB\xAB\xAB' +
                        b'\xCD\xCD\xCD\xCD\xCD\xCD\xCD\xCD') * 256
    r = analyzer.analyze(repetitive_block)
    assert r['run_length_density'] > 0.7, \
        f"Repetitive block should have high RLD, got {r['run_length_density']}"
    print(f"✓ Repetitive Block: entropy={r['entropy']:.3f}, "
          f"RLD={r['run_length_density']:.3f}, hint='{r['compression_hint']}'")

    # --- Test Block 4: Structured log data (low entropy) ---
    log_line = b"2024-01-15 12:34:56 INFO  [storage] write_lba=0x0001A2F3 len=4096\n"
    structured_block = (log_line * (4096 // len(log_line) + 1))[:4096]
    r = analyzer.analyze(structured_block)
    assert r['entropy'] < 5.5, \
        f"Structured log should have low entropy, got {r['entropy']}"
    print(f"✓ Structured Log:   entropy={r['entropy']:.3f}, "
          f"RLD={r['run_length_density']:.3f}, hint='{r['compression_hint']}'")

    # --- Test Block 5: Mixed data ---
    mixed_block = os.urandom(2048) + bytes([0x42] * 2048)
    r = analyzer.analyze(mixed_block)
    print(f"✓ Mixed Block:      entropy={r['entropy']:.3f}, "
          f"RLD={r['run_length_density']:.3f}, hint='{r['compression_hint']}'")

    # --- Throughput test ---
    N = 1000
    test_blocks = [os.urandom(4096) for _ in range(N)]
    t0 = time.perf_counter()
    for blk in test_blocks:
        analyze_block(blk)
    elapsed = time.perf_counter() - t0
    tput = N / elapsed

    print(f"\n✓ Throughput:       {tput:,.0f} blocks/sec "
          f"({elapsed * 1000 / N:.2f} ms/block)")

    # --- Final stats ---
    print()
    print("  Analyzer Statistics:")
    for k, v in analyzer.stats().items():
        print(f"    {k:<28}: {v}")
    print()
    print(analyzer)
    print()
    print("  All tests PASSED. FeatureAnalyzer is ready.")
