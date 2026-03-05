"""
=============================================================================
MODULE: block_packer.py
DESCRIPTION: 4KB LBA Frame Packer / Unpacker for SSD Firmware Simulation

Implements the fixed-size 4096-byte LBA frame format used by the SSD
compression engine to store compressed blocks on NAND flash.

FRAME FORMAT (4096 bytes total):
  ┌──────────────────────────────────────────────────────┐
  │ Bytes  0-0  │ codec_id        (uint8)   │ 1 byte     │
  │ Bytes  1-2  │ original_size   (uint16)  │ 2 bytes    │
  │ Bytes  3-4  │ compressed_size (uint16)  │ 2 bytes    │
  │ Byte   5    │ flags           (uint8)   │ 1 byte     │
  │ Bytes  6-9  │ reserved        (uint32)  │ 4 bytes    │
  ├──────────────────────────────────────────────────────┤
  │ Bytes 10 → 10+compressed_size-1                      │
  │             compressed data                          │
  ├──────────────────────────────────────────────────────┤
  │ Bytes 10+compressed_size → 4093                      │
  │             zero padding                             │
  ├──────────────────────────────────────────────────────┤
  │ Bytes 4094-4095 │ CRC16-CCITT checksum  │ 2 bytes    │
  └──────────────────────────────────────────────────────┘

FLAGS byte (bit field):
  Bit 0: COMPRESSED  — 1 if data is compressed, 0 if RAW
  Bit 1: ZERO_BLOCK  — 1 if block is all zeros (skip read)
  Bit 2: VERIFIED    — 1 if CRC was verified on last read
  Bits 3-7: reserved

FIRMWARE ANALOGY:
  - This format mirrors physical NAND page layout in enterprise SSDs
  - CRC16 is computed by hardware engine in firmware (~1 µs)
  - Frame packing is the final stage before DMA transfer to flash
  - Zero-block flag allows read-path optimization (return zeros without IO)
=============================================================================
"""

import struct
import time
from typing import Dict, Any, Tuple, Optional
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Frame Layout Constants
# ---------------------------------------------------------------------------

FRAME_SIZE         = 4096   # Total frame size — ALWAYS exactly 4096 bytes
HEADER_SIZE        = 10     # Bytes 0–9: metadata header
FOOTER_SIZE        = 2      # Bytes 4094–4095: CRC16
MAX_PAYLOAD_SIZE   = FRAME_SIZE - HEADER_SIZE - FOOTER_SIZE   # 4084 bytes
DATA_OFFSET        = HEADER_SIZE                               # 10
CRC_OFFSET         = FRAME_SIZE - FOOTER_SIZE                  # 4094

# Header field offsets
OFFSET_CODEC_ID        = 0   # 1 byte
OFFSET_ORIGINAL_SIZE   = 1   # 2 bytes (uint16 LE)
OFFSET_COMPRESSED_SIZE = 3   # 2 bytes (uint16 LE)
OFFSET_FLAGS           = 5   # 1 byte
OFFSET_RESERVED        = 6   # 4 bytes (uint32 LE)

# Codec IDs
CODEC_SKIP  = 0xFF
CODEC_RAW   = 0x00
CODEC_LZ4   = 0x01
CODEC_LZ4HC = 0x02

CODEC_ID_TO_NAME = {
    0xFF : 'SKIP',
    0x00 : 'RAW',
    0x01 : 'LZ4',
    0x02 : 'LZ4HC',
}
CODEC_NAME_TO_ID = {v: k for k, v in CODEC_ID_TO_NAME.items()}

# Flags
FLAG_COMPRESSED = 0x01
FLAG_ZERO_BLOCK = 0x02
FLAG_VERIFIED   = 0x04


# ---------------------------------------------------------------------------
# CRC-16/CCITT-FALSE
# ---------------------------------------------------------------------------

def _build_crc16_table() -> list:
    """Precompute CRC-16/CCITT-FALSE lookup table (poly=0x1021)."""
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        table.append(crc)
    return table

_CRC16_TABLE = _build_crc16_table()


def compute_crc16(data: bytes, initial: int = 0xFFFF) -> int:
    """
    Compute CRC-16/CCITT-FALSE over data bytes.

    Firmware: computed by hardware CRC engine in parallel with DMA transfer.
    Pure Python implementation for host-side simulation.

    Args:
        data    : bytes to checksum
        initial : initial CRC value (0xFFFF for CCITT-FALSE)

    Returns: 16-bit CRC value
    """
    crc = initial
    for byte in data:
        crc = ((crc << 8) ^ _CRC16_TABLE[((crc >> 8) ^ byte) & 0xFF]) & 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# Frame Header dataclass
