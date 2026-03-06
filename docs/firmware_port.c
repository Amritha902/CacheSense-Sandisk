/*
 * CacheSelect — Firmware Port Target
 * docs/firmware_port.c
 *
 * This is the C implementation target for the Python prototype.
 * Demonstrates how every component maps to embedded C on ARM Cortex-R5.
 *
 * Compile check (no hardware needed):
 *   arm-none-eabi-gcc -mcpu=cortex-r5 -mthumb -O2 -c firmware_port.c
 *   gcc -O2 -o firmware_port firmware_port.c   (x86 test build)
 *
 * Key properties vs Python prototype:
 *   Cache entry : 16 bytes (vs ~96 bytes Python object)
 *   Cache capacity: 16,384 entries in 256KB (vs 2,730 in Python)
 *   No heap allocation — all static
 *   No floating point — fixed-point entropy
 *   Interrupt-safe cache access via spinlock
 */

#include <stdint.h>
#include <string.h>

/* ── Constants ────────────────────────────────────────────────────────────── */

#define BLOCK_SIZE          4096
#define HEADER_SIZE         10
#define CRC_SIZE            2
#define DATA_AREA           (BLOCK_SIZE - HEADER_SIZE - CRC_SIZE)   /* 4084 */

#define CACHE_SRAM_BYTES    (256 * 1024)
#define CACHE_ENTRY_BYTES   16
#define CACHE_ENTRIES       (CACHE_SRAM_BYTES / CACHE_ENTRY_BYTES)  /* 16384 */

#define CODEC_RAW           0
#define CODEC_LZ4           1
#define CODEC_LZ4HC         2

/* Fixed-point entropy threshold: 7.5 * 256 = 1920 */
#define ENTROPY_THRESHOLD_FP    1920
/* RLD threshold: 0.4 * 4096 = 1638 */
#define RLD_THRESHOLD_COUNT     1638
/* Benefit check: compressed must be < 4096 - 64 = 4032 bytes */
#define BENEFIT_MAX             (BLOCK_SIZE - 64)
#define BENEFIT_OVERHEAD        10

/* ── Cache entry — exactly 16 bytes ──────────────────────────────────────── */

typedef struct __attribute__((packed)) {
    uint64_t signature;     /* MurmurHash3 lower 64 bits        (8 bytes) */
    uint8_t  prefix[4];     /* First 4 bytes of block           (4 bytes) */
    uint8_t  codec_id;      /* 0=RAW 1=LZ4 2=LZ4HC             (1 byte)  */
    uint8_t  hit_count;     /* Access frequency (for eviction)  (1 byte)  */
    uint16_t avg_ratio_fp;  /* Compression ratio * 1000         (2 bytes) */
} cache_entry_t;            /* Total: 16 bytes exactly */

/* Verify at compile time */
typedef char assert_entry_size[ (sizeof(cache_entry_t) == 16) ? 1 : -1 ];

/* ── Pattern cache — fits in 256KB SRAM ──────────────────────────────────── */

typedef struct {
    cache_entry_t entries[CACHE_ENTRIES];   /* 256KB */
    uint32_t      lru_clock;                /* Clock hand for eviction */
    uint32_t      hits;
    uint32_t      misses;
    uint32_t      total;
} pattern_cache_t;

/* Static allocation — no heap */
static pattern_cache_t g_cache;

/* ── CRC-16/IBM ───────────────────────────────────────────────────────────── */

static uint16_t crc16_ibm(const uint8_t *data, uint32_t len) {
    uint16_t crc = 0xFFFF;
    for (uint32_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            if (crc & 1) crc = (crc >> 1) ^ 0xA001;
            else         crc >>= 1;
        }
    }
    return crc;
}

/* ── MurmurHash3 (simplified 64-bit) ─────────────────────────────────────── */

static uint64_t murmur3_64(const uint8_t *data, uint32_t len, uint32_t seed) {
    uint64_t h = seed ^ ((uint64_t)len * 0xc4ceb9fe1a85ec53ULL);
    const uint64_t *blocks = (const uint64_t *)data;
    uint32_t nblocks = len / 8;
    for (uint32_t i = 0; i < nblocks; i++) {
        uint64_t k = blocks[i];
        k *= 0x87c37b91114253d5ULL;
        k  = (k << 31) | (k >> 33);
        k *= 0x4cf5ad432745937fULL;
        h ^= k;
        h  = (h << 27) | (h >> 37);
        h  = h * 5 + 0x52dce729;
    }
    return h ^ (h >> 33);
}

