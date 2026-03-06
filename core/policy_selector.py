"""
=============================================================================
MODULE: policy_selector.py
DESCRIPTION: Deterministic Compression Codec Policy Selector  v2

Implements the firmware decision tree that maps block analysis features
(entropy + RLD + uniqueness) to a codec selection. This is the core
"intelligence" of the compression engine — analogous to the policy engine
in SanDisk/WD enterprise SSD firmware.

WHAT CHANGED FROM v1:
  - Added uniqueness dimension to the decision tree
  - Tightened RAW threshold: entropy > 7.6 AND uniqueness > 0.85
    (prevents wasting RAW on high-entropy-but-structured data)
  - Added LZ4HC fast-path for low uniqueness even at moderate entropy
  - Lowered RLD threshold: 0.40 → 0.05 to catch partially repetitive blocks
  - Added entropy < 6.5 fallback to LZ4HC (catches moderate-structured data
    that the old tree left as LZ4 unnecessarily)
  - Confidence computation now incorporates uniqueness signal

IMPROVED DECISION TREE:
  ┌─────────────────────────────────────────────────────────┐
  │                     INCOMING BLOCK                       │
  └──────────────────────────┬──────────────────────────────┘
                             │
              ┌──────────────▼──────────────┐
              │   Is it a zero block?        │
              │   (zero_density > 0.999)     │
              └──┬───────────────────────────┘
                 │ YES                   NO
                 ▼                        │
           [SKIP/ZERO]    ┌───────────────▼───────────────┐
                          │  entropy > 7.6                 │
                          │  AND uniqueness > 0.85?        │  ← TIGHTENED [NEW]
                          └──┬────────────────────────────┘
                             │ YES               NO
                             ▼                    │
                          [RAW]    ┌──────────────▼──────────────┐
                                   │  rld > 0.05?                │  ← LOWERED [NEW]
                                   └──┬─────────────────────────┘
                                      │ YES               NO
                                      ▼                    │
                                   [LZ4]   ┌───────────────▼───────────────┐
                                           │  entropy < 6.5                 │
                                           │  OR uniqueness < 0.30?         │  ← NEW
                                           └──┬────────────────────────────┘
                                              │ YES               NO
                                              ▼                    ▼
                                          [LZ4HC]              [LZ4HC]
                                          (deep)               (default)

NOTE: The final two branches both go to LZ4HC — LZ4HC is now the safe default
for any block that clears the RAW and LZ4 gates. This matches real-world
SSD firmware behavior where LZ4 is reserved for specifically run-dominated data.

FIRMWARE ANALOGY:
  - This logic runs in ~2 µs on Cortex-R5 (pure integer comparisons)
  - Thresholds stored in firmware config flash (tunable per workload profile)
  - Decision cached in PatternCache for block signature reuse
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
CODEC_LZ4   = 1    # LZ4 fast compression (run-dominated data)
CODEC_LZ4HC = 2    # LZ4HC high-compression (structured / default)

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

    v2 changes:
      - ENTROPY_INCOMPRESSIBLE: 7.5 → 7.6  (tighter RAW gate)
      - UNIQUENESS_RANDOM: NEW             (second condition for RAW)
      - UNIQUENESS_LOW: NEW                (fast-path to LZ4HC)
      - RLD_HIGH: 0.40 → 0.05             (catch partially-repetitive blocks)
      - ENTROPY_LZ4HC_FALLBACK: NEW        (moderate entropy → LZ4HC)
    """

    # Entropy thresholds (bits/byte)
    ENTROPY_INCOMPRESSIBLE  = 7.6   # Tightened from 7.5 [CHANGED]
    ENTROPY_VERY_LOW        = 2.0   # Below → LZ4HC always worthwhile
    ENTROPY_LZ4HC_FALLBACK  = 6.5   # Below this at any RLD → LZ4HC [NEW]

    # Uniqueness thresholds (distinct_bytes / 256)
    UNIQUENESS_RANDOM       = 0.85  # Above + high entropy → RAW [NEW]
    UNIQUENESS_LOW          = 0.30  # Below → LZ4HC fast-path [NEW]

    # Run-length density thresholds
    RLD_HIGH                = 0.05  # Lowered from 0.40 — catches partial runs [CHANGED]

    # Zero block threshold
    ZERO_DENSITY_THRESHOLD  = 0.999   # >99.9% zeros → skip compression

    # Compression benefit threshold
    MIN_RATIO_BENEFIT       = 0.90    # Only use compressed if ratio < 0.90


