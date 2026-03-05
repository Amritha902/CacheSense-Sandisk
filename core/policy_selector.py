"""
=============================================================================
MODULE: policy_selector.py
DESCRIPTION: Deterministic Compression Codec Policy Selector

Implements the firmware decision tree that maps block analysis features
(entropy + RLD) to a codec selection. This is the core "intelligence" of
the compression engine — analogous to the policy engine in SanDisk/WD
enterprise SSD firmware.

DECISION TREE:
  ┌─────────────────────────────────────────────────────────┐
  │                     INCOMING BLOCK                       │
  └──────────────────────────┬──────────────────────────────┘
                             │
              ┌──────────────▼──────────────┐
              │   Is it a zero block?        │
              │   (zero_density > 0.999)      │
              └──┬───────────────────────────┘
                 │ YES                   NO
                 ▼                        │
           [SKIP/ZERO]         ┌──────────▼──────────┐
                               │  entropy > 7.5?      │
                               └──┬──────────────────┘
                                  │ YES           NO
                                  ▼               │
                               [RAW]   ┌──────────▼──────────┐
                                       │  rld > 0.4?          │
                                       └──┬──────────────────┘
                                          │ YES           NO
                                          ▼               ▼
                                        [LZ4]          [LZ4HC]

FIRMWARE ANALOGY:
  - This logic runs in ~2 µs on Cortex-R5 (pure integer comparisons)
  - Thresholds are stored in firmware config flash (tunable)
  - Decision is cached in PatternCache for block signature reuse
=============================================================================
"""

import time
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Codec Definitions
# ---------------------------------------------------------------------------

CODEC_SKIP  = -1   # Zero block — no compression needed
CODEC_RAW   = 0    # Store uncompressed (random/encrypted data)
CODEC_LZ4   = 1    # LZ4 fast compression (balanced)
CODEC_LZ4HC = 2    # LZ4HC high-compression (structured data)

CODEC_NAMES = {
    CODEC_SKIP  : 'SKIP',
    CODEC_RAW   : 'RAW',
    CODEC_LZ4   : 'LZ4',
    CODEC_LZ4HC : 'LZ4HC',
}

CODEC_IDS = {v: k for k, v in CODEC_NAMES.items()}

# ---------------------------------------------------------------------------
# Policy Thresholds (firmware config — tunable)
# ---------------------------------------------------------------------------

class PolicyThresholds:
    """
    Firmware-style policy threshold configuration.
    In real firmware these are stored in a config partition and can be
    updated via vendor-specific NVMe commands.
    """

    # Entropy thresholds (bits/byte)
    ENTROPY_INCOMPRESSIBLE = 7.5   # Above → RAW (compression won't help)
    ENTROPY_VERY_LOW       = 2.0   # Below → LZ4HC always worthwhile

    # Run-length density thresholds
    RLD_HIGH         = 0.40   # Above → LZ4 preferred (run-length patterns)
    RLD_MODERATE     = 0.10   # Above → LZ4 adequate

    # Zero block threshold
    ZERO_DENSITY_THRESHOLD = 0.999   # >99.9% zeros → skip compression

    # Compression benefit threshold
    MIN_RATIO_BENEFIT = 0.90   # Only use compressed if ratio < 0.90


# ---------------------------------------------------------------------------
# Decision Result
# ---------------------------------------------------------------------------

@dataclass
class CodecDecision:
    """
    Immutable codec decision record.

    Mirrors the firmware decision_record_t struct written to the
    pattern cache after each codec selection.
    """
    codec_id     : int
    codec_name   : str
    decision_reason : str
    entropy      : float
    rld          : float
    zero_density : float
    confidence   : float          # 0.0–1.0, how certain the decision is
    decision_us  : float = 0.0    # Time to reach decision (microseconds)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'codec_id'         : self.codec_id,
            'codec_name'       : self.codec_name,
            'decision_reason'  : self.decision_reason,
            'entropy'          : round(self.entropy, 4),
            'rld'              : round(self.rld, 4),
            'zero_density'     : round(self.zero_density, 4),
            'confidence'       : round(self.confidence, 4),
            'decision_us'      : round(self.decision_us, 3),
        }

    def __repr__(self) -> str:
        return (f"CodecDecision(codec={self.codec_name}, "
                f"H={self.entropy:.3f}, RLD={self.rld:.3f}, "
                f"reason='{self.decision_reason}', "
                f"conf={self.confidence:.0%})")


# ---------------------------------------------------------------------------
# Policy Selector
# ---------------------------------------------------------------------------

