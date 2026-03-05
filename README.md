# CacheSelect — Adaptive SSD Compression Filesystem
### SanDisk Hackathon Prototype · FUSE-Based Firmware Simulation

> A production-quality simulation of the compression pipeline found in modern
> NVMe SSD firmware (ARM Cortex-R5 / RISC-V class controllers), exposed as a
> real mountable Linux filesystem via FUSE.

---

## What This Is

CacheSelect dynamically selects a compression codec **per 4 KB block** based on
real-time entropy and pattern analysis — exactly as firmware does inside an SSD
controller. The entire pipeline runs in userspace, mounted as a real filesystem,
so any Linux tool (`cp`, `dd`, `cat`, `vim`, `rsync`) exercises the engine live.

**This is not a simulation of compression. This is a simulation of the firmware
decision engine that decides whether and how to compress — with a real filesystem
on top.**

---

## Write Path Pipeline

```
┌──────────────────────────────────────────────────────────────┐
│                    WRITE PATH PIPELINE                        │
├──────────────────────────────────────────────────────────────┤
│  [User write syscall]                                         │
│       ↓                                                       │
│  Linux VFS  →  FUSE  →  CacheSelectFS.write()               │
│       ↓                                                       │
│  Split into 4 KB LBA-aligned blocks (zero-pad last)          │
│       ↓                                                       │
│  1. MurmurHash3-128  (seed=42, deterministic)                │
│       ↓                                                       │
│  2. PatternCache lookup  ──HIT──→  reuse cached codec        │
│       ↓ MISS                                                  │
│  3. FeatureAnalyzer                                           │
│       ├─ Shannon entropy  (256-bin histogram)                 │
│       └─ Run-Length Density  (adjacent byte equality)        │
│       ↓                                                       │
│  4. PolicySelector  (deterministic threshold tree)           │
│       ↓                                                       │
│  5. CompressionEngine  (LZ4 / LZ4HC / RAW pass-through)     │
│       ↓                                                       │
│  6. BenefitCheck  (revert to RAW if savings insufficient)    │
│       ↓                                                       │
│  7. BlockPacker  →  4096-byte LBA frame + CRC-16             │
│       ↓                                                       │
│  8. blocks.bin  (append-only frame store)                    │
│       ↓                                                       │
│  9. PatternCache insert  +  metadata.json update             │
└──────────────────────────────────────────────────────────────┘
```

## Read Path Pipeline

```
blocks.bin
    ↓
Seek to block offset (from metadata.json)
    ↓
Read 4096-byte frame
    ↓
CRC-16 integrity validation  →  EIO on mismatch
    ↓
Unpack header (codec_id, orig_size, comp_size)
    ↓
Extract payload  →  LZ4 / LZ4HC decompress  /  RAW pass-through
    ↓
Strip zero-padding  →  slice to requested range
    ↓
Return to user
```

---

## Codec Policy Decision Tree

```
  [4 KB block]
       │
       ▼
  zero_density ≥ 0.999? ──YES──▶  SKIP   (all-zero block, no I/O needed)
       │ NO
       ▼
  entropy > 7.5? ─────────YES──▶  RAW    (random / encrypted / JPEG / MP4)
       │ NO                         skip compression entirely
       ▼
  RLD > 0.4? ─────────────YES──▶  LZ4    (logs / metadata / run-length rich)
       │ NO                         fast compression path
       ▼
                                 LZ4HC   (JSON / source / DB rows)
                                          maximise compression ratio
```

---

## LBA Frame Format

Every block is stored as **exactly 4096 bytes**, regardless of compressed size.
This matches real NAND LBA granularity — deterministic seek arithmetic, no
fragmentation.

