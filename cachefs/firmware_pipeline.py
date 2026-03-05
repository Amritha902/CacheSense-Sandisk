"""
=============================================================================
MODULE: firmware_pipeline.py
DESCRIPTION: Main SSD Firmware Compression Pipeline

Orchestrates all pipeline stages into a single, cohesive workflow that
mirrors the firmware write path in a modern NVMe SSD:

  WRITE PATH (host → NAND):
  ┌──────────────────────────────────────────────────────────┐
  │  HOST DMA → [4KB block received]                         │
  │       ↓                                                  │
  │  Stage 1: MurmurHash3 signature                          │
  │       ↓                                                  │
  │  Stage 2: Pattern Cache lookup                           │
  │       ↓ (hit)              ↓ (miss)                      │
  │  Use cached codec     Stage 3: Feature extraction         │
  │                            ↓                             │
  │                       Stage 4: Codec selection            │
  │                            ↓                             │
  │                       Stage 5: Compression               │
  │                            ↓                             │
  │  Stage 6: Block packing (4096-byte LBA frame)            │
  │       ↓                                                  │
  │  Stage 7: Write frame to blocks.bin                      │
  │       ↓                                                  │
  │  Stage 8: Update pattern cache + timing profiler         │
  └──────────────────────────────────────────────────────────┘

FIRMWARE ANALOGY:
  This module mirrors the fw_compression_write_handler() function
  in SanDisk/WD NVMe SSD firmware. Each stage corresponds to a
  real firmware subsystem with defined latency budgets.
=============================================================================
"""

import os
import sys
import time
import struct
import json
from typing import Optional, Dict, Any, List, Iterator

# --- Adjust Python path for direct execution ---
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

from pattern_cache    import PatternCache, compute_signature
from feature_analyzer import FeatureAnalyzer
from policy_selector  import PolicySelector, CODEC_NAMES
from compression_engine import CompressionEngine, CODEC_LZ4, CODEC_LZ4HC, CODEC_RAW
from block_packer     import BlockPacker
from timing_profiler  import TimingProfiler

# ---------------------------------------------------------------------------
# Pipeline Constants
# ---------------------------------------------------------------------------

BLOCK_SIZE        = 4096
OUTPUT_FILENAME   = 'blocks.bin'
METRICS_FILENAME  = 'metrics.json'


# ---------------------------------------------------------------------------
# Pipeline Result
# ---------------------------------------------------------------------------

class PipelineResult:
    """
    Result record for a single block processed through the pipeline.
    Returned by FirmwarePipeline.process_block().
    """
    __slots__ = (
        'block_index', 'signature', 'cache_hit',
        'entropy', 'rld', 'codec_selected', 'codec_used',
        'original_size', 'compressed_size', 'ratio',
        'benefit', 'frame_offset', 'total_us', 'success',
        'error_msg',
    )

    def __init__(self):
        self.block_index     : int   = 0
        self.signature       : int   = 0
        self.cache_hit       : bool  = False
        self.entropy         : float = 0.0
        self.rld             : float = 0.0
        self.codec_selected  : str   = ''
        self.codec_used      : str   = ''
        self.original_size   : int   = BLOCK_SIZE
        self.compressed_size : int   = BLOCK_SIZE
        self.ratio           : float = 1.0
        self.benefit         : bool  = False
        self.frame_offset    : int   = -1
        self.total_us        : float = 0.0
        self.success         : bool  = False
        self.error_msg       : str   = ''

    @property
    def space_saving_pct(self) -> float:
        return round((1.0 - self.ratio) * 100, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'block_index'    : self.block_index,
            'signature'      : f'{self.signature:016x}',
            'cache_hit'      : self.cache_hit,
            'entropy'        : round(self.entropy, 4),
            'rld'            : round(self.rld, 4),
            'codec_selected' : self.codec_selected,
            'codec_used'     : self.codec_used,
            'original_size'  : self.original_size,
            'compressed_size': self.compressed_size,
            'ratio'          : round(self.ratio, 4),
            'space_saving_pct': self.space_saving_pct,
            'benefit'        : self.benefit,
            'frame_offset'   : self.frame_offset,
            'total_us'       : round(self.total_us, 3),
            'success'        : self.success,
        }

    def __repr__(self) -> str:
        return (f"[Block {self.block_index:6d}] "
                f"{'HIT ' if self.cache_hit else 'miss'} | "
                f"H={self.entropy:.2f} RLD={self.rld:.2f} | "
                f"codec={self.codec_used:6s} | "
                f"ratio={self.ratio:.3f} ({self.space_saving_pct:+.1f}%) | "
                f"{self.total_us:.1f}µs")


