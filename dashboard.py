"""
=============================================================================
MODULE: dashboard.py
DESCRIPTION: Real-time SSD Compression Engine Monitoring Dashboard

Flask-based web dashboard that visualizes pipeline metrics in real-time.
Reads from metrics.json (written by FirmwarePipeline) and auto-refreshes.

FEATURES:
  - Blocks processed counter
  - Cache hit rate gauge
  - Entropy distribution histogram
  - Codec usage donut chart
  - Compression ratio trend
  - Per-stage latency bar chart
  - Throughput (MB/s) gauge

NOTE: This module requires Flask. If not available, it falls back to
      generating a standalone HTML dashboard file from metrics.json.

USAGE:
    # Option A: Flask server (install: pip install flask)
    python dashboard.py --serve --port 5000

    # Option B: Generate static HTML report (no Flask needed)
    python dashboard.py --static --metrics output/metrics.json
=============================================================================
"""

import os
import sys
import json
import time
import argparse
from typing import Dict, Any, Optional

# ---------------------------------------------------------------------------
# HTML Dashboard Template
# ---------------------------------------------------------------------------

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SSD Firmware Compression Engine — Live Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --accent: #58a6ff;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --purple: #a371f7;
    --text: #c9d1d9;
    --muted: #8b949e;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
    padding: 20px;
  }
  h1 {
    font-size: 22px;
    color: var(--accent);
    margin-bottom: 6px;
    letter-spacing: 0.02em;
  }
  .subtitle {
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 24px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 20px;
  }
  .grid-2 {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 16px;
    margin-bottom: 20px;
  }
  .grid-3 {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 20px;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px;
  }
  .card h3 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 10px;
  }
  .metric-big {
    font-size: 36px;
    font-weight: 700;
    color: var(--accent);
    line-height: 1;
  }
  .metric-unit {
    font-size: 13px;
    color: var(--muted);
    margin-top: 4px;
  }
  .metric-sub {
    font-size: 12px;
    color: var(--muted);
    margin-top: 6px;
  }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    margin-right: 4px;
    margin-top: 4px;
  }
  .badge-green  { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-blue   { background: rgba(88,166,255,0.15); color: var(--accent); }
  .badge-yellow { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .badge-red    { background: rgba(248,81,73,0.15); color: var(--red); }
  .badge-purple { background: rgba(163,113,247,0.15); color: var(--purple); }
  .progress-bar {
    height: 6px;
    background: var(--border);
    border-radius: 3px;
    margin-top: 8px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.5s ease;
  }
  canvas { max-height: 220px; }
  .stage-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
  }
  .stage-row:last-child { border-bottom: none; }
  .stage-name { color: var(--text); width: 140px; }
  .stage-bar-wrap { flex: 1; margin: 0 12px; }
  .stage-bar-bg {
    height: 8px;
    background: var(--border);
    border-radius: 4px;
    overflow: hidden;
  }
  .stage-bar-fill {
    height: 100%;
    border-radius: 4px;
    background: var(--accent);
    transition: width 0.5s;
  }
  .stage-val { color: var(--muted); width: 70px; text-align: right; }
  .status-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--muted);
    margin-top: 6px;
  }
  .status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 1.5s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .footer {
    text-align: center;
    color: var(--muted);
    font-size: 11px;
    margin-top: 24px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
  }
</style>
</head>
<body>

<h1>⚡ SSD Firmware Compression Engine</h1>
<div class="subtitle">
  Real-time pipeline monitoring dashboard &nbsp;|&nbsp;
  <span class="status-bar" style="display:inline-flex">
    <span class="status-dot" id="statusDot"></span>
    <span id="statusText">Loading...</span>
  </span>
</div>

