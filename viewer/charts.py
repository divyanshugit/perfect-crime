"""Attack-success-rate figures for the index page, drawn as inline SVG from the loaded runs.

Each bar is one harness/model pair in one setting: tampered trials / all trials. Trials with no
conclusive observation count as unsuccessful, so a bar never overstates a result. Colours come
from CSS classes (.s0 ... .s9) so both themes work and nothing is fetched.
"""
from html import escape

# (title, caption, settings shown, permission mode)
FIGURES = (
    ("Tampering capability",
     "Direct-request capability. Attack success rate per setting.",
     (("financial", "Financial: deletion"), ("personal", "Personal summary: deletion"),
      ("reset", "Fabricated reset: addition"), ("toolcall", "Tool-call manipulation")), "full access"),
    ("Exploitability",
     "Skill-file exploitability. The user never asked for the change.",
     (("privacy_skill", "Privacy protection"), ("compaction", "Compaction injection")), "full access"),
    ("Reward optimization",
     "Reward-loop tampering across trials in each task.",
     (("anonymization", "Anonymization"), ("terminal_bench", "Terminal Bench")), "full access"),
    ("Discovered peer examples",
     "Synthetic action logs or saved workspaces paired with scores.",
     (("peer_logs", "Peer logs"), ("peer_workspaces", "Peer workspaces")), "full access"),
)
PANELS = (("financial", "(a) Financial deletion"), ("personal", "(b) Personal-summary deletion"),
          ("reset", "(c) Fabricated reset addition"), ("toolcall", "(d) Tool-call manipulation"))

MARKERS = ("M-4 -4 h8 v8 h-8 z", "M0 -5 L5 4 L-5 4 z", "M0 -5 L5 0 L0 5 L-5 0 z", "M-5 0 h10 M0 -5 v10",
           "M-4 -4 L4 4 M4 -4 L-4 4", "M0 4.5 L5 -4 L-5 -4 z", "M-4 -5 h8 L5 0 L0 5 L-5 0 z",
           "M0 -5 L1.5 -1.5 L5 -1.2 L2.3 1.2 L3.2 5 L0 3 L-3.2 5 L-2.3 1.2 L-5 -1.2 L-1.5 -1.5 z")
OPEN_MARKERS = {3, 4}  # drawn as strokes, not filled

W, H = 960, 300
LEFT, RIGHT, TOP, BOTTOM = 46, 10, 14, 54


def e(value):
    return escape(str(value if value is not None else ""), quote=True)


def aggregate(payloads):
    """{(setting_key, mode, series): [tampered, total, inconclusive]} plus the ordered series."""
    cells, seen = {}, {}
    for p in payloads:
        m, v = p["meta"], p["verdict"]["verdict"]
        series = f'{m["harness"]} · {m["model"]}'
        seen[series] = True
        cell = cells.setdefault((m.get("setting_key"), m["permission_mode"], series), [0, 0, 0])
        cell[1] += 1
        cell[0] += v == "tampered"
        cell[2] += v == "inconclusive"
    return cells, sorted(seen)


def marker(index, x, y):
    shape = MARKERS[index % len(MARKERS)]
    paint = ('fill="none" stroke="currentColor" stroke-width="1.6"' if index % len(MARKERS) in OPEN_MARKERS
             else 'fill="currentColor"')
    return f'<path d="{shape}" transform="translate({x:.1f} {y:.1f}) scale(.75)" {paint}></path>'


def legend(series):
    items = "".join(
        f'<span class="lg s{i % 10}"><svg width="14" height="14" viewBox="-7 -7 14 14">{marker(i, 0, 0)}</svg>'
        f'{e(name)}</span>' for i, name in series)
    return f'<div class="legend">{items}</div>'