# ---------------------------------------------------------------------------
# Decision Result
# ---------------------------------------------------------------------------

@dataclass
class CodecDecision:
    """
    Immutable codec decision record.

    Mirrors the firmware decision_record_t struct written to the
    pattern cache after each codec selection.

    v2: added uniqueness field.
    """
    codec_id        : int
    codec_name      : str
    decision_reason : str
    entropy         : float
    rld             : float
    uniqueness      : float           # [NEW]
    zero_density    : float
    confidence      : float           # 0.0–1.0, how certain the decision is
    decision_us     : float = 0.0     # Time to reach decision (microseconds)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'codec_id'        : self.codec_id,
            'codec_name'      : self.codec_name,
            'decision_reason' : self.decision_reason,
            'entropy'         : round(self.entropy, 4),
            'rld'             : round(self.rld, 4),
            'uniqueness'      : round(self.uniqueness, 4),
            'zero_density'    : round(self.zero_density, 4),
            'confidence'      : round(self.confidence, 4),
            'decision_us'     : round(self.decision_us, 3),
        }

    def __repr__(self) -> str:
        return (f"CodecDecision(codec={self.codec_name}, "
                f"H={self.entropy:.3f}, RLD={self.rld:.3f}, "
                f"uniq={self.uniqueness:.3f}, "
                f"reason='{self.decision_reason}', "
                f"conf={self.confidence:.0%})")


# ---------------------------------------------------------------------------
# Policy Selector
# ---------------------------------------------------------------------------