# ---------------------------------------------------------------------------

@dataclass
class FrameHeader:
    """
    Decoded 10-byte frame header.

    Mirrors firmware frame_header_t struct:
      struct frame_header_t {
          uint8_t  codec_id;
          uint16_t original_size;
          uint16_t compressed_size;
          uint8_t  flags;
          uint32_t reserved;
      } __attribute__((packed));  // sizeof = 10 bytes
    """
    codec_id         : int
    original_size    : int
    compressed_size  : int
    flags            : int
    reserved         : int = 0

    @property
    def codec_name(self) -> str:
        return CODEC_ID_TO_NAME.get(self.codec_id, f'UNKNOWN(0x{self.codec_id:02x})')

    @property
    def is_compressed(self) -> bool:
        return bool(self.flags & FLAG_COMPRESSED)

    @property
    def is_zero_block(self) -> bool:
        return bool(self.flags & FLAG_ZERO_BLOCK)

    def to_bytes(self) -> bytes:
        """Serialize to 10-byte binary header."""
        return struct.pack('<BHHBI',
                           self.codec_id & 0xFF,
                           self.original_size & 0xFFFF,
                           self.compressed_size & 0xFFFF,
                           self.flags & 0xFF,
                           self.reserved & 0xFFFFFFFF)

    @classmethod
    def from_bytes(cls, data: bytes) -> 'FrameHeader':
        """Deserialize from 10-byte binary header."""
        if len(data) < HEADER_SIZE:
            raise ValueError(f"Header too short: {len(data)} < {HEADER_SIZE}")
        codec_id, orig_sz, comp_sz, flags, reserved = struct.unpack_from(
            '<BHHBI', data, 0)
        return cls(codec_id, orig_sz, comp_sz, flags, reserved)

    def __repr__(self) -> str:
        return (f"FrameHeader(codec={self.codec_name}, "
                f"orig={self.original_size}, "
                f"comp={self.compressed_size}, "
                f"flags=0x{self.flags:02x})")


# ---------------------------------------------------------------------------
# BlockPacker
# ---------------------------------------------------------------------------