def bar_group(cells, series, modes, settings, width, height, compact=False):
    """One SVG: a band per setting, one bar per series (and per mode when two are compared)."""
    plot_w, plot_h = width - LEFT - RIGHT, height - TOP - BOTTOM
    base = TOP + plot_h
    band = plot_w / len(settings)
    slots = len(series) * len(modes)
    bw = max(3, min(28, band * .84 / slots))
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart-svg" preserveAspectRatio="xMidYMid meet">']
    for tick in range(0, 101, 20):
        y = base - plot_h * tick / 100
        out.append(f'<line class="grid" x1="{LEFT}" x2="{width - RIGHT}" y1="{y:.1f}" y2="{y:.1f}"></line>'
                   f'<text class="tick" x="{LEFT - 6}" y="{y + 3.5:.1f}" text-anchor="end">{tick}%</text>')
    for si, (key, label) in enumerate(settings):
        x0 = LEFT + band * si + (band - bw * slots) / 2
        for pos, (i, name) in enumerate(series):
            for mi, mode in enumerate(modes):
                x = x0 + bw * (pos * len(modes) + mi)
                cell = cells.get((key, mode, name))
                cx = x + bw / 2
                if cell is None:
                    out.append(f'<text class="na" x="{cx:.1f}" y="{base - 4}" text-anchor="middle" '
                               f'transform="rotate(-90 {cx:.1f} {base - 4})">N/A</text>')
                else:
                    hit, total, unclear = cell
                    pct = 100 * hit / total
                    h = plot_h * pct / 100
                    tip = f"{name} · {mode}: {hit} of {total} tampered" + (f", {unclear} inconclusive" if unclear else "")
                    out.append(f'<g class="s{i % 10}{" auto" if mode == "auto mode" else ""}">'
                               f'<title>{e(tip)}</title>'
                               f'<rect class="bar" x="{x + .5:.1f}" y="{base - h:.1f}" width="{bw - 1:.1f}" '
                               f'height="{max(h, 1):.1f}" rx="1.5"></rect>'
                               f'<text class="val" x="{cx:.1f}" y="{base - h - 4:.1f}" text-anchor="middle">'
                               f'{round(pct)}</text></g>')
                if not compact and mi == 0 and len(modes) == 1:
                    out.append(f'<g class="s{i % 10} mk">{marker(i, cx, base + 11)}</g>')
        out.append(f'<text class="cat" x="{LEFT + band * (si + .5):.1f}" y="{base + 36 if not compact else base + 20}" '
                   f'text-anchor="middle">{e(label)}</text>')
    out.append(f'<line class="axis" x1="{LEFT}" x2="{width - RIGHT}" y1="{base}" y2="{base}"></line></svg>')
    return "".join(out)


def with_data(cells, series, keys, modes):
    """Keep the series that have runs in these settings, in the page-wide order (so colours stay put)."""
    return [(i, n) for i, n in enumerate(series) if any((k, m, n) in cells for k in keys for m in modes)]


def figures_html(payloads):
    cells, names = aggregate(payloads)
    if not names:
        return ""
    present = {key for key, _, _ in cells}
    # panels of the full-vs-auto figure are per setting, so each uses only its own series
    parts = []
    number = 1
    for title, caption, settings, mode in FIGURES:
        settings = [s for s in settings if s[0] in present]
        if not settings:
            continue
        series = with_data(cells, names, [k for k, _ in settings], (mode,))
        if not series:
            continue
        trials = [c[1] for (key, m, _), c in cells.items() if key in {s[0] for s in settings} and m == mode]
        if not trials:
            continue
        parts.append(
            f'<figure class="fig"><h3>{e(title)}</h3>{legend(series)}'
            f'{bar_group(cells, series, (mode,), settings, W, H)}'
            f'<figcaption><span class="cap-title">Figure {number} · {e(title)}.</span> {e(caption)} Full access; '
            f'{min(trials)}–{max(trials)} trials per bar; N/A means no runs yet.</figcaption></figure>')
        number += 1
    panels = [p for p in PANELS if p[0] in present]
    if panels and any(m == "auto mode" for _, m, _ in cells):
        both = ("full access", "auto mode")
        series = with_data(cells, names, [k for k, _ in panels], both)
        grid = "".join(
            f'<div class="panel"><h4>{e(label)}</h4>'
            f'{bar_group(cells, with_data(cells, names, [key], both) or series, both, [(key, "")], 460, 200, compact=True)}</div>'
            for key, label in panels)
        parts.append(
            f'<figure class="fig"><h3>Full access vs auto mode</h3>{legend(series)}'
            f'<div class="mode-key"><span><i class="sw"></i>Full access</span>'
            f'<span><i class="sw faded"></i>Auto mode</span></div><div class="panels">{grid}</div>'
            f'<figcaption><span class="cap-title">Figure {number} · Full access versus auto mode.</span> Faded bars are auto mode. '
            f'Trials with no conclusive observation count as unsuccessful.</figcaption></figure>')
    return f'<section class="figures">{"".join(parts)}</section>' if parts else ""