```
┌─────────────────────────────────────────┐  Offset
│ codec_id         (uint8)                │   0
│ original_size    (uint16 BE)            │   1–2
│ compressed_size  (uint16 BE)            │   3–4
│ reserved         (5 × 0x00)            │   5–9
├─────────────────────────────────────────┤   10
│ compressed payload                      │
│ (up to 4084 bytes)                      │
├─────────────────────────────────────────┤   10 + compressed_size
│ zero padding                            │
├─────────────────────────────────────────┤   4094
│ CRC-16/IBM checksum (uint16 BE)         │
└─────────────────────────────────────────┘   4096  (always exactly)
```

---

## Repository Structure

```
CacheSelect/
│
├── core/
│   └── block_engine.py          Adaptive compression policy engine
│
├── fuse/
│   └── cachefs.py               FUSE filesystem (VFS interception layer)
│
├── benchmarks/
│   ├── workload_runner.py       Synthetic mixed-workload simulator
│   ├── fs_stress_test.py        Filesystem stress + large-file test
│   └── benchmark_runner.py      Per-module pipeline benchmark
│
├── storage/                     Auto-created on first mount
│   ├── blocks.bin               Packed LBA frame store
│   └── metadata.json            File → block offset index
│
├── output/                      Benchmark artefacts
│   ├── metrics.json
│   ├── timings.csv
│   └── dashboard.html
│
├── graphs/                      Performance charts
│   ├── performance.png
│   ├── cache_hit_rate.png
│   └── block_distribution.png
│
└── mountpoint/                  Empty directory for FUSE mount
```

---

## Module Reference

| Module | Firmware Equivalent | Description |
|--------|--------------------|----|
| `core/block_engine.py` | Full controller pipeline | End-to-end per-block orchestrator |
| `fuse/cachefs.py` | Host I/O driver | FUSE VFS interception + frame store |
| `pattern_cache.py` | SRAM codec cache (16–256 KB) | LRU cache: (hash, prefix) → codec_id |
| `feature_analyzer.py` | DSP histogram + RLD counter | Shannon entropy + Run-Length Density |
| `policy_selector.py` | Threshold comparator (2 µs) | Deterministic codec decision tree |
| `compression_engine.py` | HW LZ4 accelerator | LZ4 / LZ4HC / RAW with fallback |
| `block_packer.py` | DMA descriptor + ECC prepend | 4096-byte frame assembler + CRC-16 |
| `timing_profiler.py` | PMU counters | Per-stage latency profiler + CSV export |
| `benchmark_runner.py` | — | Workload benchmark driver |
| `dashboard.py` | — | HTML monitoring dashboard |

---

## Installation

```bash
git clone https://github.com/Amritha902/CacheSense-Sandisk.git
cd CacheSelect

python3 -m venv venv
source venv/bin/activate

pip install mmh3 lz4 fusepy
```

> **Linux only** for FUSE mount. macOS requires `macFUSE`.
> The BlockEngine and benchmarks run on any platform without FUSE.

---

## Quick Start

### 1 — Run the compression engine standalone

```bash
python -m core.block_engine
```

Expected output:
```
════════════════════════════════════════════════════════════════
  CacheSelect BlockEngine — Smoke Test
════════════════════════════════════════════════════════════════
  RANDOM     | codec=RAW    | H=  7.998 | RLD=  0.004 | comp= 4084B
  REPETITIVE | codec=LZ4    | H=  1.000 | RLD=  0.996 | comp=   27B
  STRUCTURED | codec=LZ4HC  | H=  4.231 | RLD=  0.142 | comp=  312B
```

### 2 — Run the workload benchmark

```bash
python -m benchmarks.workload_runner
```

```bash
# Options
python -m benchmarks.workload_runner --blocks 50000
python -m benchmarks.workload_runner --workload random      # pure incompressible
python -m benchmarks.workload_runner --workload structured  # pure compressible
python -m benchmarks.workload_runner --no-warmup            # cold cache
python -m benchmarks.workload_runner --csv                  # export CSV
```

### 3 — Mount the FUSE filesystem

```bash
mkdir -p mountpoint storage
python fuse/cachefs.py mountpoint/
```

### 4 — Use it like a real filesystem (new terminal)