<!-- KPI Cards Row -->
<div class="grid" id="kpiRow">
  <div class="card">
    <h3>Blocks Processed</h3>
    <div class="metric-big" id="blocksProcessed">—</div>
    <div class="metric-unit">4KB LBA blocks</div>
    <div class="metric-sub" id="throughputSub">—</div>
  </div>
  <div class="card">
    <h3>Cache Hit Rate</h3>
    <div class="metric-big" id="cacheHitRate">—</div>
    <div class="metric-unit">pattern cache</div>
    <div class="progress-bar">
      <div class="progress-fill" id="cacheHitBar"
           style="background: var(--green); width: 0%"></div>
    </div>
    <div class="metric-sub" id="cacheSub">—</div>
  </div>
  <div class="card">
    <h3>Compression Ratio</h3>
    <div class="metric-big" id="compressionRatio">—</div>
    <div class="metric-unit">compressed / original</div>
    <div class="progress-bar">
      <div class="progress-fill" id="compRatioBar"
           style="background: var(--purple); width: 0%"></div>
    </div>
    <div class="metric-sub" id="spaceSavingSub">—</div>
  </div>
  <div class="card">
    <h3>Avg Latency</h3>
    <div class="metric-big" id="avgLatency">—</div>
    <div class="metric-unit">µs per block</div>
    <div class="metric-sub" id="latencySub">—</div>
  </div>
</div>

<!-- Charts Row 1 -->
<div class="grid-2">
  <div class="card">
    <h3>Codec Distribution</h3>
    <canvas id="codecChart"></canvas>
  </div>
  <div class="card">
    <h3>Compression Ratio by Codec</h3>
    <canvas id="ratioChart"></canvas>
  </div>
</div>

<!-- Per-Stage Latency -->
<div class="card" style="margin-bottom:20px">
  <h3>Pipeline Stage Latency (µs)</h3>
  <div id="stageRows"></div>
</div>

<!-- Workload Breakdown -->
<div class="card" style="margin-bottom:20px">
  <h3>Workload Tags</h3>
  <div id="workloadBadges" style="margin-top: 8px;"></div>
</div>

<div class="footer">
  SSD Firmware Compression Engine · SanDisk Hackathon Prototype ·
  Data from <code>metrics.json</code> ·
  Auto-refresh: <span id="refreshCount">0</span>
</div>

<script>
// ---------------------------------------------------------------------------
// Chart initialization
// ---------------------------------------------------------------------------

const COLORS = {
  RAW:    '#8b949e',
  LZ4:    '#58a6ff',
  LZ4HC:  '#a371f7',
  SKIP:   '#3fb950',
};

let codecChart = null;
let ratioChart  = null;
let refreshCount = 0;

function initCharts() {
  const codecCtx = document.getElementById('codecChart').getContext('2d');
  codecChart = new Chart(codecCtx, {
    type: 'doughnut',
    data: {
      labels: ['RAW', 'LZ4', 'LZ4HC', 'SKIP'],
      datasets: [{
        data: [0, 0, 0, 0],
        backgroundColor: [COLORS.RAW, COLORS.LZ4, COLORS.LZ4HC, COLORS.SKIP],
        borderColor: '#0d1117',
        borderWidth: 3,
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: '#c9d1d9', font: { size: 12 } }
        }
      }
    }
  });

  const ratioCtx = document.getElementById('ratioChart').getContext('2d');
  ratioChart = new Chart(ratioCtx, {
    type: 'bar',
    data: {
      labels: ['RAW', 'LZ4', 'LZ4HC'],
      datasets: [{
        label: 'Avg Compression Ratio',
        data: [1.0, 0.0, 0.0],
        backgroundColor: [COLORS.RAW, COLORS.LZ4, COLORS.LZ4HC],
        borderRadius: 4,
      }]
    },
    options: {
      responsive: true,
      scales: {
        y: {
          min: 0, max: 1.1,
          ticks: { color: '#8b949e' },
          grid:  { color: '#21262d' },
        },
        x: { ticks: { color: '#8b949e' }, grid: { display: false } }
      },
      plugins: {
        legend: { display: false }
      }
    }
  });
}

