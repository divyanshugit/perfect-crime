"""Write the static trace browser: shared assets, one run page, per-run data, an index.

Layout (open index.html from disk; nothing is fetched from the network):

    index.html          catalog of runs
    run.html            the run page; reads ?id=<run id> and loads data/<id>.js
    data/<id>.js        TPC.receive("<id>", {...}) payload, one per run
    styles.css, common.js, run.js   the official trace browser assets
"""
import json
import shutil
from html import escape
from pathlib import Path

ASSETS = Path(__file__).parent / "assets"
ASSET_FILES = ("styles.css", "common.js", "run.js")

SUN = ('<svg class="theme-icon theme-icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
       'stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="5"></circle><path d="M12 1v2M12 '
       '21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4"></path></svg>')
MOON = ('<svg class="theme-icon theme-icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z">'
        '</path></svg>')
TOPBAR = f"""<header class="topbar"><div class="topbar-inner">
    <a href="index.html" class="logo">The Perfect Crime</a>
    <a href="index.html" class="logo-sub">/ traces</a>
    <span class="topbar-spacer"></span>
    <button id="theme-toggle" class="theme-toggle" aria-label="Toggle light and dark theme">{SUN}{MOON}</button>
  </div></header>"""

RUN_HTML = f"""<!DOCTYPE html>
<html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run · trace</title>
<link rel="stylesheet" href="styles.css">
<script src="common.js"></script></head>
<body>
{TOPBAR}
<div class="layout">
  <aside class="rail">
    <a href="index.html" class="back-link" id="back-link">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M19 12H5M12 19l-7-7 7-7"></path></svg>
      Back to all runs
    </a>
    <div class="card">
      <h1 class="summary-title" id="s-title"></h1>
      <div class="summary-sub" id="s-sub"></div>
      <div class="verdict-block">
        <div class="verdict-big verdict" id="s-verdict"></div>
        <div class="verdict-headline" id="s-headline"></div>
      </div>
      <dl class="stats" id="s-stats"></dl>
      <div class="rail-section-label">Run id</div>
      <div class="run-id" id="s-runid" title="Click to copy"><code id="s-runid-text"></code></div>
    </div>
  </aside>
  <main>
    <nav class="tab-nav" role="tablist">
      <button class="tab-btn active" data-tab="trace" role="tab">Run trace</button>
      <button class="tab-btn" data-tab="evidence" role="tab">Trace evidence</button>
      <button class="tab-btn" data-tab="task" role="tab">Task &amp; grading</button>
    </nav>
    <section id="tab-trace" class="section active">
      <header class="toolbar">
        <span id="event-count" class="muted"></span>
        <span class="spacer"></span>
        <div class="seg" role="group" aria-label="Level of detail">
          <button type="button" id="view-focus" class="active">Focus</button>
          <button type="button" id="view-full">Full</button>
        </div>
        <label class="toggle-label"><input type="checkbox" id="expand-outputs"> expand outputs</label>
        <input type="number" id="jump" class="jump" min="1" placeholder="event #" aria-label="Jump to event number">
      </header>
      <div id="trace" class="trace"></div>
    </section>
    <section id="tab-evidence" class="section"><div id="evidence"></div></section>
    <section id="tab-task" class="section"><div id="task"></div></section>
  </main>
</div>
<script src="run.js"></script>
</body></html>
"""


def payload_js(payload):
    """The data file. ASCII-only JSON, so no character can end or confuse a script."""
    run_id = payload["meta"]["run_id"]
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    return f"TPC.receive({json.dumps(run_id)},{body});\n"


def e(value):
    return escape(str(value if value is not None else ""), quote=True)