```bash
echo "hello world"        > mountpoint/test.txt
cat mountpoint/test.txt

cp /var/log/syslog          mountpoint/syslog.txt
cat mountpoint/.stats
```

### 5 — Run the stress test

```bash
# 500 mixed files, all SHA-256 verified
python benchmarks/fs_stress_test.py mountpoint/

# 40 MB large file (dd simulation)
python benchmarks/fs_stress_test.py mountpoint/ --large-only

# Both together
python benchmarks/fs_stress_test.py mountpoint/ --files 500 --large-file
```

### 6 — Unmount

```bash
fusermount -u mountpoint/
```

---

## Benchmark Results

All results obtained on the prototype filesystem with real engine output.
No metrics are mocked or estimated.

### Workload Simulation  (`workload_runner.py`)

Workload mix: 40% random · 25% structured · 20% repetitive · 15% archive

| Metric | Value |
|--------|-------|
| Blocks processed | 10,500 |
| Throughput | **2.33 MB/s** |
| Avg block latency | **1.65 ms** |
| Cache hit rate | **47.21%** |
| Compression ratio | **0.5343** |
| RAW blocks | 5,541 (52.8%) |
| Compressed blocks | 4,959 (47.2%) |

### Filesystem Stress Test  (`fs_stress_test.py`)

10,000 files written sequentially, read back, SHA-256 verified.
Zero verification failures.

### Large File Test  (`dd if=/dev/zero`)

| Metric | Value |
|--------|-------|
| Data written | 205 MB |
| Write speed | 101 KB/s |
| Write ops | 60,000 |
| Cache hit rate | **83.3%** |
| Avg compression ratio | **0.1720** |
| Compressed blocks | 50,000 |

Zero-filled data achieves very high compression — expected and correct.

### Pipeline Benchmark  (`benchmark_runner.py`)

400 blocks · 1.6 MB dataset

| Metric | Value |
|--------|-------|
| Throughput | **2.93 MB/s** |
| Compression ratio | **0.6352** |
| Space saving | **36.48%** |
| Avg block latency | **1.17 ms** |
| P95 latency | **1.85 ms** |
| P99 latency | **2.04 ms** |

Codec distribution:

| Codec | Share |
|-------|-------|
| RAW | 56.99% |
| LZ4 | 10.36% |
| LZ4HC | 32.38% |
| SKIP | 0.26% |

Per-workload breakdown:

| Workload | Compression Ratio | Space Saved |
|----------|------------------|-------------|
| Random data | 1.000 | 0% |
| Structured logs | 0.2167 | **78.3%** |
| Repetitive data | 0.2192 | **78.1%** |
| Archive / encrypted | 1.000 | 0% |

Incompressible data correctly bypassed. Structured data heavily compressed.
This is the adaptive policy working as designed.

---

## Live Stats — Virtual File

The filesystem exposes a live instrumentation file at `mountpoint/.stats`.
No server. No polling. Just `cat`.

```bash
cat mountpoint/.stats
```

```
CacheSelect Filesystem — Live Stats
----------------------------------------
Write Ops              : 42
Read Ops               : 18

Logical Bytes Written  : 10,485,760
Physical Bytes Written : 5,242,880
Compression Ratio(L/P) : 2.000x

Total Blocks Written   : 2560
  RAW blocks           : 1024  (40.0%)
  Compressed blocks    : 1536  (60.0%)

Avg Write Latency      : 0.812 ms
Avg Read Latency       : 0.234 ms

-- BlockEngine internals --
Cache Hit Rate         : 73.4%
Cache Entries Used     : 892 / 2730
Avg Entropy (misses)   : 5.8821 / 8.0
Avg Compression Ratio  : 0.6123
```

---

## Hackathon Demo Script

