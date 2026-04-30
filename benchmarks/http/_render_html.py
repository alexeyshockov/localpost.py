"""HTML report renderer for the HTTP benchmark.

Self-contained ``RESULTS.html`` next to ``RESULTS.md``. Renders one section
per scenario, with sortable tables and per-dimension dropdown filters. The
raw report is embedded as a JSON ``<script>`` blob, so the page is the
single source of truth (no AJAX, no build step).

No third-party Python dep — just stdlib + a CDN-loaded `Grid.js`_ for sort /
search. Markdown stays as the GitHub-friendly view; this is for deeper
slicing on a developer's laptop.

.. _Grid.js: https://gridjs.io/
"""

from __future__ import annotations

import html
import json
from typing import Any

GRIDJS_VERSION = "6.2.0"
_CDN_BASE = f"https://cdn.jsdelivr.net/npm/gridjs@{GRIDJS_VERSION}/dist"


def render_html(report: dict[str, Any]) -> str:
    """Render a `RunReport` payload (already `asdict`ed) as a single HTML doc."""
    blob = json.dumps(report)
    title = f"HTTP benchmark — Python {report.get('python', '?')}"
    return _TEMPLATE.format(
        title=html.escape(title),
        gridjs_css=f"{_CDN_BASE}/theme/mermaid.min.css",
        gridjs_js=f"{_CDN_BASE}/gridjs.umd.js",
        report_json=blob.replace("</", "<\\/"),
        # Header values
        started_at=html.escape(str(report.get("started_at", ""))),
        host=html.escape(str(report.get("host", ""))),
        python=html.escape(str(report.get("python", ""))),
        python_version=html.escape(str(report.get("python_version", ""))),
        duration_s=int(report.get("duration_s", 0)),
        selection=html.escape(str(report.get("selection") or "all")),
    )


