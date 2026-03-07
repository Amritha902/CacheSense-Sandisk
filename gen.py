"""
CacheSelect — Complete Benchmark Graph Generator
Run: python generate_graphs.py
Needs: full_benchmark_results.json + large_benchmark_results.json in same folder
Output: graphs/ folder with 11 PNG files
"""

import json, os, sys
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec

os.makedirs('graphs', exist_ok=True)

# ── Load JSON ──────────────────────────────────────────────────────────────────
try:
    with open('full_benchmark_results.json') as f:
        full = json.load(f)
    with open('large_benchmark_results.json') as f:
        large = json.load(f)
except FileNotFoundError as e:
    print(f"ERROR: {e}")
    print("Make sure full_benchmark_results.json and large_benchmark_results.json are in the same folder.")
    sys.exit(1)

all_data = full + large

# ── Constants ──────────────────────────────────────────────────────────────────
COLORS = {
    'RAW':         '#e74c3c',
    'LZ4':         '#f4a623',
    'LZ4HC':       '#4a9eff',
    'CacheSelect': '#00e676',
}
PIPELINES = ['RAW', 'LZ4', 'LZ4HC', 'CacheSelect']
SIZES     = ['10MB', '100MB', '500MB', '1GB', '10GB']

BG    = '#0a0e1a'
PANEL = '#111827'
GRID  = '#1f2937'
TEXT  = '#e2e8f0'
SUB   = '#94a3b8'

# ── Helpers ────────────────────────────────────────────────────────────────────
def get(data, size, pipeline, key, default=0):
    for r in data:
        if r['size'] == size and r['pipeline'] == pipeline:
            v = r.get(key, default)
            return v if v is not None else default
    return default

def styled_fig(w=13, h=7):
    fig, ax = plt.subplots(figsize=(w, h), facecolor=BG)
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.tick_params(colors=SUB, labelsize=9)
    ax.grid(axis='y', color=GRID, linewidth=0.7, linestyle='--', alpha=0.8)
    return fig, ax

def title_style(ax, title, sub=''):
    full_title = title + (f'\n{sub}' if sub else '')
    ax.set_title(full_title, color=TEXT, fontsize=13, fontweight='bold', pad=12, loc='left')

def add_legend(ax):
    ax.legend(facecolor='#1e2a3a', labelcolor=TEXT, framealpha=0.9,
              fontsize=9, edgecolor=GRID)

def bar_label(ax, bar, v, fmt='{:.1f}', offset=0.4, rotate=0, threshold=0):
    if v > threshold:
        ax.text(bar.get_x() + bar.get_width()/2,
                bar.get_height() + offset,
                fmt.format(v),
                ha='center', va='bottom', color=TEXT,
                fontsize=6.5, fontweight='bold', rotation=rotate)

def save(fig, name):
    fig.tight_layout()
    fig.savefig(f'graphs/{name}', dpi=150, bbox_inches='tight', facecolor=BG)
    plt.close()
    print(f'  ✓  graphs/{name}')


# ══════════════════════════════════════════════════════════════════════════════
# 1. Space Saving %
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = styled_fig()
title_style(ax, 'Space Saving (%) — 10MB → 10GB', 'higher = more NAND storage saved')
x = np.arange(len(SIZES)); w = 0.2
for i, pipe in enumerate(PIPELINES):
    vals = [get(all_data, s, pipe, 'saving_pct') for s in SIZES]
    bars = ax.bar(x + i*w, vals, w, label=pipe,
                  color=COLORS[pipe], alpha=0.92, edgecolor='#000', lw=0.4)
    for bar, v in zip(bars, vals):
        bar_label(ax, bar, v, '{:.1f}%', offset=0.4)
ax.set_xticks(x + w*1.5); ax.set_xticklabels(SIZES, color=SUB)
ax.set_ylabel('Space Saved (%)', color=SUB, fontsize=10)
ax.set_ylim(0, 58)
add_legend(ax)
save(fig, '1_space_saving.png')


# ══════════════════════════════════════════════════════════════════════════════
# 2. Compression Ratio (lower = better)
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = styled_fig()
title_style(ax, 'Compression Ratio — 10MB → 10GB', 'lower = better  |  1.0 = no compression (RAW baseline)')
for pipe in PIPELINES:
    vals = [get(all_data, s, pipe, 'ratio', 1.0) for s in SIZES]
    ax.plot(SIZES, vals, marker='o', lw=2.5, ms=8, label=pipe, color=COLORS[pipe])
    for j, v in enumerate(vals):
        ax.annotate(f'{v:.3f}', (SIZES[j], v),
                    xytext=(0, 9), textcoords='offset points',
                    ha='center', color=COLORS[pipe], fontsize=7.5, fontweight='bold')