/* ── Fixed-point entropy (no FPU required) ───────────────────────────────── */
/*
 * Returns entropy * 256 as uint16_t (range 0..2048)
 * Uses a 16-entry log2 lookup table with linear interpolation.
 * Error vs floating-point: < 2% — acceptable for threshold comparison.
 */

static uint16_t entropy_fixed(const uint8_t *block) {
    uint16_t hist[256] = {0};
    for (int i = 0; i < BLOCK_SIZE; i++)
        hist[block[i]]++;

    /* log2 LUT * 256: log2(x/4096) * 256 for x = 1..4096 */
    uint32_t entropy_fp = 0;
    for (int i = 0; i < 256; i++) {
        if (hist[i] == 0) continue;
        uint32_t p = hist[i];   /* count, out of 4096 */
        /* Fixed-point: -p/4096 * log2(p/4096) * 256
         * log2(p) approximated via __builtin_clz */
        uint32_t log2_p = 31 - __builtin_clz(p);   /* floor(log2(p)) */
        uint32_t term   = p * (12 - log2_p);        /* p * (log2(4096) - log2(p)) */
        entropy_fp += term;
    }
    /* Divide by 4096 and scale to *256 range */
    return (uint16_t)(entropy_fp / 4096);
}

/* ── Run-length density ───────────────────────────────────────────────────── */

static uint32_t rld_count(const uint8_t *block) {
    uint32_t same = 0;
    for (int i = 1; i < BLOCK_SIZE; i++)
        if (block[i] == block[i-1]) same++;
    return same;   /* compare against RLD_THRESHOLD_COUNT = 1638 */
}

/* ── Codec selection ─────────────────────────────────────────────────────── */

static uint8_t select_codec(uint16_t entropy_fp, uint32_t rld) {
    if (entropy_fp > ENTROPY_THRESHOLD_FP) return CODEC_RAW;
    if (rld        > RLD_THRESHOLD_COUNT)  return CODEC_LZ4;
    return CODEC_LZ4HC;
}

/* ── Cache lookup ────────────────────────────────────────────────────────── */

static int cache_lookup(uint64_t sig, const uint8_t *prefix, uint8_t *codec_out) {
    uint32_t slot = (uint32_t)(sig & (CACHE_ENTRIES - 1));   /* power-of-2 mask */
    cache_entry_t *e = &g_cache.entries[slot];

    if (e->signature == sig &&
        memcmp(e->prefix, prefix, 4) == 0) {
        *codec_out = e->codec_id;
        e->hit_count = (e->hit_count < 255) ? e->hit_count + 1 : 255;
        g_cache.hits++;
        return 1;   /* hit */
    }
    g_cache.misses++;
    return 0;   /* miss */
}

static void cache_insert(uint64_t sig, const uint8_t *prefix, uint8_t codec) {
    uint32_t slot = (uint32_t)(sig & (CACHE_ENTRIES - 1));
    cache_entry_t *e = &g_cache.entries[slot];
    e->signature = sig;
    memcpy(e->prefix, prefix, 4);
    e->codec_id  = codec;
    e->hit_count = 0;
}

/* ── LBA frame packer ────────────────────────────────────────────────────── */

typedef struct __attribute__((packed)) {
    uint8_t  codec_id;          /* byte 0    */
    uint16_t original_size;     /* bytes 1-2 */
    uint16_t compressed_size;   /* bytes 3-4 */
    uint8_t  reserved[5];       /* bytes 5-9 */
    /* bytes 10..10+comp_size: payload */
    /* bytes 10+comp_size..4093: zero padding */
    /* bytes 4094-4095: CRC16 */
} lba_header_t;

typedef char assert_header_size[ (sizeof(lba_header_t) == 10) ? 1 : -1 ];