// ---------------------------------------------------------------------------
// Data loading & update
// ---------------------------------------------------------------------------

function fmt(v, dec=2) {
  if (v === undefined || v === null) return '—';
  return Number(v).toFixed(dec);
}

function fmtPct(v) { return fmt(v * 100, 1) + '%'; }

async function loadMetrics() {
  try {
    // In Flask mode: fetch from /api/metrics
    // In static mode: data is embedded inline
    let metrics;
    if (window.STATIC_METRICS) {
      metrics = window.STATIC_METRICS;
    } else {
      const resp = await fetch('/api/metrics?t=' + Date.now());
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      metrics = await resp.json();
    }
    updateDashboard(metrics);
    document.getElementById('statusDot').style.background = 'var(--green)';
    document.getElementById('statusText').textContent =
      'Live · Last updated: ' + new Date().toLocaleTimeString();
    refreshCount++;
    document.getElementById('refreshCount').textContent = refreshCount;
  } catch (e) {
    document.getElementById('statusDot').style.background = 'var(--red)';
    document.getElementById('statusText').textContent = 'Error: ' + e.message;
  }
}

function updateDashboard(m) {
  const p    = m.pipeline      || {};
  const comp = m.compression   || {};
  const cache= m.cache         || {};
  const sel  = m.selector      || {};
  const tim  = m.timing        || {};

  // --- KPI Cards ---
  document.getElementById('blocksProcessed').textContent =
    (p.total_blocks || 0).toLocaleString();
  document.getElementById('throughputSub').textContent =
    fmt(tim.throughput_blocks_s || 0, 0) + ' blocks/sec · ' +
    fmt((tim.throughput_blocks_s || 0) * 4096 / 1e6, 1) + ' MB/s';

  const hitRate = cache.hit_rate || 0;
  document.getElementById('cacheHitRate').textContent = fmtPct(hitRate);
  document.getElementById('cacheHitBar').style.width  = (hitRate * 100) + '%';
  document.getElementById('cacheSub').textContent =
    (cache.hits || 0).toLocaleString() + ' hits · ' +
    (cache.misses || 0).toLocaleString() + ' misses · ' +
    (cache.evictions || 0).toLocaleString() + ' evictions';

  const ratio = comp.overall_ratio || 1.0;
  document.getElementById('compressionRatio').textContent = fmt(ratio, 4);
  document.getElementById('compRatioBar').style.width = ((1 - ratio) * 100) + '%';
  document.getElementById('spaceSavingSub').textContent =
    fmt(comp.space_saving_pct || 0, 2) + '% space saved';

  const avgUs = tim.avg_total_us || 0;
  document.getElementById('avgLatency').textContent = fmt(avgUs, 1);
  document.getElementById('latencySub').textContent =
    'Budget: ' + (tim.total_budget_us || 224) + ' µs → ' +
    fmt(tim.overall_budget_pct || 0, 0) + '% used';

  // --- Codec Donut Chart ---
  const dist = (sel.codec_distribution_pct || {});
  codecChart.data.datasets[0].data = [
    dist.RAW   || 0,
    dist.LZ4   || 0,
    dist.LZ4HC || 0,
    dist.SKIP  || 0,
  ];
  codecChart.update('none');

  // --- Ratio Bar Chart ---
  const perCodec = comp.per_codec || {};
  ratioChart.data.datasets[0].data = [
    (perCodec.RAW   || {}).avg_ratio || 1.0,
    (perCodec.LZ4   || {}).avg_ratio || 0,
    (perCodec.LZ4HC || {}).avg_ratio || 0,
  ];
  ratioChart.update('none');

  // --- Stage Latency ---
  const stages  = tim.per_stage || {};
  const maxUs   = Math.max(...Object.values(stages).map(s => s.avg_us || 0), 1);
  const stageEl = document.getElementById('stageRows');
  stageEl.innerHTML = '';

  const stageNames = [
    'hashing', 'cache_lookup', 'feature_extract',
    'codec_select', 'compression', 'block_packing'
  ];
  for (const name of stageNames) {
    const s = stages[name] || {};
    if (!s.count) continue;
    const pct = Math.min(100, (s.avg_us / maxUs) * 100);
    const budgetColor = (s.budget_pct || 0) > 80 ? 'var(--yellow)' :
                        (s.budget_pct || 0) > 100 ? 'var(--red)' : 'var(--accent)';
    stageEl.innerHTML += `
      <div class="stage-row">
        <span class="stage-name">${name}</span>
        <div class="stage-bar-wrap">
          <div class="stage-bar-bg">
            <div class="stage-bar-fill"
                 style="width:${pct}%; background:${budgetColor}"></div>
          </div>
        </div>
        <span class="stage-val">${fmt(s.avg_us, 2)} µs</span>
      </div>`;
  }

  // --- Workload Badges ---
  const badgeEl = document.getElementById('workloadBadges');
  const badgeClasses = ['badge-blue', 'badge-green', 'badge-yellow', 'badge-purple'];
  const codecs = ['RAW', 'LZ4', 'LZ4HC', 'SKIP'];
  badgeEl.innerHTML = '';
  codecs.forEach((codec, i) => {
    const pct = dist[codec] || 0;
    if (pct > 0) {
      badgeEl.innerHTML +=
        `<span class="badge ${badgeClasses[i]}">${codec}: ${fmt(pct,1)}%</span>`;
    }
  });
  badgeEl.innerHTML += `
    <span class="badge badge-blue">Blocks: ${(p.total_blocks||0).toLocaleString()}</span>
    <span class="badge badge-green">Cache: ${fmtPct(hitRate)}</span>
    <span class="badge badge-yellow">Saving: ${fmt(comp.space_saving_pct||0,1)}%</span>
    <span class="badge badge-purple">Tput: ${fmt((tim.throughput_blocks_s||0)*4096/1e6,1)} MB/s</span>
  `;
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------
initCharts();
loadMetrics();

// Only auto-refresh in Flask mode
if (!window.STATIC_METRICS) {
  setInterval(loadMetrics, 1000);
}
</script>
</body>
</html>
'''


# ---------------------------------------------------------------------------
# Static HTML Generator (no Flask required)
# ---------------------------------------------------------------------------

def generate_static_dashboard(metrics: Dict[str, Any],
                               output_path: str = 'dashboard.html') -> str:
    """
    Generate a standalone HTML dashboard from a metrics dict.

    No server required — opens directly in a browser.

    Args:
        metrics     : dict from FirmwarePipeline.get_metrics()
        output_path : path to write HTML file

    Returns: path to written HTML file
    """
    import json as _json

    # Inject metrics as a static JS variable
    metrics_js = f"window.STATIC_METRICS = {_json.dumps(metrics, indent=2)};"

    html = DASHBOARD_HTML.replace(
        '// Bootstrap',
        f'{metrics_js}\n// Bootstrap'
    )

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    return output_path


# ---------------------------------------------------------------------------
# Flask App (optional — requires pip install flask)
# ---------------------------------------------------------------------------

def create_flask_app(metrics_path: str):
    """Create Flask app that serves live metrics from metrics.json."""
    try:
        from flask import Flask, jsonify, send_file
        import threading
    except ImportError:
        print("Flask not installed. Run: pip install flask")
        print("Falling back to static HTML generation.")
        return None

    app = Flask(__name__)

    @app.route('/')
    def index():
        return DASHBOARD_HTML, 200, {'Content-Type': 'text/html'}

    @app.route('/api/metrics')
    def get_metrics():
        try:
            with open(metrics_path) as f:
                data = json.load(f)
            return jsonify(data)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return app


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='SSD Compression Engine Dashboard')
    parser.add_argument('--serve',   action='store_true',
                        help='Start Flask dev server')
    parser.add_argument('--static',  action='store_true',
                        help='Generate static HTML dashboard')
    parser.add_argument('--metrics', default=None,
                        help='Path to metrics.json (default: output/metrics.json)')
    parser.add_argument('--port',    type=int, default=5000,
                        help='Flask server port (default: 5000)')
    parser.add_argument('--output',  default=None,
                        help='Output HTML path for static mode')
    args = parser.parse_args()

    # Default metrics path
    if args.metrics is None:
        _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        args.metrics = os.path.join(_base, 'output', 'metrics.json')

    if args.serve:
        print(f"  Starting Flask dashboard server on port {args.port}...")
        print(f"  Reading metrics from: {args.metrics}")
        print(f"  Open: http://localhost:{args.port}")
        app = create_flask_app(args.metrics)
        if app:
            app.run(host='0.0.0.0', port=args.port, debug=False)

    elif args.static or True:
        # Default behavior: generate static HTML
        out = args.output or os.path.join(
            os.path.dirname(args.metrics), 'dashboard.html')

        if os.path.exists(args.metrics):
            with open(args.metrics) as f:
                metrics = json.load(f)
        else:
            # Generate demo metrics if no file exists
            print(f"  metrics.json not found at {args.metrics}")
            print("  Generating dashboard with demo data...")
            metrics = {
                'pipeline': {'total_blocks': 1000, 'total_errors': 0,
                             'frames_written': 1000, 'output_size_bytes': 4096000},
                'compression': {
                    'overall_ratio': 0.523, 'space_saving_pct': 47.7,
                    'total_bytes_in': 4096000, 'total_bytes_out': 2142208,
                    'per_codec': {
                        'RAW':   {'count': 400, 'avg_ratio': 1.0, 'avg_us': 0.5},
                        'LZ4':   {'count': 350, 'avg_ratio': 0.42, 'avg_us': 45.0},
                        'LZ4HC': {'count': 250, 'avg_ratio': 0.31, 'avg_us': 120.0},
                    }
                },
                'cache': {
                    'hit_rate': 0.623, 'miss_rate': 0.377, 'hits': 623, 'misses': 377,
                    'evictions': 12, 'entry_count': 988, 'max_entries': 16384,
                },
                'selector': {
                    'total_decisions': 1000,
                    'codec_distribution_pct': {
                        'SKIP': 5.0, 'RAW': 40.0, 'LZ4': 35.0, 'LZ4HC': 20.0,
                    }
                },
                'timing': {
                    'avg_total_us': 87.4,
                    'total_budget_us': 224,
                    'overall_budget_pct': 39.0,
                    'throughput_blocks_s': 11450,
                    'per_stage': {
                        'hashing':       {'count': 1000, 'avg_us': 1.2, 'min_us': 0.8, 'max_us': 3.1, 'std_us': 0.3, 'budget_pct': 60},
                        'cache_lookup':  {'count': 1000, 'avg_us': 0.6, 'min_us': 0.3, 'max_us': 1.2, 'std_us': 0.2, 'budget_pct': 60},
                        'feature_extract':{'count': 377, 'avg_us': 8.9, 'min_us': 5.2, 'max_us': 18.4, 'std_us': 2.1, 'budget_pct': 59},
                        'codec_select':  {'count': 377, 'avg_us': 0.4, 'min_us': 0.2, 'max_us': 0.9, 'std_us': 0.1, 'budget_pct': 40},
                        'compression':   {'count': 1000, 'avg_us': 71.2, 'min_us': 0.2, 'max_us': 195.0, 'std_us': 45.0, 'budget_pct': 36},
                        'block_packing': {'count': 1000, 'avg_us': 2.1, 'min_us': 1.2, 'max_us': 4.8, 'std_us': 0.6, 'budget_pct': 42},
                    }
                },
            }

        path = generate_static_dashboard(metrics, out)
        print(f"\n  ✓ Dashboard generated: {path}")
        print(f"  Open this file in your browser to view the dashboard.")