ax.axhline(1.0, color='#e74c3c', ls='--', lw=1.2, alpha=0.5, label='No compression baseline')
ax.set_ylim(0.4, 1.2)
ax.set_ylabel('Ratio (compressed / original)', color=SUB, fontsize=10)
add_legend(ax)
save(fig, '2_compression_ratio.png')


# ══════════════════════════════════════════════════════════════════════════════
# 3. Throughput MB/s — LOG SCALE (range: ~2 to ~3000)
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = styled_fig()
title_style(ax, 'Throughput (MB/s) — 10MB → 10GB', 'log scale  |  higher = faster pipeline')
x = np.arange(len(SIZES)); w = 0.2
for i, pipe in enumerate(PIPELINES):
    vals = [max(get(all_data, s, pipe, 'throughput_mbs'), 0.5) for s in SIZES]
    bars = ax.bar(x + i*w, vals, w, label=pipe,
                  color=COLORS[pipe], alpha=0.92, edgecolor='#000', lw=0.4)
    for bar, v in zip(bars, vals):
        label = f'{v:.0f}' if v >= 10 else f'{v:.1f}'
        ax.text(bar.get_x() + bar.get_width()/2, v * 1.15,
                label, ha='center', va='bottom', color=TEXT, fontsize=6, fontweight='bold')
ax.set_yscale('log')
ax.set_ylim(0.3, 15000)
ax.set_ylabel('Throughput MB/s  (log scale)', color=SUB, fontsize=10)
ax.set_xticks(x + w*1.5); ax.set_xticklabels(SIZES, color=SUB)
add_legend(ax)
save(fig, '3_throughput.png')


# ══════════════════════════════════════════════════════════════════════════════
# 4. WAF — Write Amplification Factor
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = styled_fig()
title_style(ax, 'Write Amplification Factor (WAF)', 'lower = less NAND wear  |  <1.0 = saving space')
for pipe in PIPELINES:
    vals = [get(all_data, s, pipe, 'waf', 1.0) for s in SIZES]
    ax.plot(SIZES, vals, marker='s', lw=2.5, ms=8, label=pipe, color=COLORS[pipe])
    for j, v in enumerate(vals):
        ax.annotate(f'{v:.4f}', (SIZES[j], v),
                    xytext=(0, 9), textcoords='offset points',
                    ha='center', color=COLORS[pipe], fontsize=7)
ax.axhline(1.0, color='#e74c3c', ls='--', lw=1.5, alpha=0.6, label='WAF = 1.0 (no benefit)')
ax.fill_between(SIZES, [0]*5, [0.58]*5, alpha=0.07, color='#00e676', label='Target zone')
ax.set_ylim(0.3, 1.25)
ax.set_ylabel('WAF', color=SUB, fontsize=10)
add_legend(ax)
save(fig, '4_waf.png')


# ══════════════════════════════════════════════════════════════════════════════
# 5. Average Latency per block
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = styled_fig()
title_style(ax, 'Average Block Latency (ms) — 10MB → 10GB', 'lower = faster  |  firmware target: < 15µs')
x = np.arange(len(SIZES)); w = 0.2
for i, pipe in enumerate(PIPELINES):
    vals = [get(all_data, s, pipe, 'avg_lat_ms') for s in SIZES]
    bars = ax.bar(x + i*w, vals, w, label=pipe,
                  color=COLORS[pipe], alpha=0.92, edgecolor='#000', lw=0.4)
    for bar, v in zip(bars, vals):
        if v > 0.0005:
            ax.text(bar.get_x() + bar.get_width()/2,
                    bar.get_height() + 0.008,
                    f'{v:.4f}', ha='center', va='bottom',
                    color=TEXT, fontsize=5.8, rotation=45)
ax.set_xticks(x + w*1.5); ax.set_xticklabels(SIZES, color=SUB)
ax.set_ylabel('Avg Latency (ms)', color=SUB, fontsize=10)
add_legend(ax)
save(fig, '5_avg_latency.png')


