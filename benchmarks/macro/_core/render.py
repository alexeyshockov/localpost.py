"""Markdown + HTML report renderers, dim-driven.

The HTML page generates filter dropdowns and result columns from
``report.dim_keys`` — every suite gets its own dimensions for free. The
raw report payload is embedded as a JSON ``<script>`` blob, so the page
is the single source of truth (no AJAX, no build step).

No third-party Python dep — just stdlib + a CDN-loaded `Grid.js`_ for sort /
search. Markdown stays as the GitHub-friendly view.

.. _Grid.js: https://gridjs.io/
"""

from __future__ import annotations

import html
import json
from typing import Any

from benchmarks.macro._core.types import Cell, RunReport

GRIDJS_VERSION = "6.2.0"
_CDN_BASE = f"https://cdn.jsdelivr.net/npm/gridjs@{GRIDJS_VERSION}/dist"


def render_markdown(report: RunReport) -> str:
    out: list[str] = []
    out.append(f"# {report.title} results — Python {report.python}\n")
    out.append(f"- Run at: `{report.started_at}`")
    out.append(f"- Host: `{report.host}`")
    out.append(f"- Python: `{report.python}` (`{report.python_version}`)")
    out.append(f"- Duration per cell: `{report.duration_s}s`")
    if report.selection:
        out.append(f"- Selection: {report.selection}")
    out.append("")
    out.append("> Numbers are single-process, single-host. Don't read absolute RPS as gospel —")
    out.append("> what matters is the relative ordering on the same machine in one run.")
    out.append("")

    cells_by_scenario: dict[str, list[Cell]] = {}
    for c in report.cells:
        cells_by_scenario.setdefault(c.scenario, []).append(c)

    for scenario_name, cells in cells_by_scenario.items():
        cells.sort(key=lambda c: c.rps, reverse=True)
        out.append(f"## {scenario_name}\n")
        out.append("| Stack | RPS | p50 (ms) | p90 (ms) | p99 (ms) | expected | other |")
        out.append("|---|---:|---:|---:|---:|---:|---:|")
        out.extend(
            f"| `{c.stack}` | {c.rps:,.0f} | {c.p50_ms:.2f} | {c.p90_ms:.2f} "
            f"| {c.p99_ms:.2f} | {c.status_expected:,} | {c.status_other:,} |"
            for c in cells
        )
        out.append("")
    return "\n".join(out)


def render_html(report: dict[str, Any]) -> str:
    """Render an asdict'd :class:`RunReport` as a single HTML doc."""
    blob = json.dumps(report)
    title = f"{report.get('title', 'Benchmark')} — Python {report.get('python', '?')}"
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

<div class="toolbar" id="toolbar">
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
  const dimKeys = report.dim_keys || [];

  // --- helpers
  function dimVal(c, key) {{ return (c.dims && c.dims[key] != null) ? String(c.dims[key]) : ''; }}
  function distinctSorted(extract) {{
    return Array.from(new Set(cells.map(extract).filter(v => v !== ''))).sort();
  }}

  // --- dynamically build dim filter dropdowns + a scenario filter
  const toolbar = document.getElementById('toolbar');
  function addSelect(id, label, values) {{
    const wrap = document.createElement('label');
    wrap.textContent = label + ' ';
    const sel = document.createElement('select');
    sel.id = id;
    const allOpt = document.createElement('option');
    allOpt.value = '';
    allOpt.textContent = 'all';
    sel.appendChild(allOpt);
    for (const v of values) {{
      const o = document.createElement('option');
      o.value = String(v);
      o.textContent = String(v);
      sel.appendChild(o);
    }}
    wrap.appendChild(sel);
    // Insert before the trailing spacer span(s).
    toolbar.insertBefore(wrap, toolbar.firstChild);
    return sel;
  }}

  // Build dim filters in declared order (insertBefore prepends, so iterate reverse).
  for (const key of [...dimKeys].reverse()) {{
    addSelect('filter-dim-' + key, key, distinctSorted(c => dimVal(c, key)));
  }}
  const scenarios = distinctSorted(c => c.scenario);
  addSelect('filter-scenario', 'scenario', scenarios);

  // --- build one Grid.js table per scenario
  const root = document.getElementById('scenarios');
  const grids = {{}};

  function rowsFor(scenario, filtered) {{
    const list = filtered || cells.filter(c => c.scenario === scenario);
    return list
      .filter(c => c.scenario === scenario)
      .map(c => [
        c.stack,
        ...dimKeys.map(k => dimVal(c, k)),
        Math.round(c.rps), +c.p50_ms.toFixed(2), +c.p90_ms.toFixed(2), +c.p99_ms.toFixed(2),
        c.status_expected, c.status_other,
      ]);
  }}

  const columns = [
    {{ name: 'Stack' }},
    ...dimKeys.map(k => ({{ name: k }})),
    {{ name: 'RPS', sort: {{ compare: (a, b) => a - b }} }},
    {{ name: 'p50 (ms)' }},
    {{ name: 'p90 (ms)' }},
    {{ name: 'p99 (ms)' }},
    {{ name: 'expected' }},
    {{ name: 'other' }},
  ];

  for (const scenario of scenarios) {{
    const section = document.createElement('section');
    section.className = 'scenario';
    section.dataset.scenario = scenario;
    section.innerHTML = '<h2>' + scenario + '</h2><div class="grid"></div>';
    root.appendChild(section);
    const g = new gridjs.Grid({{
      columns: columns,
      data: rowsFor(scenario),
      sort: true,
      search: true,
      pagination: false,
      style: {{ table: {{ 'font-size': '0.85rem' }} }},
    }}).render(section.querySelector('.grid'));
    grids[scenario] = g;
  }}

  // --- top-level filter dropdowns rebuild grids in place
  function currentFilters() {{
    const f = {{ dims: {{}}, scenario: '' }};
    for (const k of dimKeys) {{
      const v = document.getElementById('filter-dim-' + k).value;
      if (v !== '') f.dims[k] = v;
    }}
    f.scenario = document.getElementById('filter-scenario').value;
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
        for (const k of Object.keys(f.dims)) {{
          if (dimVal(c, k) !== f.dims[k]) return false;
        }}
        return true;
      }});
      grids[scenario].updateConfig({{ data: rowsFor(scenario, filtered) }}).forceRender();
    }}
  }}
  for (const k of dimKeys) {{
    document.getElementById('filter-dim-' + k).addEventListener('change', applyFilters);
  }}
  document.getElementById('filter-scenario').addEventListener('change', applyFilters);
}})();
</script>
</body>
</html>
"""