# ---------------------------------------------------------------------------
# Firmware Pipeline
# ---------------------------------------------------------------------------

class FirmwarePipeline:
    """
    Main SSD firmware compression pipeline.

    Processes 4KB blocks sequentially through all compression stages,
    writes LBA frames to binary output file, and maintains full
    performance statistics.

    Example:
        pipeline = FirmwarePipeline(output_dir='output/')
        result = pipeline.process_block(block_bytes)
        pipeline.process_folder('data/logs/')
        pipeline.print_summary()
        pipeline.export_metrics('output/metrics.json')
    """

    def __init__(self,
                 output_dir      : str  = 'output',
                 output_filename : str  = OUTPUT_FILENAME,
                 verbose         : bool = True,
                 log_every_n     : int  = 100):
        """
        Initialize pipeline with all subsystems.

        Args:
            output_dir     : directory for blocks.bin and metrics
            output_filename: binary output file name
            verbose        : print per-block log lines
            log_every_n    : only print log every N blocks (0 = all)
        """
        self.output_dir     = output_dir
        self.verbose        = verbose
        self.log_every_n    = log_every_n

        os.makedirs(output_dir, exist_ok=True)
        self._output_path = os.path.join(output_dir, output_filename)

        # --- Initialize all subsystems ---
        self.cache    = PatternCache()
        self.analyzer = FeatureAnalyzer()
        self.selector = PolicySelector()
        self.engine   = CompressionEngine()
        self.packer   = BlockPacker()
        self.profiler = TimingProfiler()

        # --- Output file ---
        self._output_file = open(self._output_path, 'wb')
        self._frame_count = 0
        self._current_offset = 0   # byte offset in blocks.bin

        # --- Pipeline-level stats ---
        self._total_blocks    = 0
        self._total_errors    = 0
        self._total_cache_hits= 0
        self._total_bytes_in  = 0
        self._total_bytes_out = 0

        if verbose:
            print(f"  FirmwarePipeline initialized")
            print(f"  Output: {self._output_path}")
            print()

    # ------------------------------------------------------------------
    # Core processing method
    # ------------------------------------------------------------------

    def process_block(self, block_bytes: bytes,
                      label: str = '') -> PipelineResult:
        """
        Process a single 4KB block through the full pipeline.

        Pipeline stages:
          1. Hash       — compute MurmurHash3 signature
          2. Cache      — lookup pattern cache
          3. Features   — entropy + RLD (skip if cache hit)
          4. Policy     — codec selection (skip if cache hit)
          5. Compress   — LZ4 / LZ4HC / RAW
          6. Pack       — assemble 4096-byte LBA frame
          7. Write      — append frame to blocks.bin
          8. Update     — cache insert / profiler commit

        Args:
            block_bytes : 4096-byte block (padded/truncated if needed)
            label       : optional label for logging

        Returns:
            PipelineResult with all stage outcomes
        """
        # Normalize block to exactly 4096 bytes
        if len(block_bytes) < BLOCK_SIZE:
            block_bytes = block_bytes + bytes(BLOCK_SIZE - len(block_bytes))
        elif len(block_bytes) > BLOCK_SIZE:
            block_bytes = block_bytes[:BLOCK_SIZE]

        result = PipelineResult()
        result.block_index  = self._total_blocks + 1
        result.original_size = BLOCK_SIZE
        t_pipeline_start = time.perf_counter()

        try:
            # --------------------------------------------------------
            # Stage 1: Hash
            # --------------------------------------------------------
            with self.profiler.measure('hashing'):
                sig = compute_signature(block_bytes)
            result.signature = sig

            # --------------------------------------------------------
            # Stage 2: Pattern Cache Lookup
            # --------------------------------------------------------
            with self.profiler.measure('cache_lookup'):
                cache_entry = self.cache.lookup(block_bytes)

            cache_hit = cache_entry is not None
            result.cache_hit = cache_hit
            if cache_hit:
                self._total_cache_hits += 1

            # --------------------------------------------------------
            # Stage 3 & 4: Feature Extraction + Codec Selection
            # (Only if cache MISS — skip on hit to save latency)
            # --------------------------------------------------------
            if cache_hit:
                # Use cached codec decision
                codec_name = CODEC_NAMES.get(cache_entry.codec_id, 'LZ4')
                result.entropy = 0.0   # not recomputed on hit
                result.rld     = 0.0
                result.codec_selected = codec_name
                # Update EMA ratio in cache
                # (will be updated after compression with actual ratio)
            else:
                # Cache miss — full feature extraction
                with self.profiler.measure('feature_extract'):
                    features = self.analyzer.analyze(block_bytes)

                result.entropy = features['entropy']
                result.rld     = features['run_length_density']

                with self.profiler.measure('codec_select'):
                    decision = self.selector.select_codec_from_features(features)

                codec_name = decision.codec_name
                result.codec_selected = codec_name

            # --------------------------------------------------------
            # Stage 5: Compression
            # --------------------------------------------------------
            with self.profiler.measure('compression'):
                comp_result = self.engine.compress(block_bytes, codec_name)

            result.compressed_size = comp_result['compressed_size']
            result.ratio           = comp_result['ratio']
            result.benefit         = comp_result['benefit']
            result.codec_used      = comp_result['used_codec']

            # --------------------------------------------------------
            # Stage 6: Block Packing
            # --------------------------------------------------------
            with self.profiler.measure('block_packing'):
                frame = self.packer.pack_block(
                    codec_id_or_name = comp_result['used_codec'],
                    original_bytes   = block_bytes,
                    compressed_bytes = comp_result['compressed_bytes'],
                )

            # --------------------------------------------------------
            # Stage 7: Write frame to blocks.bin
            # --------------------------------------------------------
            result.frame_offset = self._current_offset
            self._output_file.write(frame)
            self._output_file.flush()   # ensures durability
            self._current_offset += BLOCK_SIZE
            self._frame_count    += 1

            # --------------------------------------------------------
            # Stage 8: Update cache + stats
            # --------------------------------------------------------
            if not cache_hit:
                codec_id = {'RAW': 0, 'LZ4': 1, 'LZ4HC': 2, 'SKIP': -1}.get(
                    comp_result['used_codec'], 0)
                self.cache.insert(block_bytes, codec_id, comp_result['ratio'])
            else:
                # Update cached ratio with actual observed ratio
                self.cache.update_ratio(sig, comp_result['ratio'])

            # --------------------------------------------------------
            # Profiler commit
            # --------------------------------------------------------
            self.profiler.set_metadata(
                codec_used        = result.codec_used,
                cache_hit         = cache_hit,
                compression_ratio = result.ratio,
            )
            bt = self.profiler.commit()
            result.total_us = bt.total_us
            result.success  = True

        except Exception as e:
            result.error_msg = str(e)
            result.success   = False
            result.total_us  = (time.perf_counter() - t_pipeline_start) * 1e6
            self._total_errors += 1

        # --- Update pipeline-level totals ---
        self._total_blocks    += 1
        self._total_bytes_in  += BLOCK_SIZE
        self._total_bytes_out += result.compressed_size

        # --- Logging ---
        if self.verbose:
            should_log = (self.log_every_n == 0 or
                          self._total_blocks % self.log_every_n == 0 or
                          self._total_blocks == 1)
            if should_log:
                tag = f"[{label}] " if label else ""
                print(f"  {tag}{result}")

        return result

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def process_blocks(self, blocks: List[bytes],
                       label: str = '') -> List[PipelineResult]:
        """
        Process a list of 4KB blocks.

        Args:
            blocks : list of byte strings (each ~4096 bytes)
            label  : source label for logging

        Returns: list of PipelineResult objects
        """
        results = []
        for i, block in enumerate(blocks):
            lbl = f"{label}:{i}" if label else str(i)
            results.append(self.process_block(block, label=lbl))
        return results

    def process_file(self, filepath: str) -> List[PipelineResult]:
        """
        Process a file as a stream of 4KB blocks.

        Splits file into 4096-byte chunks (padding last block with zeros).

        Args:
            filepath : path to input file

        Returns: list of PipelineResult objects
        """
        results = []
        filename = os.path.basename(filepath)
        with open(filepath, 'rb') as f:
            block_num = 0
            while True:
                block = f.read(BLOCK_SIZE)
                if not block:
                    break
                block_num += 1
                label = f"{filename}:blk{block_num}"
                results.append(self.process_block(block, label=label))
        return results

    def process_folder(self, folder_path: str,
                       extensions: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Process all files in a folder as block streams.

        Args:
            folder_path : path to folder
            extensions  : list of extensions to filter (e.g., ['.log', '.bin'])
                          None means process all files

        Returns: summary dict with per-file stats
        """
        if not os.path.isdir(folder_path):
            raise ValueError(f"Not a directory: {folder_path}")

        summary = {
            'folder'         : folder_path,
            'files_processed': 0,
            'blocks_processed': 0,
            'total_results'  : [],
        }

        for fname in sorted(os.listdir(folder_path)):
            fpath = os.path.join(folder_path, fname)
            if not os.path.isfile(fpath):
                continue
            if extensions:
                if not any(fname.endswith(ext) for ext in extensions):
                    continue

            if self.verbose:
                print(f"\n  ── Processing: {fname} "
                      f"({os.path.getsize(fpath):,} bytes) ──")

            try:
                results = self.process_file(fpath)
                summary['files_processed'] += 1
                summary['blocks_processed'] += len(results)
                summary['total_results'].extend(results)
            except Exception as e:
                if self.verbose:
                    print(f"  ERROR processing {fname}: {e}")

        return summary

    # ------------------------------------------------------------------
    # Metrics & reporting
    # ------------------------------------------------------------------

    def get_metrics(self) -> Dict[str, Any]:
        """
        Retrieve comprehensive pipeline metrics.

        Returns a dict suitable for export to JSON / dashboard.
        """
        overall_ratio = (self._total_bytes_out /
                         max(self._total_bytes_in, 1))
        cache_hit_rate = (self._total_cache_hits /
                          max(self._total_blocks, 1))

        return {
            'pipeline': {
                'total_blocks'       : self._total_blocks,
                'total_errors'       : self._total_errors,
                'frames_written'     : self._frame_count,
                'output_file'        : self._output_path,
                'output_size_bytes'  : self._current_offset,
            },
            'compression': {
                'overall_ratio'      : round(overall_ratio, 4),
                'space_saving_pct'   : round((1 - overall_ratio) * 100, 2),
                'total_bytes_in'     : self._total_bytes_in,
                'total_bytes_out'    : self._total_bytes_out,
                **self.engine.stats(),
            },
            'cache'      : self.cache.stats(),
            'analyzer'   : self.analyzer.stats(),
            'selector'   : self.selector.stats(),
            'packer'     : self.packer.stats(),
            'timing'     : self.profiler.stats(),
        }

    def export_metrics(self, filepath: Optional[str] = None) -> str:
        """Export metrics to JSON file."""
        if filepath is None:
            filepath = os.path.join(self.output_dir, METRICS_FILENAME)
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(self.get_metrics(), f, indent=2)
        return filepath

    def export_timing_csv(self, filepath: Optional[str] = None) -> str:
        """Export timing data to CSV."""
        if filepath is None:
            filepath = os.path.join(self.output_dir, 'timings.csv')
        rows = self.profiler.export_csv(filepath)
        return filepath

    def print_summary(self) -> None:
        """Print comprehensive pipeline summary."""
        m = self.get_metrics()
        p = m['pipeline']
        c = m['compression']
        ca = m['cache']

        print()
        print("╔══════════════════════════════════════════════════════╗")
        print("║        SSD Firmware Pipeline — Run Summary           ║")
        print("╠══════════════════════════════════════════════════════╣")
        print(f"║  Blocks processed  : {p['total_blocks']:>8,}                      ║")
        print(f"║  Errors            : {p['total_errors']:>8,}                      ║")
        print(f"║  Frames written    : {p['frames_written']:>8,}                      ║")
        print(f"║  Output size       : {p['output_size_bytes'] // 1024:>8,} KB                    ║")
        print(f"╠══════════════════════════════════════════════════════╣")
        print(f"║  COMPRESSION RESULTS                                 ║")
        print(f"║  Overall ratio     : {c['overall_ratio']:>8.4f}                      ║")
        print(f"║  Space saving      : {c['space_saving_pct']:>7.2f}%                      ║")
        print(f"║  Bytes in          : {c['total_bytes_in']:>8,}                      ║")
        print(f"║  Bytes out         : {c['total_bytes_out']:>8,}                      ║")
        print(f"╠══════════════════════════════════════════════════════╣")
        print(f"║  PATTERN CACHE                                       ║")
        print(f"║  Hit rate          : {ca['hit_rate']:>7.1%}                       ║")
        print(f"║  Entries           : {ca['entry_count']:>8,} / {ca['max_entries']:<6,}             ║")
        print(f"║  Evictions         : {ca['evictions']:>8,}                      ║")
        print(f"╠══════════════════════════════════════════════════════╣")
        print(f"║  CODEC DISTRIBUTION                                  ║")
        dist = m['selector']['codec_distribution_pct']
        for codec, pct in dist.items():
            if pct > 0:
                bar = '█' * int(pct / 5)
                print(f"║  {codec:<6}  {pct:>5.1f}%  {bar:<20}              ║")
        print(f"╠══════════════════════════════════════════════════════╣")

        t = m['timing']
        print(f"║  TIMING                                              ║")
        print(f"║  Avg latency       : {t['avg_total_us']:>8.1f} µs                    ║")
        print(f"║  Throughput        : {t['throughput_blocks_s']:>8,.0f} blocks/sec            ║")
        tput_mbs = t['throughput_blocks_s'] * 4096 / 1e6
        print(f"║  Throughput        : {tput_mbs:>8.1f} MB/sec                   ║")
        print(f"╚══════════════════════════════════════════════════════╝")

    def close(self) -> None:
        """Close output file and finalize."""
        if self._output_file and not self._output_file.closed:
            self._output_file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __repr__(self) -> str:
        return (f"FirmwarePipeline(blocks={self._total_blocks}, "
                f"ratio={self._total_bytes_out / max(self._total_bytes_in, 1):.3f}, "
                f"cache_hits={self._total_cache_hits})")


# ---------------------------------------------------------------------------
# Example usage script
# ---------------------------------------------------------------------------

def _generate_test_data(output_dir: str, n_blocks_each: int = 25) -> str:
    """Generate synthetic test data representing different workload types."""
    import random
    import string

    data_dir = os.path.join(output_dir, 'test_data')
    os.makedirs(data_dir, exist_ok=True)

    # Workload 1: Random data (simulates encrypted / compressed archives)
    with open(os.path.join(data_dir, 'random_data.bin'), 'wb') as f:
        for _ in range(n_blocks_each):
            f.write(os.urandom(BLOCK_SIZE))

    # Workload 2: Structured log data (simulates application logs)
    log_template = (
        "2024-01-15 {h:02d}:{m:02d}:{s:02d}.{ms:03d} "
        "{level:5s} [{module}] {msg}\n"
    )
    levels  = ['INFO ', 'DEBUG', 'WARN ', 'ERROR']
    modules = ['StorageEngine', 'WriteBuffer', 'GarbageCollector', 'NVMe']
    msgs    = ['write_complete', 'cache_evict', 'block_alloc', 'gc_cycle_start']
    with open(os.path.join(data_dir, 'app_logs.log'), 'wb') as f:
        for _ in range(n_blocks_each):
            block = b''
            while len(block) < BLOCK_SIZE:
                line = log_template.format(
                    h=random.randint(0, 23), m=random.randint(0, 59),
                    s=random.randint(0, 59), ms=random.randint(0, 999),
                    level=random.choice(levels),
                    module=random.choice(modules),
                    msg=random.choice(msgs),
                ).encode()
                block += line
            f.write(block[:BLOCK_SIZE])

    # Workload 3: Repetitive data (simulates database null pages / buffers)
    with open(os.path.join(data_dir, 'repetitive.bin'), 'wb') as f:
        for i in range(n_blocks_each):
            pattern = bytes([i % 256] * 64 + [0xFF] * 64) * 32
            f.write(pattern[:BLOCK_SIZE])

    # Workload 4: Zero blocks (simulates unwritten LBAs)
    with open(os.path.join(data_dir, 'zero_blocks.bin'), 'wb') as f:
        f.write(bytes(BLOCK_SIZE * n_blocks_each))

    # Workload 5: Mixed structured data
    with open(os.path.join(data_dir, 'mixed_data.bin'), 'wb') as f:
        for i in range(n_blocks_each):
            if i % 3 == 0:
                f.write(os.urandom(BLOCK_SIZE))
            elif i % 3 == 1:
                f.write((b'\x00\x01\x02\x03' * 1024)[:BLOCK_SIZE])
            else:
                f.write((b'SANDISK_WD_NAND_CTRL_' * 200)[:BLOCK_SIZE])

    print(f"  Test data generated in: {data_dir}")
    return data_dir


if __name__ == '__main__':
    print("╔══════════════════════════════════════════════════════╗")
    print("║   SSD Firmware Compression Engine — Main Pipeline    ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    OUTPUT_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'output'
    )
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # --- Generate test data ---
    print("  [1/3] Generating synthetic workload data...")
    data_dir = _generate_test_data(OUTPUT_DIR, n_blocks_each=30)

    # --- Run pipeline ---
    print()
    print("  [2/3] Processing blocks through firmware pipeline...")
    print(f"  {'─'*54}")

    with FirmwarePipeline(
        output_dir   = OUTPUT_DIR,
        verbose      = True,
        log_every_n  = 20,    # log every 20th block
    ) as pipeline:

        summary = pipeline.process_folder(data_dir)

        print()
        print("  [3/3] Exporting results...")
        metrics_path = pipeline.export_metrics()
        timing_path  = pipeline.export_timing_csv()
        print(f"  Metrics JSON  : {metrics_path}")
        print(f"  Timing CSV    : {timing_path}")
        print(f"  Binary output : {pipeline._output_path}")

        print()
        pipeline.print_summary()

    print()
    print("  Pipeline complete. Firmware simulation finished.")