class PolicySelector:
    """
    Deterministic compression policy selector.

    Implements the codec selection decision tree described in the
    SSD compression engine design document.

    Usage:
        selector = PolicySelector()
        decision = selector.select_codec(entropy=4.2, rld=0.15)
        print(decision.codec_name)   # → 'LZ4HC'
    """

    def __init__(self,
                 thresholds: Optional[PolicyThresholds] = None,
                 enable_logging: bool = False):
        self._thresh = thresholds or PolicyThresholds()
        self._logger = logging.getLogger('PolicySelector')
        if enable_logging:
            logging.basicConfig(
                level=logging.DEBUG,
                format='[%(name)s] %(message)s'
            )

        # Decision counters (firmware perf registers)
        self._decision_counts = {
            CODEC_SKIP  : 0,
            CODEC_RAW   : 0,
            CODEC_LZ4   : 0,
            CODEC_LZ4HC : 0,
        }
        self._total_decisions = 0
        self._total_us = 0.0

    def select_codec(self,
                     entropy     : float,
                     rld         : float,
                     zero_density: float = 0.0) -> CodecDecision:
        """
        Select compression codec based on block features.

        Implements the deterministic decision tree from firmware spec.
        Runs in O(1) — pure threshold comparisons, no ML inference.

        Args:
            entropy      : Shannon entropy in bits/byte [0.0, 8.0]
            rld          : Run-length density [0.0, 1.0]
            zero_density : Fraction of zero bytes [0.0, 1.0]

        Returns:
            CodecDecision with selected codec and reasoning
        """
        t_start = time.perf_counter()
        t = self._thresh

        # ----------------------------------------------------------------
        # Stage 0: Zero block detection (fastest check, most common case)
        # ----------------------------------------------------------------
        if zero_density >= t.ZERO_DENSITY_THRESHOLD:
            decision = CodecDecision(
                codec_id       = CODEC_SKIP,
                codec_name     = 'SKIP',
                decision_reason= (f"Zero block detected "
                                  f"(zero_density={zero_density:.3f} ≥ "
                                  f"{t.ZERO_DENSITY_THRESHOLD})"),
                entropy        = entropy,
                rld            = rld,
                zero_density   = zero_density,
                confidence     = 1.0,
            )
            return self._finalize(decision, t_start)

        # ----------------------------------------------------------------
        # Stage 1: Entropy gate — incompressible data check
        # ----------------------------------------------------------------
        if entropy > t.ENTROPY_INCOMPRESSIBLE:
            # High entropy → compression overhead not worth it
            # Confidence scales with how far above threshold we are
            conf = min(1.0, (entropy - t.ENTROPY_INCOMPRESSIBLE) / 0.5 + 0.7)
            decision = CodecDecision(
                codec_id       = CODEC_RAW,
                codec_name     = 'RAW',
                decision_reason= (f"High entropy: {entropy:.3f} > "
                                  f"{t.ENTROPY_INCOMPRESSIBLE} "
                                  f"(random/encrypted data — skip compression)"),
                entropy        = entropy,
                rld            = rld,
                zero_density   = zero_density,
                confidence     = min(conf, 1.0),
            )
            return self._finalize(decision, t_start)

        # ----------------------------------------------------------------
        # Stage 2: Run-length density gate — LZ4 vs LZ4HC
        # ----------------------------------------------------------------
        if rld > t.RLD_HIGH:
            # High run-length density → LZ4 fast compression handles this well
            # LZ4HC would give marginally better ratio but 3-5x slower
            conf = min(1.0, (rld - t.RLD_HIGH) / 0.2 + 0.75)
            decision = CodecDecision(
                codec_id       = CODEC_LZ4,
                codec_name     = 'LZ4',
                decision_reason= (f"High RLD: {rld:.3f} > {t.RLD_HIGH} "
                                  f"(run-length patterns → LZ4 efficient)"),
                entropy        = entropy,
                rld            = rld,
                zero_density   = zero_density,
                confidence     = min(conf, 1.0),
            )
            return self._finalize(decision, t_start)

        # ----------------------------------------------------------------
        # Stage 3: Default — LZ4HC for structured/compressible data
        # ----------------------------------------------------------------
        # We reach here when:
        #   - Not a zero block
        #   - entropy ≤ 7.5  (some compressibility)
        #   - rld ≤ 0.4      (not dominated by run-length patterns)
        # → structured data, logs, text: LZ4HC gives best ratio

        # Confidence based on how low the entropy is
        conf_from_entropy = max(0.5, (t.ENTROPY_INCOMPRESSIBLE - entropy) /
                               t.ENTROPY_INCOMPRESSIBLE)
        decision = CodecDecision(
            codec_id       = CODEC_LZ4HC,
            codec_name     = 'LZ4HC',
            decision_reason= (f"Moderate entropy: {entropy:.3f} ≤ "
                              f"{t.ENTROPY_INCOMPRESSIBLE}, "
                              f"RLD={rld:.3f} ≤ {t.RLD_HIGH} "
                              f"(structured data → LZ4HC deep compression)"),
            entropy        = entropy,
            rld            = rld,
            zero_density   = zero_density,
            confidence     = round(conf_from_entropy, 4),
        )
        return self._finalize(decision, t_start)

    def select_codec_from_features(self,
                                   features: Dict[str, Any]) -> CodecDecision:
        """
        Convenience wrapper: accepts analyze_block() output dict directly.

        Args:
            features: dict from FeatureAnalyzer.analyze() or analyze_block()

        Returns:
            CodecDecision
        """
        return self.select_codec(
            entropy      = features.get('entropy', 4.0),
            rld          = features.get('run_length_density', 0.0),
            zero_density = features.get('zero_density', 0.0),
        )

    def _finalize(self, decision: CodecDecision,
                  t_start: float) -> CodecDecision:
        """Record decision timing, update counters, log result."""
        elapsed_us = (time.perf_counter() - t_start) * 1e6
        decision.decision_us = round(elapsed_us, 3)

        self._decision_counts[decision.codec_id] = (
            self._decision_counts.get(decision.codec_id, 0) + 1
        )
        self._total_decisions += 1
        self._total_us += elapsed_us

        self._logger.debug(
            f"Block #{self._total_decisions:06d} → {decision.codec_name:6s} | "
            f"H={decision.entropy:.3f} RLD={decision.rld:.3f} "
            f"conf={decision.confidence:.0%} | {decision.decision_reason}"
        )

        return decision

    def stats(self) -> Dict[str, Any]:
        """
        Return policy selector statistics.

        Useful for understanding workload composition and tuning thresholds.
        """
        n = max(self._total_decisions, 1)
        dist = {
            CODEC_NAMES[k]: round(v / n * 100, 2)
            for k, v in self._decision_counts.items()
        }
        return {
            'total_decisions'  : self._total_decisions,
            'codec_distribution_pct': dist,
            'avg_decision_us'  : round(self._total_us / n, 3),
            'decisions_per_sec': round(n / (self._total_us / 1e6), 0)
                                  if self._total_us > 0 else 0,
            'thresholds': {
                'entropy_incompressible': PolicyThresholds.ENTROPY_INCOMPRESSIBLE,
                'rld_high'              : PolicyThresholds.RLD_HIGH,
                'zero_density'          : PolicyThresholds.ZERO_DENSITY_THRESHOLD,
            }
        }

    def __repr__(self) -> str:
        s = self.stats()
        return (f"PolicySelector(decisions={s['total_decisions']}, "
                f"dist={s['codec_distribution_pct']})")