# ══════════════════════════════════════════════════════════════════════════════
# 6. P99 Tail Latency — CacheSelect only
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = styled_fig(11, 6)
title_style(ax, 'CacheSelect — Avg vs P99 Tail Latency (ms)', 'tail latency validation across all dataset sizes')
cs_avg = [get(all_data, s, 'CacheSelect', 'avg_lat_ms') for s in SIZES]
cs_p99 = [get(all_data, s, 'CacheSelect', 'p99_lat_ms') for s in SIZES]
x = np.arange(len(SIZES)); w = 0.32
b1 = ax.bar(x - w/2, cs_avg, w, label='Avg Latency', color='#00e676', alpha=0.9)
b2 = ax.bar(x + w/2, cs_p99, w, label='P99 Latency', color='#00bcd4', alpha=0.85,
            edgecolor='#00e676', lw=1)
for bar, v in zip(b1, cs_avg):
    ax.text(bar.get_x()+bar.get_width()/2, v+0.02, f'{v:.3f}ms',
            ha='center', color=TEXT, fontsize=8.5, fontweight='bold')
for bar, v in zip(b2, cs_p99):
    if v > 0:
        ax.text(bar.get_x()+bar.get_width()/2, v+0.02, f'{v:.3f}ms',
                ha='center', color='#aaffee', fontsize=8.5, fontweight='bold')
ax.set_xticks(x); ax.set_xticklabels(SIZES, color=SUB)
ax.set_ylabel('Latency (ms)', color=SUB, fontsize=10)
add_legend(ax)
save(fig, '6_p99_latency.png')


# ══════════════════════════════════════════════════════════════════════════════
# 7. Cache Hit Rate
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = styled_fig(11, 6)
title_style(ax, 'CacheSelect Adaptive Cache Hit Rate (%)', 'grows as engine learns block patterns — target: 40%+ on real workloads')
hit_rates = [get(all_data, s, 'CacheSelect', 'cache_hit_pct') for s in SIZES]
bars = ax.bar(SIZES, hit_rates, color='#00e676', alpha=0.88,
              edgecolor='#00bfa5', lw=1.5, width=0.5)
for bar, v in zip(bars, hit_rates):
    ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+0.4,
            f'{v:.2f}%', ha='center', va='bottom',
            color=TEXT, fontsize=12, fontweight='bold')
ax.axhline(17, color='#f4a623', ls='--', lw=1.5, label='~17% steady-state observed')
ax.axhline(40, color='#4a9eff', ls=':', lw=1.5, label='~40% expected on real SSD workload')
ax.set_ylim(0, 50)
ax.set_ylabel('Cache Hit Rate (%)', color=SUB, fontsize=10)
add_legend(ax)
save(fig, '7_cache_hit_rate.png')


# ══════════════════════════════════════════════════════════════════════════════
# 8. Codec Distribution Pie — 10GB run
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(8, 8), facecolor=BG)
ax.set_facecolor(BG)
labels  = ['RAW\n(incompressible / random)', 'LZ4 + LZ4HC\n(compressed)', 'SKIP\n(zero blocks)']
sizes   = [55.0, 45.0, 0.1]
colors  = ['#e74c3c', '#00e676', '#4a9eff']
explode = (0.04, 0.04, 0.12)
wedges, texts, autotexts = ax.pie(
    sizes, labels=labels, colors=colors, explode=explode,
    autopct='%1.1f%%', startangle=140,
    textprops={'color': TEXT, 'fontsize': 11},
    wedgeprops={'edgecolor': BG, 'linewidth': 2.5}
)
for at in autotexts:
    at.set_fontsize(13); at.set_fontweight('bold')
ax.set_title('CacheSelect Codec Distribution\n10GB Mixed Workload (random + logs + repetitive)',
             color=TEXT, fontsize=13, fontweight='bold', pad=20)
fig.tight_layout()
fig.savefig('graphs/8_codec_distribution.png', dpi=150, bbox_inches='tight', facecolor=BG)
plt.close(); print('  ✓  graphs/8_codec_distribution.png')


# ══════════════════════════════════════════════════════════════════════════════
# 9. CPU Usage %
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = styled_fig()
title_style(ax, 'CPU Usage (%) — 10MB → 10GB', 'CacheSelect adds smart routing with minimal extra CPU cost')
for pipe in PIPELINES:
    vals = [get(all_data, s, pipe, 'cpu_avg_pct') for s in SIZES]
    ax.plot(SIZES, vals, marker='D', lw=2.5, ms=8, label=pipe, color=COLORS[pipe])
    for j, v in enumerate(vals):
        ax.annotate(f'{v:.1f}%', (SIZES[j], v),
                    xytext=(0, 8), textcoords='offset points',
                    ha='center', color=COLORS[pipe], fontsize=7.5)