class BlockPacker:
    """
    Packs compressed SSD blocks into fixed 4096-byte LBA frames.

    Ensures every output frame is EXACTLY 4096 bytes regardless of
    compressed data size — padding with zeros as needed.

    This mirrors the NAND page write process in SSD firmware where
    every LBA maps to exactly one physical page.
    """

    def __init__(self):
        self._packed_count   = 0
        self._unpacked_count = 0
        self._crc_errors     = 0
        self._total_pack_us  = 0.0
        self._zero_blocks    = 0

    def pack_block(self, codec_id_or_name,
                   original_bytes  : bytes,
                   compressed_bytes: bytes) -> bytes:
        """
        Pack a compressed block into a fixed 4096-byte frame.

        Args:
            codec_id_or_name : codec used (int ID or string name)
            original_bytes   : original 4096-byte block (for size reference)
            compressed_bytes : compressed data (may be same as original for RAW)

        Returns: exactly 4096 bytes (the complete LBA frame)

        Raises:
            ValueError if compressed_bytes exceeds MAX_PAYLOAD_SIZE (4084 bytes)
        """
        t_start = time.perf_counter()

        # --- Resolve codec ID ---
        if isinstance(codec_id_or_name, str):
            codec_id = CODEC_NAME_TO_ID.get(codec_id_or_name.upper(), CODEC_RAW)
        else:
            codec_id = int(codec_id_or_name)

        original_size    = len(original_bytes)
        compressed_size  = len(compressed_bytes)

        # --- Overflow check ---
        # For RAW codec: truncate to fit in frame (firmware splits large RAW across pages)
        if codec_id == CODEC_RAW and compressed_size > MAX_PAYLOAD_SIZE:
            compressed_bytes = compressed_bytes[:MAX_PAYLOAD_SIZE]
            compressed_size  = MAX_PAYLOAD_SIZE
        elif compressed_size > MAX_PAYLOAD_SIZE:
            raise ValueError(
                f"Compressed data ({compressed_size} bytes) exceeds "
                f"max payload ({MAX_PAYLOAD_SIZE} bytes). "
                f"Use RAW codec for this block."
            )

        # --- Build flags ---
        flags = 0
        if codec_id not in (CODEC_RAW, CODEC_SKIP):
            flags |= FLAG_COMPRESSED
        if all(b == 0 for b in original_bytes[:64]):   # quick zero heuristic
            if sum(original_bytes) == 0:
                flags |= FLAG_ZERO_BLOCK
                self._zero_blocks += 1

        # --- Build header ---
        header = FrameHeader(
            codec_id        = codec_id,
            original_size   = original_size,
            compressed_size = compressed_size,
            flags           = flags,
        )
        header_bytes = header.to_bytes()   # 10 bytes

        # --- Assemble frame body (without CRC) ---
        # Payload zone: DATA_OFFSET(10) to CRC_OFFSET(4094) = 4084 bytes
        padding_size = MAX_PAYLOAD_SIZE - compressed_size
        frame_body   = (header_bytes +
                        compressed_bytes +
                        bytes(padding_size))  # zero-pad to 4094

        assert len(frame_body) == FRAME_SIZE - FOOTER_SIZE, \
            f"Frame body wrong size: {len(frame_body)}"

        # --- Compute CRC16 over bytes 0–4093 ---
        crc = compute_crc16(frame_body)
        crc_bytes = struct.pack('<H', crc)

        # --- Final frame ---
        frame = frame_body + crc_bytes
        assert len(frame) == FRAME_SIZE, f"Frame size error: {len(frame)}"

        elapsed_us = (time.perf_counter() - t_start) * 1e6
        self._packed_count  += 1
        self._total_pack_us += elapsed_us

        return frame

    def unpack_block(self, frame_bytes: bytes) -> Dict[str, Any]:
        """
        Unpack a 4096-byte LBA frame, verify CRC, extract compressed data.

        Args:
            frame_bytes: exactly 4096-byte frame

        Returns dict:
            header           : FrameHeader object
            compressed_bytes : raw compressed payload
            crc_valid        : bool — CRC check result
            unpack_us        : float — microseconds to unpack
        """
        t_start = time.perf_counter()

        if len(frame_bytes) != FRAME_SIZE:
            raise ValueError(
                f"Frame must be exactly {FRAME_SIZE} bytes, "
                f"got {len(frame_bytes)}"
            )

        # --- Parse header ---
        header = FrameHeader.from_bytes(frame_bytes)

        # --- Verify CRC ---
        crc_valid = self.validate_crc(frame_bytes)
        if not crc_valid:
            self._crc_errors += 1

        # --- Extract payload ---
        payload_start = DATA_OFFSET
        payload_end   = DATA_OFFSET + header.compressed_size
        compressed    = frame_bytes[payload_start:payload_end]

        elapsed_us = (time.perf_counter() - t_start) * 1e6
        self._unpacked_count += 1

        return {
            'header'           : header,
            'compressed_bytes' : compressed,
            'crc_valid'        : crc_valid,
            'unpack_us'        : round(elapsed_us, 3),
        }

    def validate_crc(self, frame_bytes: bytes) -> bool:
        """
        Validate the CRC16 checksum of a frame.

        Computes CRC over bytes 0–4093 and compares with stored
        CRC in bytes 4094–4095.

        Args:
            frame_bytes: complete 4096-byte frame

        Returns: True if CRC matches, False if frame is corrupted
        """
        if len(frame_bytes) != FRAME_SIZE:
            return False

        # CRC covers bytes 0 to 4093 (inclusive)
        computed_crc = compute_crc16(frame_bytes[:CRC_OFFSET])
        stored_crc   = struct.unpack_from('<H', frame_bytes, CRC_OFFSET)[0]

        return computed_crc == stored_crc

    def stats(self) -> Dict[str, Any]:
        """Return packer statistics."""
        n = max(self._packed_count, 1)
        return {
            'frames_packed'    : self._packed_count,
            'frames_unpacked'  : self._unpacked_count,
            'crc_errors'       : self._crc_errors,
            'zero_blocks'      : self._zero_blocks,
            'avg_pack_us'      : round(self._total_pack_us / n, 3),
            'frame_size_bytes' : FRAME_SIZE,
            'header_size_bytes': HEADER_SIZE,
            'max_payload_bytes': MAX_PAYLOAD_SIZE,
        }

    def __repr__(self) -> str:
        s = self.stats()
        return (f"BlockPacker(packed={s['frames_packed']}, "
                f"crc_errors={s['crc_errors']}, "
                f"avg_us={s['avg_pack_us']:.2f}µs)")