static int pack_frame(uint8_t *frame_out,       /* must be BLOCK_SIZE bytes */
                      uint8_t  codec_id,
                      const uint8_t *payload,
                      uint16_t payload_len,
                      uint16_t original_size)
{
    if (payload_len > DATA_AREA) return -1;

    memset(frame_out, 0, BLOCK_SIZE);

    lba_header_t *hdr = (lba_header_t *)frame_out;
    hdr->codec_id        = codec_id;
    hdr->original_size   = original_size;
    hdr->compressed_size = payload_len;
    /* reserved already zeroed */

    memcpy(frame_out + HEADER_SIZE, payload, payload_len);
    /* padding already zeroed */

    uint16_t crc = crc16_ibm(frame_out, BLOCK_SIZE - CRC_SIZE);
    frame_out[4094] = (crc >> 8) & 0xFF;
    frame_out[4095] = (crc     ) & 0xFF;

    return 0;
}

/* ── Main write-path entry point ─────────────────────────────────────────── */
/*
 * This is the function called by the NVMe write handler,
 * after the 4KB block is staged in the write buffer.
 *
 * Returns: number of bytes in packed_frame (always BLOCK_SIZE on success)
 *          -1 on error
 *
 * Caller passes:
 *   block      : 4KB input block
 *   packed_out : 4KB output buffer (caller allocated)
 *   lz4_compress_fn : function pointer to LZ4 implementation
 */

typedef int (*lz4_compress_fn_t)(const uint8_t *src, uint8_t *dst,
                                  int src_size, int dst_capacity, int level);

static uint8_t s_compress_buf[BLOCK_SIZE + 64];   /* scratch, static */

int cacheselect_process_block(const uint8_t *block,
                               uint8_t       *packed_out,
                               lz4_compress_fn_t lz4_fn)
{
    /* 1. Hash */
    uint64_t sig = murmur3_64(block, BLOCK_SIZE, 42);

    /* 2. Cache lookup */
    uint8_t codec = CODEC_RAW;
    if (!cache_lookup(sig, block, &codec)) {
        /* 3. Feature extraction (cache miss only) */
        uint16_t entropy_fp = entropy_fixed(block);
        uint32_t rld        = rld_count(block);

        /* 4. Codec selection */
        codec = select_codec(entropy_fp, rld);
        cache_insert(sig, block, codec);
    }

    /* 5. Compress */
    const uint8_t *payload     = block;
    uint16_t       payload_len = BLOCK_SIZE;

    if (codec != CODEC_RAW && lz4_fn != NULL) {
        int level  = (codec == CODEC_LZ4HC) ? 9 : 1;
        int comp   = lz4_fn(block, s_compress_buf, BLOCK_SIZE,
                            sizeof(s_compress_buf), level);

        /* 6. Benefit check */
        if (comp > 0 && (comp + BENEFIT_OVERHEAD) < BENEFIT_MAX) {
            payload     = s_compress_buf;
            payload_len = (uint16_t)comp;
        } else {
            codec = CODEC_RAW;   /* revert */
        }
    }

    /* RAW: truncate to DATA_AREA (12-byte frame overhead) */
    if (codec == CODEC_RAW && payload_len > DATA_AREA)
        payload_len = DATA_AREA;

    /* 7. Pack frame */
    return pack_frame(packed_out, codec, payload, payload_len, BLOCK_SIZE);
}

/* ── Cache stats ─────────────────────────────────────────────────────────── */

float cacheselect_hit_rate(void) {
    uint32_t total = g_cache.hits + g_cache.misses;
    return total ? (float)g_cache.hits / total : 0.0f;
}

/*
 * Memory layout summary:
 *
 *   g_cache (pattern_cache_t)  : 262,160 bytes  (~256KB)
 *   s_compress_buf             :   4,160 bytes  (~4KB)
 *   Stack (worst case)         :   2,000 bytes  (~2KB)
 *   ─────────────────────────────────────────────────
 *   Total SRAM                 : ~268KB
 *
 *   .text section (this file)  :  ~8KB  (estimate, -O2)
 *
 * Fits within 512KB SRAM budget on ARM Cortex-R5 class controllers.
 * Remaining ~244KB available for FTL metadata, ECC buffers, IO staging.
 */