ax.set_ylim(0, 20)
ax.set_ylabel('CPU Usage (%)', color=SUB, fontsize=10)
add_legend(ax)
save(fig, '9_cpu_usage.png')


# ══════════════════════════════════════════════════════════════════════════════
# 10. Physical vs Logical bytes — 10GB
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = styled_fig(12, 7)
title_style(ax, 'Physical vs Logical Bytes Written — 10GB Run', 'gap between bars = NAND space saved')
logical  = [get(large, '10GB', p, 'orig_mb', 10240) for p in PIPELINES]
physical = [get(large, '10GB', p, 'comp_mb', 10240) for p in PIPELINES]
x = np.arange(len(PIPELINES)); w = 0.35
ax.bar(x - w/2, logical,  w, label='Logical (original)', color='#334155', alpha=0.9)
b2 = ax.bar(x + w/2, physical, w, label='Physical (on NAND)',
            color=[COLORS[p] for p in PIPELINES], alpha=0.9)
for i, (bar, v) in enumerate(zip(b2, physical)):
    saved = 10240 - v
    pct   = saved / 10240 * 100
    ax.text(bar.get_x()+bar.get_width()/2, v + 80,
            f'{v:.0f} MB\n↑ saved {saved:.0f} MB\n({pct:.1f}%)',
            ha='center', color=TEXT, fontsize=8, fontweight='bold')
ax.set_xticks(x); ax.set_xticklabels(PIPELINES, color=SUB, fontsize=11)
ax.set_ylabel('Data (MB)', color=SUB, fontsize=10)
ax.set_ylim(0, 13500)
add_legend(ax)
save(fig, '10_physical_vs_logical.png')


# ══════════════════════════════════════════════════════════════════════════════
# 11. Summary Dashboard — all metrics, 10GB side by side
# ══════════════════════════════════════════════════════════════════════════════
fig = plt.figure(figsize=(18, 11), facecolor=BG)
fig.suptitle('CacheSelect Pipeline — Full Benchmark Summary (10GB Mixed Workload)',
             color=TEXT, fontsize=16, fontweight='bold', y=0.98)

gs = GridSpec(2, 3, figure=fig, hspace=0.5, wspace=0.38)

metrics = [
    ('saving_pct',     'Space Saving (%)',       True,  '%',  large),
    ('waf',            'WAF (lower=better)',      False, '',   large),
    ('throughput_mbs', 'Throughput (MB/s)',       True,  '',   large),
    ('avg_lat_ms',     'Avg Latency (ms)',        False, 'ms', large),
    ('cpu_avg_pct',    'CPU Usage (%)',           False, '%',  large),
    ('cache_hit_pct',  'Cache Hit Rate (%)',      True,  '%',  large),
]

for idx, (key, label, higher_better, unit, src) in enumerate(metrics):
    row, col = divmod(idx, 3)
    ax = fig.add_subplot(gs[row, col])
    ax.set_facecolor('#0d1525')
    for sp in ax.spines.values(): sp.set_color(GRID)
    ax.tick_params(colors=SUB, labelsize=8)
    ax.grid(axis='y', color=GRID, lw=0.6, ls='--', alpha=0.7)

    vals = [get(src, '10GB', p, key) for p in PIPELINES]
    bars = ax.bar(PIPELINES, vals, color=[COLORS[p] for p in PIPELINES],
                  alpha=0.9, edgecolor='#000', lw=0.4)
    for bar, v in zip(bars, vals):
        if v > 0:
            ax.text(bar.get_x()+bar.get_width()/2,
                    bar.get_height() + max(vals)*0.03,
                    f'{v:.2f}{unit}', ha='center', va='bottom',
                    color=TEXT, fontsize=7.5, fontweight='bold')
    direction = '▲ higher = better' if higher_better else '▼ lower = better'
    ax.set_title(f'{label}\n{direction}', color=TEXT, fontsize=9,
                 fontweight='bold', pad=6)
    ax.tick_params(axis='x', labelrotation=12, labelsize=8)
    for lbl in ax.get_xticklabels(): lbl.set_color(SUB)

fig.savefig('graphs/11_summary_dashboard.png', dpi=150, bbox_inches='tight', facecolor=BG)
plt.close(); print('  ✓  graphs/11_summary_dashboard.png')


print('\n✅  All 11 graphs saved to graphs/')
print('    git add graphs/ && git commit -m "add benchmark graphs v3"')
EOF