class PolicySelector:
    """
    Deterministic compression policy selector  v2.

    Now uses entropy + RLD + uniqueness for sharper codec decisions.
    Reduces false RAW classifications on structured-but-complex data.
    Increases LZ4HC usage on partially-repetitive blocks.

    Usage:
        selector = PolicySelector()
        decision = selector.select_codec(entropy=4.2, rld=0.15, uniqueness=0.45)
        print(decision.codec_name)   # → 'LZ4HC'
    """

    def __init__(self,
                 thresholds    : Optional[PolicyThresholds] = None,
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
        self._total_us        = 0.0

    def select_codec(self,
                     entropy     : float,
                     rld         : float,
                     zero_density: float = 0.0,
                     uniqueness  : float = 0.5) -> 'CodecDecision':
        """
        Select compression codec based on block features.

        Implements the improved deterministic decision tree.
        Runs in O(1) — pure threshold comparisons, no ML inference.

        Args:
            entropy      : Shannon entropy in bits/byte [0.0, 8.0]
            rld          : Run-length density [0.0, 1.0]
            zero_density : Fraction of zero bytes [0.0, 1.0]
            uniqueness   : Distinct byte values / 256 [0.004, 1.0]  [NEW]

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
                uniqueness     = uniqueness,
                zero_density   = zero_density,
                confidence     = 1.0,
            )
            return self._finalize(decision, t_start)

        # ----------------------------------------------------------------
        # Stage 1: Entropy + Uniqueness gate — true incompressible check
        #
        # v2 CHANGE: require BOTH high entropy AND high uniqueness for RAW.
        # Rationale: genuine random/encrypted data has both.
        # Structured-but-complex data (compressed images, etc.) often has
        # high entropy but only moderate uniqueness → try LZ4 anyway.
        # ----------------------------------------------------------------
        if entropy > t.ENTROPY_INCOMPRESSIBLE and uniqueness > t.UNIQUENESS_RANDOM:
            conf = min(1.0,
                       0.5 * (entropy - t.ENTROPY_INCOMPRESSIBLE) / 0.4 +
                       0.5 * (uniqueness - t.UNIQUENESS_RANDOM) / 0.15 +
                       0.6)
            decision = CodecDecision(
                codec_id       = CODEC_RAW,
                codec_name     = 'RAW',
                decision_reason= (f"Truly random/encrypted: "
                                  f"entropy={entropy:.3f} > {t.ENTROPY_INCOMPRESSIBLE} "
                                  f"AND uniqueness={uniqueness:.3f} > {t.UNIQUENESS_RANDOM}"),
                entropy        = entropy,
                rld            = rld,
                uniqueness     = uniqueness,
                zero_density   = zero_density,
                confidence     = round(min(conf, 1.0), 4),
            )
            return self._finalize(decision, t_start)

        # ----------------------------------------------------------------
        # Stage 2: Run-length density gate
        #
        # v2 CHANGE: threshold lowered from 0.40 → 0.05.
        # Even modest run-length structure (5%+ savings) makes LZ4 efficient.
        # LZ4HC would give marginally better ratio here but 3-5× slower encode.
        # ----------------------------------------------------------------
        if rld > t.RLD_HIGH:
            conf = min(1.0, 0.6 + rld * 0.5)
            decision = CodecDecision(
                codec_id       = CODEC_LZ4,
                codec_name     = 'LZ4',
                decision_reason= (f"Run-length structure: rld={rld:.3f} > {t.RLD_HIGH} "
                                  f"(LZ4 handles runs efficiently)"),
                entropy        = entropy,
                rld            = rld,
                uniqueness     = uniqueness,
                zero_density   = zero_density,
                confidence     = round(min(conf, 1.0), 4),
            )
            return self._finalize(decision, t_start)

        # ----------------------------------------------------------------
        # Stage 3: LZ4HC — low entropy or low uniqueness fast-path
        #
        # v2 CHANGE: entropy < 6.5 OR uniqueness < 0.30 → LZ4HC.
        # Old tree defaulted to LZ4HC only after all other gates.
        # Now we actively route structured + moderate-entropy blocks here.
        # ----------------------------------------------------------------
        if entropy < t.ENTROPY_LZ4HC_FALLBACK or uniqueness < t.UNIQUENESS_LOW:
            # Higher confidence when both signals agree
            conf_h = max(0.0, (t.ENTROPY_LZ4HC_FALLBACK - entropy) / t.ENTROPY_LZ4HC_FALLBACK)
            conf_u = max(0.0, (t.UNIQUENESS_LOW - uniqueness) / t.UNIQUENESS_LOW) if uniqueness < t.UNIQUENESS_LOW else 0.0
            conf   = round(min(1.0, 0.55 + 0.3 * conf_h + 0.3 * conf_u), 4)
            reason_parts = []
            if entropy < t.ENTROPY_LZ4HC_FALLBACK:
                reason_parts.append(f"entropy={entropy:.3f} < {t.ENTROPY_LZ4HC_FALLBACK}")
            if uniqueness < t.UNIQUENESS_LOW:
                reason_parts.append(f"uniqueness={uniqueness:.3f} < {t.UNIQUENESS_LOW}")
            decision = CodecDecision(
                codec_id       = CODEC_LZ4HC,
                codec_name     = 'LZ4HC',
                decision_reason= (f"LZ4HC fast-path: {' AND '.join(reason_parts)} "
                                  f"(structured/low-diversity data → deep compression)"),
                entropy        = entropy,
                rld            = rld,
                uniqueness     = uniqueness,
                zero_density   = zero_density,
                confidence     = conf,
            )
            return self._finalize(decision, t_start)

        # ----------------------------------------------------------------
        # Stage 4: Default — LZ4HC for everything else
        #
        # We reach here: not zero, not random, not run-dominated,
        # entropy moderate (6.5–7.6), uniqueness moderate (0.30–0.85).
        # These are structured pages (DB, filesystem) that compress well
        # with LZ4HC's deeper search.
        # ----------------------------------------------------------------
        conf_from_entropy = max(0.5,
                                (t.ENTROPY_INCOMPRESSIBLE - entropy) /
                                t.ENTROPY_INCOMPRESSIBLE)
        decision = CodecDecision(
            codec_id       = CODEC_LZ4HC,
            codec_name     = 'LZ4HC',
            decision_reason= (f"Default structured path: "
                              f"entropy={entropy:.3f}, rld={rld:.3f}, "
                              f"uniqueness={uniqueness:.3f} "
                              f"(moderate data → LZ4HC deep compression)"),
            entropy        = entropy,
            rld            = rld,
            uniqueness     = uniqueness,
            zero_density   = zero_density,
            confidence     = round(conf_from_entropy, 4),
        )
        return self._finalize(decision, t_start)

    def select_codec_from_features(self,
                                   features: Dict[str, Any]) -> 'CodecDecision':
        """
        Convenience wrapper: accepts analyze_block() output dict directly.

        v2: now passes uniqueness through to the decision tree.

        Args:
            features: dict from FeatureAnalyzer.analyze() or analyze_block()

        Returns:
            CodecDecision
        """
        return self.select_codec(
            entropy      = features.get('entropy', 4.0),
            rld          = features.get('run_length_density', 0.0),
            zero_density = features.get('zero_density', 0.0),
            uniqueness   = features.get('uniqueness', 0.5),   # [NEW]
        )

    def _finalize(self, decision: 'CodecDecision',
                  t_start: float) -> 'CodecDecision':
        """Record decision timing, update counters, log result."""
        elapsed_us = (time.perf_counter() - t_start) * 1e6
        decision.decision_us = round(elapsed_us, 3)

        self._decision_counts[decision.codec_id] = (
            self._decision_counts.get(decision.codec_id, 0) + 1
        )
        self._total_decisions += 1
        self._total_us        += elapsed_us

        self._logger.debug(
            f"Block #{self._total_decisions:06d} → {decision.codec_name:6s} | "
            f"H={decision.entropy:.3f} RLD={decision.rld:.3f} "
            f"uniq={decision.uniqueness:.3f} "
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
            'total_decisions'       : self._total_decisions,
            'codec_distribution_pct': dist,
            'avg_decision_us'       : round(self._total_us / n, 3),
            'decisions_per_sec'     : round(n / (self._total_us / 1e6), 0)
                                      if self._total_us > 0 else 0,
            'thresholds': {
                'entropy_incompressible': PolicyThresholds.ENTROPY_INCOMPRESSIBLE,
                'uniqueness_random'     : PolicyThresholds.UNIQUENESS_RANDOM,
                'rld_high'              : PolicyThresholds.RLD_HIGH,
                'entropy_lz4hc_fallback': PolicyThresholds.ENTROPY_LZ4HC_FALLBACK,
                'uniqueness_low'        : PolicyThresholds.UNIQUENESS_LOW,
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
    print("=" * 65)
    print("  PolicySelector v2 — Codec Decision Self-Test")
    print("=" * 65)

    selector = PolicySelector()

    # Test cases: (entropy, rld, zero_density, uniqueness, expected_codec, description)
    test_cases = [
        # zero blocks
        (0.0,  1.0,  1.0,  0.004, 'SKIP',   "Zero block (all zeros)"),
        (0.01, 0.99, 0.999, 0.004, 'SKIP',   "Almost-zero block (zero_density ≥ 0.999)"),

        # genuine random / encrypted → RAW (high entropy + high uniqueness)
        (7.9,  0.01, 0.0,  0.98,  'RAW',    "Random/encrypted data"),
        (7.7,  0.02, 0.0,  0.92,  'RAW',    "Near-random, high uniqueness"),

        # high entropy but low uniqueness → NOT RAW → LZ4 or LZ4HC
        (7.8,  0.01, 0.0,  0.60,  'LZ4HC',  "High H, moderate uniqueness → try compress"),
        (7.6,  0.00, 0.0,  0.40,  'LZ4HC',  "High H, low uniqueness → compress"),

        # run-length dominated → LZ4
        (4.2,  0.55, 0.0,  0.25,  'LZ4',    "Repetitive structured data"),
        (2.1,  0.65, 0.0,  0.10,  'LZ4',    "Highly repetitive (runs dominate)"),
        (5.0,  0.10, 0.0,  0.35,  'LZ4',    "Moderate runs"),

        # low entropy or low uniqueness → LZ4HC fast-path
        (3.5,  0.03, 0.0,  0.45,  'LZ4HC',  "Log file / text (low entropy)"),
        (1.2,  0.02, 0.0,  0.15,  'LZ4HC',  "Very structured data (low uniq)"),
        (5.5,  0.02, 0.0,  0.20,  'LZ4HC',  "Low uniqueness DB page"),

        # moderate → LZ4HC default
        (6.0,  0.03, 0.0,  0.55,  'LZ4HC',  "Moderate entropy structured"),
        (6.8,  0.04, 0.0,  0.65,  'LZ4HC',  "High-moderate entropy, moderate uniq"),
    ]

    all_pass = True
    for entropy, rld, zero_den, uniq, expected, desc in test_cases:
        decision = selector.select_codec(entropy, rld, zero_den, uniq)
        status = "✓" if decision.codec_name == expected else "✗"
        if decision.codec_name != expected:
            all_pass = False
        print(f"  {status} [{decision.codec_name:6s}] exp={expected:6s} | "
              f"H={entropy:.1f} RLD={rld:.2f} uniq={uniq:.2f} | {desc}")
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
            rld          = 0.02 + (i % 10) * 0.04,
            zero_density = 0.0,
            uniqueness   = 0.1 + (i % 8) * 0.1,
        )
    elapsed = time.perf_counter() - t0
    print(f"✓ Throughput: {N/elapsed:,.0f} decisions/sec "
          f"({elapsed * 1e6 / N:.2f} µs/decision)")

    # --- Stats ---
    print()
    print("  Policy Statistics:")
    for k, v in selector.stats().items():
        print(f"    {k:<32}: {v}")
    print()
    print(selector)
    print()
    print("  PolicySelector v2 is ready.")