# A single-string template avoids accidental .format collisions inside
# inline JS / CSS — only the tagged ``{...}`` placeholders below are
# substituted; literal braces in JS are doubled (``{{`` / ``}}``).
_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="{gridjs_css}">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
          margin: 1.5rem; color: #1a1a1a; }}
  h1 {{ margin: 0 0 0.5rem 0; font-size: 1.5rem; }}
  .meta {{ color: #555; font-size: 0.9rem; margin-bottom: 1rem; }}
  .meta code {{ background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }}
  .toolbar {{ position: sticky; top: 0; background: #fff; z-index: 10;
              padding: 0.6rem 0; border-bottom: 1px solid #ddd; margin-bottom: 1rem;
              display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; }}
  .toolbar label {{ font-size: 0.85rem; color: #333; }}
  .toolbar select {{ font-size: 0.85rem; padding: 2px 4px; }}
  .scenario {{ margin-bottom: 2rem; }}
  .scenario h2 {{ font-size: 1.15rem; margin: 0 0 0.5rem 0; }}
  .gridjs-search {{ float: right; }}
  .note {{ color: #777; font-size: 0.85rem; margin: 0.5rem 0 1rem 0; }}
  .dim {{ display: inline-block; padding: 1px 6px; background: #eef; border-radius: 3px;
          font-size: 0.8rem; margin-right: 4px; color: #225; }}
</style>
</head>
<body>

<h1>{title}</h1>
<div class="meta">
  Run at <code>{started_at}</code> · Host <code>{host}</code> ·
  Python <code>{python}</code> (<code>{python_version}</code>) ·
  Duration per cell <code>{duration_s}s</code> · Selection <code>{selection}</code>
</div>

<div class="note">
  Numbers are single-process, single-host. What matters is the relative ordering
  on the same machine in one run. Use the dropdowns to slice; click any column
  header to sort.
</div>

<div class="toolbar">
  <label>app
    <select id="filter-app"><option value="">all</option></select>
  </label>
  <label>backend
    <select id="filter-backend"><option value="">all</option></select>
  </label>
  <label>selectors
    <select id="filter-selectors"><option value="">all</option></select>
  </label>
  <label>pool
    <select id="filter-pool">
      <option value="">all</option><option value="true">on</option><option value="false">off</option>
    </select>
  </label>
  <label>scenario
    <select id="filter-scenario"><option value="">all</option></select>
  </label>
  <span style="flex:1"></span>
  <span style="font-size:0.8rem;color:#666">filters AND together</span>
</div>

<div id="scenarios"></div>

<script src="{gridjs_js}"></script>
<script id="report-data" type="application/json">{report_json}</script>
<script>
(function () {{
  const report = JSON.parse(document.getElementById('report-data').textContent);
  const cells = report.cells || [];

  // --- populate filter dropdowns from observed values
  function distinctSorted(field) {{
    return Array.from(new Set(cells.map(c => c[field]))).sort();
  }}
  function fillSelect(id, values, render) {{
    const el = document.getElementById(id);
    for (const v of values) {{
      const o = document.createElement('option');
      o.value = String(v);
      o.textContent = render ? render(v) : String(v);
      el.appendChild(o);
    }}
  }}
  fillSelect('filter-app', distinctSorted('app'));
  fillSelect('filter-backend', distinctSorted('backend'));
  fillSelect('filter-selectors', distinctSorted('selectors'));
  fillSelect('filter-scenario', distinctSorted('scenario'));

  // --- build one Grid.js table per scenario
  const scenarios = distinctSorted('scenario');
  const root = document.getElementById('scenarios');
  const grids = {{}};

  function rowsFor(scenario) {{
    return cells
      .filter(c => c.scenario === scenario)
      .map(c => [
        c.stack, c.app, c.backend, c.selectors, c.pool ? 'on' : 'off',
        Math.round(c.rps), +c.p50_ms.toFixed(2), +c.p90_ms.toFixed(2), +c.p99_ms.toFixed(2),
        c.status_2xx, c.status_other,
      ]);
  }}

  for (const scenario of scenarios) {{
    const section = document.createElement('section');
    section.className = 'scenario';
    section.dataset.scenario = scenario;
    section.innerHTML = '<h2>' + scenario + '</h2><div class="grid"></div>';
    root.appendChild(section);
    const g = new gridjs.Grid({{
      columns: [
        {{ name: 'Stack' }},
        {{ name: 'app' }},
        {{ name: 'backend' }},
        {{ name: 'sel' }},
        {{ name: 'pool' }},
        {{ name: 'RPS', sort: {{ compare: (a, b) => a - b }} }},
        {{ name: 'p50 (ms)' }},
        {{ name: 'p90 (ms)' }},
        {{ name: 'p99 (ms)' }},
        {{ name: '2xx' }},
        {{ name: 'non-2xx' }},
      ],
      data: rowsFor(scenario),
      sort: true,
      search: true,
      pagination: false,
      style: {{ table: {{ 'font-size': '0.85rem' }} }},
    }}).render(section.querySelector('.grid'));
    grids[scenario] = g;
  }}

  // --- top-level filter dropdowns rebuild grids in place
  const filterIds = ['app', 'backend', 'selectors', 'pool', 'scenario'];
  function currentFilters() {{
    const f = {{}};
    for (const k of filterIds) {{
      const v = document.getElementById('filter-' + k).value;
      if (v !== '') f[k] = v;
    }}
    return f;
  }}
  function applyFilters() {{
    const f = currentFilters();
    for (const scenario of scenarios) {{
      const section = root.querySelector('section[data-scenario="' + scenario + '"]');
      if (f.scenario && f.scenario !== scenario) {{
        section.style.display = 'none';
        continue;
      }}
      section.style.display = '';
      const filtered = cells.filter(c => {{
        if (c.scenario !== scenario) return false;
        if (f.app && c.app !== f.app) return false;
        if (f.backend && c.backend !== f.backend) return false;
        if (f.selectors && String(c.selectors) !== f.selectors) return false;
        if (f.pool && (c.pool ? 'true' : 'false') !== f.pool) return false;
        return true;
      }});
      grids[scenario].updateConfig({{
        data: filtered.map(c => [
          c.stack, c.app, c.backend, c.selectors, c.pool ? 'on' : 'off',
          Math.round(c.rps), +c.p50_ms.toFixed(2), +c.p90_ms.toFixed(2), +c.p99_ms.toFixed(2),
          c.status_2xx, c.status_other,
        ]),
      }}).forceRender();
    }}
  }}
  for (const k of filterIds) {{
    document.getElementById('filter-' + k).addEventListener('change', applyFilters);
  }}
}})();
</script>
</body>
</html>
"""