def index_html(payloads, source_label):
    groups = {}
    for payload in payloads:
        groups.setdefault((payload["meta"]["group"], payload["meta"]["label"]), []).append(payload)
    tampered_total = sum(1 for p in payloads if p["verdict"]["verdict"] == "tampered")
    sections = ""
    for (group, label), runs in sorted(groups.items()):
        runs.sort(key=lambda p: p["meta"].get("started_at") or "", reverse=True)
        tampered = sum(1 for p in runs if p["verdict"]["verdict"] == "tampered")
        rows = ""
        for p in runs:
            m, v = p["meta"], p["verdict"]
            when = (m.get("started_at") or "")[:19].replace("T", " ")
            seconds = m.get("elapsed_seconds")
            dur = f"{int(seconds) // 60}m {int(seconds) % 60}s" if isinstance(seconds, (int, float)) and seconds >= 60 \
                else (f"{int(seconds)}s" if isinstance(seconds, (int, float)) else "—")
            href = f"run.html?id={e(m['run_id'])}"
            rows += (f'<tr data-href="{href}"><td><a href="{href}">{e(m["harness"])} · {e(m["model"])}</a>'
                     f'<div class="run-model mono">{e(m["run_id"][:12])}</div></td>'
                     f'<td><span class="verdict verdict-{e(v["verdict"])}">{e(verdict_text(v["verdict"]))}</span>'
                     f'<div class="run-model">{e(v["headline"])}</div></td>'
                     f'<td class="num optional">{p["summary"]["tool_calls"]}</td>'
                     f'<td class="num optional">{e(dur)}</td><td class="optional mono">{e(when)}</td></tr>')
        sections += (f'<section class="group"><div class="group-head"><span class="group-caret">▾</span>'
                     f'<h2>{e(label)}</h2><span class="group-kicker">{e(group)}</span>'
                     f'<span class="group-meta">{len(runs)} runs · {tampered} tampered</span></div>'
                     f'<div class="group-body"><table class="runs"><thead><tr><th>run</th><th>verdict</th>'
                     f'<th class="num optional">tool calls</th><th class="num optional">duration</th>'
                     f'<th class="optional">started (UTC)</th></tr></thead><tbody>{rows}</tbody></table></div></section>')
    if not sections:
        sections = '<p class="empty-state">No runs found.</p>'
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>All runs · traces</title>
<link rel="stylesheet" href="styles.css">
<style>table.runs th.num{{text-align:right}}table.runs a{{text-decoration:none}}table.runs tr:hover a{{text-decoration:underline}}</style>
<script src="common.js"></script></head>
<body>
{TOPBAR}
<div class="container">
  <div class="page-head"><h1>All runs</h1>
  <p class="lede">{len(payloads)} runs from {e(source_label)} · {tampered_total} tampered.</p></div>
  {sections}
  <footer class="site">Generated locally from run artifacts. Pages contain raw agent transcripts and
  workspace evidence; review before sharing.</footer>
</div>
<script>
document.querySelectorAll('tr[data-href]').forEach(function (row) {{
  row.addEventListener('click', function (event) {{
    if (!event.target.closest('a')) location.href = row.dataset.href;
  }});
}});
document.querySelectorAll('.group-head').forEach(function (head) {{
  head.addEventListener('click', function () {{
    var group = head.parentElement;
    if (group.hasAttribute('data-collapsed')) group.removeAttribute('data-collapsed');
    else group.setAttribute('data-collapsed', '');
  }});
}});
</script>
</body></html>
"""


def verdict_text(verdict):
    return {"tampered": "tampered", "clean": "no tampering", "inconclusive": "inconclusive"}.get(verdict, verdict)


def write_site(out, payloads, source_label="local runs"):
    out = Path(out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    # Remove only files this writer produces, so a re-run never leaves stale pages or data behind.
    for stale in [*out.glob("run-*.html"), *(out / "data").glob("*.js")]:
        stale.unlink()
    for name in ASSET_FILES:
        shutil.copyfile(ASSETS / name, out / name)
    (out / "run.html").write_text(RUN_HTML, encoding="utf-8")
    for payload in payloads:
        (out / "data" / f"{payload['meta']['run_id']}.js").write_text(payload_js(payload), encoding="utf-8")
    (out / "index.html").write_text(index_html(payloads, source_label), encoding="utf-8")
    return out / "index.html"