# ---------------------------------------------------------------------------
# Example usage / self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print("=" * 60)
    print("  PolicySelector — Codec Decision Self-Test")
    print("=" * 60)

    selector = PolicySelector()

    # Define test cases with expected outcomes
    test_cases = [
        # (entropy, rld, zero_density, expected_codec, description)
        (0.0,  1.0,  1.0,  'SKIP',   "Zero block (all zeros)"),
        (7.9,  0.01, 0.0,  'RAW',    "Random/encrypted data"),
        (7.6,  0.05, 0.0,  'RAW',    "Near-random data"),
        (4.2,  0.55, 0.0,  'LZ4',    "Repetitive structured data"),
        (2.1,  0.65, 0.0,  'LZ4',    "Highly repetitive (runs dominate)"),
        (3.5,  0.15, 0.0,  'LZ4HC',  "Log file / text data"),
        (5.0,  0.08, 0.0,  'LZ4HC',  "Database pages"),
        (1.2,  0.05, 0.0,  'LZ4HC',  "Very structured data"),
        (6.8,  0.02, 0.0,  'LZ4HC',  "Moderate entropy structured"),
        (0.01, 0.99, 0.97, 'SKIP',   "Almost-zero block"),
    ]

    all_pass = True
    for entropy, rld, zero_den, expected, desc in test_cases:
        decision = selector.select_codec(entropy, rld, zero_den)
        status = "✓" if decision.codec_name == expected else "✗"
        if decision.codec_name != expected:
            all_pass = False
        print(f"  {status} [{decision.codec_name:6s}] expected={expected:6s} | "
              f"H={entropy:.1f} RLD={rld:.2f} | {desc}")
        if decision.codec_name != expected:
            print(f"    ↳ MISMATCH! reason: {decision.decision_reason}")

    print()
    if all_pass:
        print("✓ All decision tests PASSED.")
    else:
        print("✗ Some tests FAILED — check thresholds.")

    # --- Throughput test ---
    import time
    N = 100_000
    t0 = time.perf_counter()
    for i in range(N):
        selector.select_codec(
            entropy      = 3.0 + (i % 50) * 0.1,
            rld          = 0.05 + (i % 10) * 0.04,
            zero_density = 0.0,
        )
    elapsed = time.perf_counter() - t0
    print(f"✓ Throughput: {N/elapsed:,.0f} decisions/sec "
          f"({elapsed * 1e6 / N:.2f} µs/decision)")

    # --- Stats ---
    print()
    print("  Policy Statistics:")
    for k, v in selector.stats().items():
        print(f"    {k:<30}: {v}")
    print()
    print(selector)
    print()
    print("  PolicySelector is ready.")