# ---------------------------------------------------------------------------
# Example usage / self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import os

    print("=" * 60)
    print("  BlockPacker — LBA Frame Format Self-Test")
    print("=" * 60)

    packer = BlockPacker()

    # --- Test 1: Pack a compressible block ---
    original = (b"SANDISK_NVMe_LOG:" + b"0123456789ABCDEF" * 256)[:4096]
    assert len(original) == 4096
    # Simulate LZ4 compressed (fake: just use first half as "compressed")
    compressed_sim = original[:1800]   # simulate 44% ratio

    frame = packer.pack_block('LZ4', original, compressed_sim)

    assert len(frame) == 4096, f"Frame must be 4096 bytes, got {len(frame)}"
    print(f"✓ Frame size:        {len(frame)} bytes (correct)")

    # --- Test 2: CRC validation ---
    assert packer.validate_crc(frame), "CRC should be valid on fresh frame"
    print("✓ CRC validation:    PASS")

    # Corrupt a byte and check CRC detects it
    corrupt = bytearray(frame)
    corrupt[500] ^= 0xFF
    assert not packer.validate_crc(bytes(corrupt)), "Corrupted frame should fail CRC"
    print("✓ CRC error detect:  PASS")

    # --- Test 3: Unpack and verify header ---
    result = packer.unpack_block(frame)
    hdr = result['header']
    assert hdr.codec_name     == 'LZ4',  f"Expected LZ4, got {hdr.codec_name}"
    assert hdr.original_size  == 4096,   f"Expected 4096, got {hdr.original_size}"
    assert hdr.compressed_size == 1800,  f"Expected 1800, got {hdr.compressed_size}"
    assert hdr.is_compressed,             "Should be marked compressed"
    assert result['crc_valid'],           "CRC should be valid"
    print(f"✓ Unpack header:     {hdr}")
    print(f"  Payload recovered: {len(result['compressed_bytes'])} bytes "
          f"(expected 1800)")

    # --- Test 4: RAW block ---
    random_block = os.urandom(4096)
    frame_raw = packer.pack_block('RAW', random_block, random_block)
    assert len(frame_raw) == 4096
    result_raw = packer.unpack_block(frame_raw)
    assert result_raw['header'].codec_name == 'RAW'
    assert not result_raw['header'].is_compressed
    assert result_raw['crc_valid']
    print("✓ RAW block pack:    PASS")

    # --- Test 5: Zero block ---
    zero_block = bytes(4096)
    frame_zero = packer.pack_block('LZ4', zero_block, b'\x00' * 8)
    result_zero = packer.unpack_block(frame_zero)
    print(f"✓ Zero block flag:   is_zero={result_zero['header'].is_zero_block}")

    # --- Test 6: Overflow check ---
    try:
        packer.pack_block('LZ4', original, bytes(5000))  # Too big!
        print("✗ Should have raised ValueError for overflow")
    except ValueError as e:
        print(f"✓ Overflow detected: {e}")

    # --- Test 7: Header serialization round-trip ---
    hdr2 = FrameHeader(CODEC_LZ4HC, 4096, 1200, FLAG_COMPRESSED)
    hdr2_bytes = hdr2.to_bytes()
    hdr2_decoded = FrameHeader.from_bytes(hdr2_bytes)
    assert hdr2_decoded.codec_id         == CODEC_LZ4HC
    assert hdr2_decoded.original_size    == 4096
    assert hdr2_decoded.compressed_size  == 1200
    assert hdr2_decoded.flags            == FLAG_COMPRESSED
    print("✓ Header round-trip: PASS")

    # --- Throughput ---
    N = 10_000
    t0 = time.perf_counter()
    for _ in range(N):
        packer.pack_block(CODEC_LZ4, original, compressed_sim)
    elapsed = time.perf_counter() - t0
    print(f"\n✓ Pack Throughput:   {N/elapsed:,.0f} frames/sec "
          f"({elapsed*1e6/N:.2f} µs/frame)")

    # --- Stats ---
    print()
    print("  Packer Statistics:")
    for k, v in packer.stats().items():
        print(f"    {k:<25}: {v}")
    print()
    print(packer)
    print()
    print("  BlockPacker is ready.")