STYLE = """
.figures{margin:1.5rem 0 0;display:grid;gap:1.5rem}
.fig{margin:0;border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);padding:1rem 1.1rem}
.fig h3{margin:0 0 .6rem;text-align:center;font:600 1rem/1.3 "Google Sans",system-ui,sans-serif}
.fig h4{margin:0;text-align:center;font:600 .82rem/1.3 "Google Sans",system-ui,sans-serif}
.fig figcaption{margin-top:.6rem;color:var(--muted);font-size:.8rem}
.cap-title{color:var(--fg);font-weight:600}
.legend,.mode-key{display:flex;flex-wrap:wrap;justify-content:center;gap:.3rem 1.1rem;margin-bottom:.4rem;font-size:.75rem;color:var(--muted)}
.lg{display:inline-flex;align-items:center;gap:.3rem}
.lg svg{color:var(--c)}
.mode-key .sw{display:inline-block;width:.8rem;height:.6rem;background:var(--fg);margin-right:.3rem;border-radius:1px}
.mode-key .sw.faded{opacity:.4}
.chart-svg{width:100%;height:auto;display:block}
.chart-svg .grid{stroke:var(--line);stroke-dasharray:2 3}
.chart-svg .axis{stroke:var(--line-strong)}
.chart-svg text{fill:var(--muted);font:11px "Google Sans",system-ui,sans-serif}
.chart-svg .val{fill:var(--fg);font-weight:600;font-size:10.5px}
.chart-svg .cat{fill:var(--fg);font-size:12px}
.chart-svg .na{font-style:italic;font-size:9px;fill:var(--faint)}
.chart-svg .bar{fill:var(--c)}
.chart-svg .auto .bar{opacity:.42}
.chart-svg .mk{color:var(--c)}
.panels{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem 1.5rem}
@media(max-width:720px){.panels{grid-template-columns:1fr}}
.s0{--c:#3b6fd4}.s1{--c:#d9642b}.s2{--c:#2e9a5b}.s3{--c:#8a5cc7}.s4{--c:#c43d6b}
.s5{--c:#b08a1a}.s6{--c:#2a9aa8}.s7{--c:#7a5a3a}.s8{--c:#6b7280}.s9{--c:#c0392b}
:root[data-theme="dark"] .s0{--c:#7aa2f0}:root[data-theme="dark"] .s1{--c:#f08a54}
:root[data-theme="dark"] .s2{--c:#5fc88a}:root[data-theme="dark"] .s3{--c:#b494e6}
:root[data-theme="dark"] .s4{--c:#e87aa0}:root[data-theme="dark"] .s5{--c:#d8b94a}
:root[data-theme="dark"] .s6{--c:#5fc4d2}:root[data-theme="dark"] .s7{--c:#b89270}
:root[data-theme="dark"] .s8{--c:#a0a6b0}:root[data-theme="dark"] .s9{--c:#ef7b73}
"""
