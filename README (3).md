# SSD Firmware Compression Engine
## FUSE-Based Host Simulation — SanDisk Hackathon Prototype

A complete, production-quality simulation of the compression pipeline found
in modern NVMe SSD firmware (ARM Cortex-R5 / RISC-V class controllers).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    WRITE PATH PIPELINE                        │
├──────────────────────────────────────────────────────────────┤
│  [4KB block]                                                  │
│       ↓                                                       │
│  1. MurmurHash3 (64-bit signature)                           │
│       ↓                                                       │
│  2. PatternCache lookup  ──HIT──→ use cached codec           │
│       ↓ MISS                                                  │
│  3. FeatureAnalyzer: Shannon entropy + Run-Length Density    │
│       ↓                                                       │
│  4. PolicySelector: RAW / LZ4 / LZ4HC decision              │
│       ↓                                                       │
│  5. CompressionEngine: pure-Python LZ4 / LZ4HC              │
│       ↓                                                       │
│  6. BlockPacker: 4096-byte LBA frame + CRC16                 │
│       ↓                                                       │
│  7. blocks.bin (binary output)                               │
│       ↓                                                       │
│  8. PatternCache insert + TimingProfiler commit              │
└──────────────────────────────────────────────────────────────┘
```

---

## Modules

| Module | Description |
|--------|-------------|
| `pattern_cache.py` | Firmware-style LRU cache with MurmurHash3 (pure Python) |
| `feature_analyzer.py` | Shannon entropy + Run-Length Density analysis |
| `policy_selector.py` | Deterministic codec selection decision tree |
| `compression_engine.py` | LZ4 / LZ4HC / RAW codec (pure Python, no dependencies) |
| `block_packer.py` | 4096-byte LBA frame format + CRC16-CCITT |
| `timing_profiler.py` | Per-stage latency profiler + CSV export |
| `firmware_pipeline.py` | Full pipeline orchestrator |
| `benchmark_runner.py` | Workload benchmark driver |
| `dashboard.py` | HTML monitoring dashboard (Flask or static) |

---

## LBA Frame Format

```
┌─────────────────────────────────────────┐  Offset
│ codec_id         (uint8)                │   0
│ original_size    (uint16 LE)            │   1
│ compressed_size  (uint16 LE)            │   3
│ flags            (uint8)                │   5
│ reserved         (uint32 LE)            │   6
├─────────────────────────────────────────┤  10
│ compressed payload                      │
│ (up to 4084 bytes)                      │
├─────────────────────────────────────────┤  10 + compressed_size
│ zero padding                            │
├─────────────────────────────────────────┤  4094
│ CRC16-CCITT checksum (uint16 LE)        │
└─────────────────────────────────────────┘  4096 (always exactly)
```

---

## Codec Policy Decision Tree

```
Is zero_density ≥ 0.999? → SKIP (zero block, no IO needed)
Is entropy > 7.5?        → RAW  (random/encrypted data)
Is RLD > 0.4?            → LZ4  (run-length patterns)
Otherwise                → LZ4HC (structured/log data)
```

---

## Quick Start

```bash
# Run the full pipeline
python3 modules/firmware_pipeline.py

# Run benchmark
python3 modules/benchmark_runner.py

# Generate dashboard
python3 modules/dashboard.py --static

# Run all module self-tests
python3 modules/pattern_cache.py
python3 modules/feature_analyzer.py
python3 modules/policy_selector.py
python3 modules/compression_engine.py
python3 modules/block_packer.py
python3 modules/timing_profiler.py
```

---

## Output Files

| File | Description |
|------|-------------|
| `output/blocks.bin` | Packed LBA frames (4096 bytes each) |
| `output/metrics.json` | Pipeline performance metrics |
| `output/timings.csv` | Per-block stage latency records |
| `output/dashboard.html` | Interactive monitoring dashboard |
| `output/<run>_metrics.csv` | Benchmark results CSV |
| `output/<run>_full.json` | Full benchmark JSON |

---

## Dependencies

**Zero external dependencies.** All modules use Python 3.8+ stdlib only:
- `struct`, `math`, `time`, `os`, `csv`, `json`, `collections`, `contextlib`

MurmurHash3, LZ4, LZ4HC, and CRC16 are all implemented from scratch.

---

## Firmware Correspondence

| This Module | Firmware Equivalent |
|-------------|-------------------|
| `murmurhash3_x64_128()` | HW hash engine (CRC32C or multiply-shift) |
| `PatternCache` | SRAM-resident codec cache (16 KB–256 KB) |
| `FeatureAnalyzer` | DSP block: histogram + run-length counter |
| `PolicySelector` | Threshold comparator (2 µs budget) |
| `CompressionEngine` | HW LZ4 accelerator / software LZ4HC |
| `BlockPacker` | DMA descriptor builder + ECC prepend |
| `TimingProfiler` | PMU (Performance Monitor Unit) counters |

---

*SanDisk Hackathon — FUSE-based firmware compression simulation*