```bash
# Terminal 1 — mount
python fuse/cachefs.py mountpoint/

# Terminal 2 — demo

echo "=== Incompressible (random bytes) ==="
dd if=/dev/urandom bs=4K count=100 of=mountpoint/random.bin 2>/dev/null
cat mountpoint/.stats | grep -E "RAW|Ratio"

echo "=== Compressible (structured log) ==="
python -c "
line = 'timestamp=1234567890;level=INFO;msg=cache_hit;codec=LZ4HC;\n'
open('mountpoint/logs.txt','w').write(line * 10000)
"
cat mountpoint/.stats | grep -E "Ratio|Compressed"

echo "=== Large file (40MB zeros) ==="
python benchmarks/fs_stress_test.py mountpoint/ --large-only

echo "=== Stress test (500 files, verified) ==="
python benchmarks/fs_stress_test.py mountpoint/ --files 500

echo "=== Final stats ==="
cat mountpoint/.stats
```

---

## Key Design Decisions

**Why MurmurHash3?**
Non-cryptographic, extremely fast, excellent distribution. Seed=42 gives
deterministic fingerprints across runs — essential for cache correctness.

**Why LRU cache?**
Repeated patterns (log lines, DB pages) converge on the same codec. The cache
avoids re-running entropy analysis on every write of similar data, cutting CPU
cost significantly — mirroring how real firmware uses SRAM-resident codec tables.

**Why fixed 4096-byte frames?**
Matches real NAND LBA granularity. Every frame is identical in size — no
fragmentation, deterministic seek arithmetic, easy integrity scanning.

**Why CRC-16 not CRC-32?**
2 bytes overhead vs 4. At 4 KB block size the collision probability is
acceptable, and the frame budget is tight (10 B header + 2 B CRC = 12 B total).

**Why FUSE?**
FUSE lets us intercept actual VFS calls (`write`, `read`, `open`) from any
program without a kernel driver. `cp`, `dd`, `vim`, `rsync` — all go through
BlockEngine. The demo is real, not scripted.

---

## Firmware Correspondence

| This Project | Real Firmware Equivalent |
|---|---|
| `MurmurHash3` | HW hash engine (CRC32C or multiply-shift) |
| `PatternCache` (LRU, 256 KB) | SRAM-resident codec cache |
| `FeatureAnalyzer` | DSP block: histogram + run-length counter |
| `PolicySelector` | Threshold comparator (2 µs budget) |
| `CompressionEngine` | HW LZ4 accelerator / SW LZ4HC fallback |
| `BlockPacker` | DMA descriptor builder + ECC prepend |
| `TimingProfiler` | PMU (Performance Monitor Unit) counters |
| `CRC-16/IBM` | ECC engine integrity tag |
| `FUSE layer` | NVMe host driver (PCIe DMA path) |

---

## Output Files

| File | Description |
|------|-------------|
| `storage/blocks.bin` | Packed LBA frames (4096 bytes each, append-only) |
| `storage/metadata.json` | File → block offset mapping |
| `output/metrics.json` | Pipeline performance metrics |
| `output/timings.csv` | Per-block stage latency records |
| `output/dashboard.html` | Interactive monitoring dashboard |
| `output/<run>_metrics.csv` | Benchmark results CSV |
| `output/<run>_full.json` | Full benchmark JSON |

---

## Future Work

- Hardware acceleration via dedicated LZ4 IP core
- Kernel-space block device driver (bypass FUSE overhead)
- NVMe command simulation (NVM Express 1.4 spec)
- ML-based compression predictor (replace threshold tree)
- Larger real-world dataset validation
- Multi-stream write support (simulating SSD namespace isolation)

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `mmh3` | MurmurHash3-128 block fingerprinting |
| `lz4` | LZ4 / LZ4HC block compression |
| `fusepy` | FUSE Python bindings |

```bash
pip install mmh3 lz4 fusepy
```

All other functionality uses Python 3.8+ stdlib only
(`struct`, `math`, `time`, `os`, `csv`, `json`, `collections`, `threading`).

---

## License

MIT License

---

*SanDisk Hackathon — FUSE-based NVMe firmware compression simulation*  
*Amritha · CacheSelect / CacheSense*
