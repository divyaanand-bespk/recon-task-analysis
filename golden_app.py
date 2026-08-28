#!/usr/bin/env python3
"""Render the GOLDEN SET -- the pilot tasks the diversity-first selector picks --
as a standalone, shareable HTML page.

Separate deliverable from render_report.py (the operational readiness report).
This page answers one question: *which tasks does the strategy pick, and why
each at that position?*  It is presentation only; it never touches Horizon.

    python3 golden_app.py /tmp/pilot-full.json -o /tmp/golden-set.html

The input is re-read on every run and never cached: the analysis file is still
growing, so a stale copy would quietly present an out-of-date golden set.

The ordering itself is imported from render_report.diverse_order() so that this
page always presents the shipped policy rather than a private copy of it.  The
per-pick *explanation* needs the runner-up at each step, which diverse_order()
does not expose, so the six-term key is re-stated here and then SELF-CHECKED:
if replaying it does not reproduce the imported order exactly, the page says so
and falls back to axis-only rationale.  A silent divergence is not possible.
"""
from __future__ import annotations

import argparse, collections, html, importlib.util, json, sys
from pathlib import Path

TARGET = 50          # pilot size we are trying to fill
REPO_CAP = 3         # per-repository cap the pilot rules impose
HORIZON = "https://horizon.bespokelabs.ai/tasks/"

# The three variety axes, in the priority order the policy states.
AX = (("lang_key", "language"), ("shape", "shape"), ("repo_key", "repository"))

TERMS = [
    ("Language", "least-used language", "diversity"),
    ("Shape", "least-used shape", "diversity"),
    ("Repository", "least-used repository", "diversity"),
    ("Rollouts", "most eligible rollouts", "quality"),
    ("Turns", "highest median turns", "quality"),
    ("Rank", "existing rank (exact ties only)", "tiebreak"),
]

# --------------------------------------------------------------------------- io

def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def load_strategy(script_dir: Path):
    """Import diverse_order()/blockers() from the strategy owner's module.

    Returns (module, note). A failed import is reported on the page rather than
    silently swapped for a local copy.
    """
    path = script_dir / "render_report.py"
    if not path.exists():
        return None, f"render_report.py not found at {path}"
    try:
        spec = importlib.util.spec_from_file_location("_render_report", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "diverse_order"):
            return None, "render_report.py has no diverse_order()"
        return mod, None
    except Exception as exc:                                  # pragma: no cover
        return None, f"could not import render_report.py: {exc.__class__.__name__}: {exc}"


def num(r: dict, field: str):
    """A number or None -- never a default. Missing must read as missing."""
    v = r.get(field)
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


def fmt(v, dash="—"):
    return dash if v is None else f"{v:,}" if isinstance(v, int) else str(v)


def axis(r: dict, field: str) -> str:
    return (r.get(field) or "unknown") or "unknown"


def short_repo(v) -> str:
    return (v or "—").replace("github.com/", "")


def norm_reason(text: str) -> str:
    """Group blocker strings that differ only by the measured value.

    "R/P gate 9 (needs >20)" and "R/P gate 14 (needs >20)" are the same gate;
    left ungrouped they fragment into one row per task and the table stops being
    readable at 78 or 300 tasks. Only the two numeric gates are rewritten -
    everything else is reported in the words the gate used.
    """
    if text.startswith("R/P gate"):
        return "R/P gate below the required >20"
    if text.startswith("solved "):
        return "solved 2 or more of its eligible rollouts (needs <2)"
    return text


# ------------------------------------------------------------------ selection

def key_tuple(r, lang_used, shape_used, repo_used):
    """The shipped six-term lexicographic key, re-stated for explanation only."""
    return (lang_used[axis(r, "lang_key")],
            shape_used[axis(r, "shape")],
            repo_used[axis(r, "repo_key")],
            -(num(r, "rollouts_n") or 0),
            -(num(r, "turns_median") or 0),
            r.get("_rank", 0))


def replay(ordered: list[dict]) -> tuple[list[dict], bool]:
    """Replay the key over the imported order to recover, per pick, the key
    tuple and the runner-up it beat. Returns (picks, key_reproduces_order).

    Rows are tracked by identity, never by task_id: names and ids can collide
    across source and delivered copies of a task, and matching on id would drop
    every row sharing one.
    """
    rows = list(ordered)
    for i, r in enumerate(rows):
        r.setdefault("_rank", i)
    lang_used, shape_used, repo_used = (collections.Counter() for _ in range(3))
    picks, faithful = [], True
    remaining = list(rows)
    for chosen in rows:
        scored = sorted(remaining, key=lambda r: key_tuple(r, lang_used, shape_used, repo_used))
        if scored[0] is not chosen:
            faithful = False
        k = key_tuple(chosen, lang_used, shape_used, repo_used)
        runner = next((r for r in scored if r is not chosen), None)
        rk = key_tuple(runner, lang_used, shape_used, repo_used) if runner else None
        decided = next((j for j in range(6) if rk and k[j] != rk[j]), None)
        picks.append({"row": chosen, "key": k, "runner": runner, "runner_key": rk,
                      "decided": decided})
        lang_used[axis(chosen, "lang_key")] += 1
        shape_used[axis(chosen, "shape")] += 1
        repo_used[axis(chosen, "repo_key")] += 1
        remaining = [r for r in remaining if r is not chosen]
    return picks, faithful


def local_order(rows: list[dict]) -> tuple[list[dict], dict]:
    """Fallback ordering, used only if render_report.py cannot be imported.

    Annotates rows with the same field names the strategy module uses so the
    rest of this page is indifferent to which produced the order.
    """
    pool = [dict(r) for r in rows if r.get("fit") == "YES"]
    for i, r in enumerate(pool):
        r["_rank"] = i
    used = {f: collections.Counter() for f, _ in AX}
    first_repeat = {f: None for f, _ in AX}
    forced_n = {f: 0 for f, _ in AX}
    traded_n = {f: 0 for f, _ in AX}
    distinct = {f: len({axis(r, f) for r in pool}) for f, _ in AX}
    unknown_n = {f: sum(1 for r in pool if axis(r, f) == "unknown") for f, _ in AX}
    out = []
    while pool:
        pick = min(pool, key=lambda r: key_tuple(
            r, used["lang_key"], used["shape"], used["repo_key"]))
        seq = len(out) + 1
        for field, _ in AX:
            val = axis(pick, field)
            fresh = used[field][val] == 0
            pick[f"_{field}_fresh"] = fresh
            pick[f"_{field}_unknown"] = val == "unknown"
            if fresh:
                pick[f"_{field}_why"] = "fresh"
            else:
                avoidable = any(used[field][axis(c, field)] == 0 for c in pool if c is not pick)
                pick[f"_{field}_why"] = "traded" if avoidable else "forced"
                (traded_n if avoidable else forced_n)[field] += 1
                if first_repeat[field] is None:
                    first_repeat[field] = seq
            used[field][val] += 1
            pick[f"_{field}_n"] = used[field][val]
        pick["_fresh"] = all(pick[f"_{f}_fresh"] for f, _ in AX)
        pick["_traded"] = any(pick[f"_{f}_why"] == "traded" for f, _ in AX)
        out.append(pick)
        pool = [r for r in pool if r is not pick]
    axes = []
    for field, label in AX:
        d = distinct[field]
        got = first_repeat[field]
        axes.append({"field": field, "label": label.capitalize(), "distinct": d, "floor": d + 1,
                     "first_repeat": got, "forced": forced_n[field], "traded": traded_n[field],
                     "unknown": unknown_n[field],
                     "optimal": got is None or got >= d + 1, "degenerate": d <= 1})
    spent = [a for a in axes if a["first_repeat"]]
    diag = {"axes": axes, "total": len(out), "duplicate_ids": [],
            "traded_picks": sum(1 for g in out if g["_traded"]),
            "first_spent": min(spent, key=lambda a: a["first_repeat"]) if spent else None,
            "all_fresh_through": next((i for i, g in enumerate(out, 1) if not g["_fresh"]),
                                      len(out) + 1) - 1}
    return out, diag


# ------------------------------------------------------------------ rationale

WHY_WORD = {"fresh": "opened", "traded": "traded", "forced": "forced"}


def axis_state(row: dict) -> list[tuple]:
    """(field, label, value, why, nth, unknown) for each of the three axes.

    ``why`` is the strategy module's own verdict: ``fresh`` (this pick opened a
    value nobody had used), ``forced`` (it repeated because no remaining task
    carried an unused value there) or ``traded`` (an unused value existed, but
    taking it would have spent a higher-priority axis).
    """
    out = []
    for field, label in AX:
        val = axis(row, field)
        shown = short_repo(val) if field == "repo_key" else val
        why = row.get(f"_{field}_why")
        if why is None:                       # older/simpler annotation shape
            why = "fresh" if row.get(f"_{field}_fresh") else "forced"
        out.append((field, label, shown, why, row.get(f"_{field}_n"),
                    bool(row.get(f"_{field}_unknown")) or val == "unknown"))
    return out


def rationale(pick: dict, position: int, faithful: bool) -> tuple[str, str]:
    """(what this pick did to the axes, what decided it over the runner-up)."""
    st = axis_state(pick["row"])
    opened = [s for s in st if s[3] == "fresh"]
    forced = [s for s in st if s[3] == "forced"]
    traded = [s for s in st if s[3] == "traded"]

    def phrase(items, with_nth=False):
        parts = [f"{lab} <b>{esc(val)}</b>" + (f" (use #{nth})" if with_nth and nth else "")
                 for _f, lab, val, _w, nth, _u in items]
        return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]

    bits = []
    if position == 1:
        bits.append(f"First pick, so nothing is spent yet: it opens {phrase(opened)}.")
    elif opened:
        bits.append(("Fully fresh — opens " if not (forced or traded) else "Opens ")
                    + phrase(opened) + ("." if forced or traded else " without repeating anything."))
    if forced:
        ax = "that axis" if len(forced) == 1 else "those axes"
        bits.append(f"It repeats {phrase(forced, True)}, and had no choice: no task left in the "
                    f"pool carried an unused value on {ax}.")
    if traded:
        it = "an unused value was" if len(traded) == 1 else "unused values were"
        bits.append(f"It repeats {phrase(traded, True)} by choice — {it} available, but taking "
                    f"{'it' if len(traded) == 1 else 'them'} would have spent a higher-priority "
                    f"axis, so the policy paid the cheaper repeat.")
    if not opened and not forced and not traded:
        bits.append("No axis annotation was recorded for this pick.")
    why = " ".join(bits)

    runner, k, rk, j = pick["runner"], pick["key"], pick["runner_key"], pick["decided"]
    if not faithful:
        decided = ""
    elif runner is None:
        decided = "Last task left in the eligible pool — no contest to settle."
    elif j is None:
        decided = ""
    else:
        name = TERMS[j][0]
        a, b = k[j], rk[j]
        if j < 3:
            detail = f"{AX[j][1]} used {a}× vs {b}× at this point"
        elif j == 3:
            detail = f"{-a:,} eligible rollouts vs {-b:,}"
        elif j == 4:
            detail = f"median {-a:,} turns vs {-b:,}"
        else:
            detail = "identical on every measured term; the earlier row wins"
        decided = (f'Beat <span class="rname">{esc(runner.get("name"))}</span> at '
                   f"<b>term {j+1} · {name}</b> — {detail}.")
    return why, decided


# ---------------------------------------------------------------------- charts

SERIES = 6  # validated categorical slots; anything past this folds into "Other"


UNKNOWN_LABEL = "not recorded"


def fold(counter: collections.Counter):
    """Split a category count into the segments a chart may actually draw.

    Only SERIES validated hues exist, so a ninth category is never given a
    generated colour: the tail folds into one "other" segment that names what it
    contains. The absence bucket is pulled out separately -- "not recorded" is
    not a value, and colouring it like one would make missing data look like
    variety.
    """
    unknown = counter.get("unknown", 0)
    known = [(k, v) for k, v in counter.items() if k != "unknown"]
    known.sort(key=lambda kv: (-kv[1], kv[0]))          # deterministic
    head, tail = known[:SERIES], known[SERIES:]
    colors = {k: f"var(--s{i+1})" for i, k in enumerate(sorted(k for k, _ in head))}
    segs = [(k, v, colors[k], None) for k, v in head]
    if tail:
        segs.append((f"other ({len(tail)} more)", sum(v for _, v in tail), "var(--s-other)",
                     ", ".join(f"{k} {v}" for k, v in tail)))
    if unknown:
        segs.append((UNKNOWN_LABEL, unknown, "var(--s-unknown)",
                     "these tasks carry no value on this axis"))
    return segs


def stat_tile(label, value, sub, wide=False):
    return (f'<div class="tile{" wide" if wide else ""}"><span class="t-l">{esc(label)}</span>'
            f'<span class="t-v">{esc(value)}</span><span class="t-s">{sub}</span></div>')


def composition(title, counter: collections.Counter, total: int, note: str) -> str:
    """Stacked composition bar -- or a stat tile when there is only one category,
    because a one-segment bar is a number pretending to be a chart."""
    if total == 0:
        return (f'<div class="chart"><h3>{esc(title)}</h3>'
                f'<p class="empty">No selected tasks to describe.</p></div>')
    if len(counter) == 1:
        name, k = next(iter(counter.items()))
        shown = UNKNOWN_LABEL if name == "unknown" else name
        return (f'<div class="chart"><h3>{esc(title)}</h3>'
                f'<div class="mono-hero"><span class="mh-v">{esc(shown)}</span>'
                f'<span class="mh-s">every one of the {k} selected tasks · 1 distinct value</span></div>'
                f'<p class="note">{note}</p></div>')
    segs_in = fold(counter)
    W, H, GAP = 1000.0, 30.0, 2.0
    rects, legend, x = [], [], 0.0
    span = W - GAP * (len(segs_in) - 1)
    for name, k, color, extra in segs_in:
        w = span * k / total
        tip = f"{name}: {k} of {total} ({k*100/total:.0f}%)" + (f" — {extra}" if extra else "")
        rects.append(f'<rect x="{x:.2f}" y="0" width="{max(w,1):.2f}" height="{H}" rx="2" '
                     f'fill="{color}"><title>{esc(tip)}</title></rect>')
        legend.append(f'<li title="{esc(tip)}"><span class="sw" style="background:{color}"></span>'
                      f'<span class="lg-n">{esc(name)}</span>'
                      f'<span class="lg-v">{k} · {k*100/total:.0f}%</span></li>')
        x += w + GAP
    return (f'<div class="chart"><h3>{esc(title)}</h3>'
            f'<svg class="cbar" viewBox="0 0 {W:.0f} {H:.0f}" preserveAspectRatio="none" '
            f'role="img" aria-label="{esc(title)} composition of {total} tasks">'
            f'{"".join(rects)}</svg>'
            f'<ul class="legend">{"".join(legend)}</ul>'
            f'<p class="note">{note}</p></div>')


def repo_bars(counter: collections.Counter, shown=14) -> str:
    if not counter:
        return '<p class="empty">No selected tasks to describe.</p>'
    items = counter.most_common()
    top = items[:shown]
    tail = items[shown:]
    top_n = max(c for _, c in items)
    scale_max = max(REPO_CAP, top_n)
    ROW, BAR, PAD_L = 26.0, 14.0, 250.0
    W = 900.0
    H = ROW * len(top) + 26
    plot = W - PAD_L - 60
    out = [f'<svg class="hbar" viewBox="0 0 {W:.0f} {H:.0f}" role="img" '
           f'aria-label="Tasks per repository, cap {REPO_CAP}">']
    capx = PAD_L + plot * REPO_CAP / scale_max
    out.append(f'<line class="grid rule-cap" x1="{capx:.1f}" y1="4" x2="{capx:.1f}" y2="{ROW*len(top)+4:.1f}"></line>')
    out.append(f'<text class="axl" x="{capx+6:.1f}" y="{ROW*len(top)+18:.1f}">cap {REPO_CAP}</text>')
    for i, (name, c) in enumerate(top):
        y = i * ROW + 4
        w = max(plot * c / scale_max, 3)
        out.append(f'<text class="rowlab" x="{PAD_L-10:.0f}" y="{y+BAR-2:.1f}" text-anchor="end">'
                   f'{esc(short_repo(name))}</text>')
        over = c > REPO_CAP
        out.append(f'<rect class="bar{" over" if over else ""}" x="{PAD_L:.0f}" y="{y:.1f}" '
                   f'width="{w:.1f}" height="{BAR}" rx="3">'
                   f'<title>{esc(short_repo(name))}: {c} selected task{"s" if c != 1 else ""} — '
                   + ("over the stated cap of " if over else "cap ") + f'{REPO_CAP}</title></rect>')
        out.append(f'<text class="val" x="{PAD_L+w+8:.1f}" y="{y+BAR-2:.1f}">{c}</text>')
    out.append('</svg>')
    if tail:
        lo, hi = min(c for _, c in tail), max(c for _, c in tail)
        span_txt = f"{lo}" if lo == hi else f"{lo}\u2013{hi}"
        out.append(f'<p class="note">{len(tail)} further repositories are not plotted; they hold '
                   f'{span_txt} selected task{"s" if hi != 1 else ""} each.</p>')
    return "".join(out)


def evidence_plot(picks: list[dict]) -> str:
    pts = []
    missing = []
    for i, p in enumerate(picks, 1):
        r = p["row"]
        t, n = num(r, "turns_median"), num(r, "rollouts_n")
        if t is None or n is None:
            missing.append((i, r))
        else:
            pts.append((i, r, t, n))
    if not pts:
        return ('<p class="empty">No pick carries both a median turn count and an '
                'eligible-rollout count, so there is nothing to plot.</p>')
    W, H = 900.0, 380.0
    L, R, T, B = 62.0, 210.0, 22.0, 46.0
    xs = [p[2] for p in pts]; ys = [p[3] for p in pts]
    xmin, xmax = 0, max(xs) * 1.08 or 1
    ymin, ymax = 0, max(ys) * 1.15 or 1

    def sx(v): return L + (W - L - R) * (v - xmin) / (xmax - xmin or 1)
    def sy(v): return H - B - (H - B - T) * (v - ymin) / (ymax - ymin or 1)

    out = [f'<svg class="scatter" viewBox="0 0 {W:.0f} {H:.0f}" role="img" '
           f'aria-label="Median turns against eligible rollouts, one dot per selected task">']
    for frac in (0, .25, .5, .75, 1):
        gy = T + (H - B - T) * frac
        out.append(f'<line class="grid" x1="{L}" y1="{gy:.1f}" x2="{W-R:.0f}" y2="{gy:.1f}"></line>')
        out.append(f'<text class="axl" x="{L-10}" y="{gy+4:.1f}" text-anchor="end">'
                   f'{round(ymax*(1-frac)):,}</text>')
    for frac in (0, .25, .5, .75, 1):
        gx = L + (W - L - R) * frac
        out.append(f'<text class="axl" x="{gx:.1f}" y="{H-B+22:.1f}" text-anchor="middle">'
                   f'{round(xmax*frac):,}</text>')
    out.append(f'<line class="axis" x1="{L}" y1="{H-B:.1f}" x2="{W-R:.0f}" y2="{H-B:.1f}"></line>')
    label_all = len(pts) <= 12
    number_dots = len(pts) <= 24
    r_dot = 7 if len(pts) <= 40 else (5 if len(pts) <= 120 else 4)
    for i, r, t, n in pts:
        cx, cy = sx(t), sy(n)
        out.append(f'<circle class="dot" cx="{cx:.1f}" cy="{cy:.1f}" r="{r_dot}">'
                   f'<title>#{i} {esc(r.get("name"))} — median {t:,} turns, '
                   f'{n:,} eligible rollouts</title></circle>')
        if number_dots:
            out.append(f'<text class="dotn" x="{cx:.1f}" y="{cy+3.5:.1f}" text-anchor="middle">'
                       f'{i}</text>')
        if label_all:
            out.append(f'<text class="pt" x="{cx+13:.1f}" y="{cy+4:.1f}">'
                       f'{esc(short_repo(r.get("repo_key")))}</text>')
    out.append(f'<text class="axt" x="{(L+W-R)/2:.0f}" y="{H-6:.0f}" text-anchor="middle">'
               f'median assistant turns →</text>')
    out.append(f'<text class="axt" transform="rotate(-90 16 {(T+H-B)/2:.0f})" x="16" '
               f'y="{(T+H-B)/2:.0f}" text-anchor="middle">eligible rollouts →</text>')
    out.append('</svg>')
    if missing:
        out.append('<p class="note warn">Not plotted, because a value is missing rather than zero: '
                   + ", ".join(f"#{i} {esc(r.get('name'))}" for i, r in missing) + ".</p>")
    return "".join(out)


GLYPH = {"fresh": "●", "traded": "◐", "forced": "○"}
STATE_NOTE = {"fresh": "opened — first use of this value",
              "traded": "repeated by choice, to protect a higher-priority axis",
              "forced": "repeated because the pool held nothing unused here"}


def ribbon(picks: list[dict]) -> str:
    """One column per pick, one row per axis. Glyph and colour both carry the
    state, so the reading never depends on colour alone."""
    cols = []
    for i, p in enumerate(picks, 1):
        cells = []
        for _f, label, val, why, nth, _u in axis_state(p["row"]):
            tip = (f"pick {i} · {label} {val} — {STATE_NOTE.get(why, why)}"
                   + (f" (use #{nth})" if why != "fresh" and nth else ""))
            cells.append(f'<span class="cell {why}" title="{esc(tip)}">'
                         f'<span aria-hidden="true">{GLYPH.get(why, "·")}</span>'
                         f'<span class="sr">{esc(tip)}</span></span>')
        cols.append(f'<div class="rcol"><span class="rn">{i}</span>{"".join(cells)}</div>')
    key = ('<ul class="legend rkey">'
           + "".join(f'<li><span class="gl {k}" aria-hidden="true">{v}</span>'
                     f'<span class="lg-n">{esc(STATE_NOTE[k])}</span></li>'
                     for k, v in GLYPH.items())
           + '</ul>')
    return ('<div class="ribbon-wrap"><div class="ribbon">'
            '<div class="rlab"><span class="rn">&nbsp;</span><span>language</span>'
            '<span>shape</span><span>repository</span></div>'
            + "".join(cols) + '</div></div>' + key)


# ------------------------------------------------------------------------ css

CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  color-scheme:light;
  --bg:#f2f5f3; --panel:#ffffff; --panel-2:#f8faf9; --sunk:#eef3f1;
  --ink:#0f1e1b; --ink-2:#4c5b57; --ink-3:#7a8985;
  --rule:#d6e0dd; --rule-2:#e8efed;
  --accent:#0a6b5e; --accent-2:#0d8574; --accent-soft:#e0efeb;
  --warm:#8c4e17; --warm-soft:#f6ece1;
  --s1:#009b88; --s2:#eb6834; --s3:#4a3aa7; --s4:#eda100; --s5:#2a78d6; --s6:#e87ba4;
  --s-other:#93a19d; --s-unknown:#c3ccc9;
  --shadow:0 1px 0 rgba(15,30,27,.04), 0 10px 28px -22px rgba(15,30,27,.45);
  --disp:"Bricolage Grotesque","Helvetica Neue",Arial,sans-serif;
  --body:Literata,Georgia,"Times New Roman",serif;
  --mono:"DM Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --bg:#0d1615; --panel:#141d1c; --panel-2:#182322; --sunk:#101917;
    --ink:#eaf1ef; --ink-2:#a1b1ad; --ink-3:#778783;
    --rule:#26332f; --rule-2:#1d2827;
    --accent:#45cdb6; --accent-2:#5ad9c2; --accent-soft:#10302b;
    --warm:#e0a165; --warm-soft:#2e2114;
    --s1:#12ab97; --s2:#d95926; --s3:#9085e9; --s4:#c98500; --s5:#3987e5; --s6:#d55181;
    --s-other:#798985; --s-unknown:#40504c;
    --shadow:0 1px 0 rgba(0,0,0,.3), 0 12px 30px -24px rgba(0,0,0,.9);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#0d1615; --panel:#141d1c; --panel-2:#182322; --sunk:#101917;
  --ink:#eaf1ef; --ink-2:#a1b1ad; --ink-3:#778783;
  --rule:#26332f; --rule-2:#1d2827;
  --accent:#45cdb6; --accent-2:#5ad9c2; --accent-soft:#10302b;
  --warm:#e0a165; --warm-soft:#2e2114;
  --s1:#12ab97; --s2:#d95926; --s3:#9085e9; --s4:#c98500; --s5:#3987e5; --s6:#d55181;
  --s-other:#798985; --s-unknown:#40504c;
  --shadow:0 1px 0 rgba(0,0,0,.3), 0 12px 30px -24px rgba(0,0,0,.9);
}

body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body);
  font-size:16px;line-height:1.62;-webkit-font-smoothing:antialiased;overflow-x:hidden}
.wrap{max-width:1140px;margin:0 auto;padding:0 24px 96px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
a{color:var(--accent);text-decoration-color:color-mix(in srgb,var(--accent) 40%,transparent);
  text-underline-offset:3px}
a:hover{color:var(--accent-2)}
:focus-visible{outline:2px solid var(--accent-2);outline-offset:3px;border-radius:3px}
b,strong{font-weight:600}
h1,h2,h3{font-family:var(--disp);text-wrap:balance;margin:0;letter-spacing:-.015em}

/* --- masthead ------------------------------------------------------------ */
header.mast{padding:64px 0 34px;border-bottom:1px solid var(--rule)}
.kicker{font-family:var(--mono);font-size:11.5px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--accent);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.kicker .dotsep{width:4px;height:4px;border-radius:50%;background:var(--rule);flex:none}
h1{font-size:clamp(46px,8.4vw,84px);line-height:.94;font-weight:800;margin:18px 0 0}
h1 .thin{font-weight:500;color:var(--ink-2)}
.lede{max-width:62ch;font-size:18.5px;color:var(--ink-2);margin:20px 0 0}
.lede b{color:var(--ink);font-weight:600}
.stamp{margin-top:22px;font-family:var(--mono);font-size:12px;color:var(--ink-3);
  display:flex;gap:8px 22px;flex-wrap:wrap}

/* --- verdict band -------------------------------------------------------- */
.verdict{margin-top:30px;border:1px solid var(--rule);border-left:3px solid var(--warm);
  background:var(--panel);border-radius:4px;padding:20px 24px;box-shadow:var(--shadow)}
.verdict.ok{border-left-color:var(--accent)}
.verdict.flat{margin-top:14px;box-shadow:none;border-left-color:var(--ink-3)}
.verdict h2{font-size:19px;font-weight:700;margin-bottom:6px}
.verdict p{margin:0;color:var(--ink-2);max-width:76ch}
.verdict p + p{margin-top:8px}

/* --- section scaffolding ------------------------------------------------- */
section{margin-top:64px;border-top:1px solid var(--rule);padding-top:26px}
.sec-grid{display:grid;grid-template-columns:1fr;gap:8px 40px}
@media (min-width:940px){.sec-grid{grid-template-columns:170px minmax(0,1fr)}}
.sec-head{font-family:var(--mono);font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--ink-3);padding-top:7px}
.sec-body{min-width:0}
h2{font-size:29px;font-weight:700;margin-bottom:10px}
.sub{color:var(--ink-2);max-width:66ch;margin:0 0 24px}

/* --- tiles --------------------------------------------------------------- */
.tiles{display:grid;grid-template-columns:repeat(2,1fr);gap:1px;
  background:var(--rule-2);border:1px solid var(--rule-2);border-radius:4px;overflow:hidden}
@media (min-width:620px){.tiles{grid-template-columns:repeat(3,1fr)}}
@media (min-width:1000px){.tiles{grid-template-columns:repeat(6,1fr)}}
.tile{background:var(--panel);padding:16px 18px 18px;display:flex;flex-direction:column;gap:2px;
  min-width:0;overflow-wrap:anywhere}
.t-l{font-family:var(--mono);font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--ink-3)}
.t-v{font-family:var(--disp);font-size:34px;font-weight:700;line-height:1.1;
  font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.t-s{font-size:13px;color:var(--ink-2);line-height:1.4}

/* --- key ladder ---------------------------------------------------------- */
.ladder{list-style:none;margin:0;padding:0;border:1px solid var(--rule);border-radius:4px;
  background:var(--panel);overflow:hidden}
.ladder li{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:14px;
  align-items:baseline;padding:13px 18px;border-top:1px solid var(--rule-2)}
.ladder .lr{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;justify-content:flex-end}
.ladder .lv{font-family:var(--mono);font-size:11px;color:var(--ink-3);white-space:nowrap}
.ladder .lv.inert{color:var(--warm)}

.ladder li:first-child{border-top:0}
.ladder .ln{font-family:var(--mono);font-size:13px;color:var(--ink-3)}
.ladder .lt{font-family:var(--disp);font-weight:600;font-size:16px}
.ladder .lt em{font-family:var(--body);font-style:normal;font-weight:400;color:var(--ink-2);
  font-size:14.5px;margin-left:10px}
.ladder .lk{font-family:var(--mono);font-size:10.5px;letter-spacing:.11em;text-transform:uppercase}
.ladder .lk.diversity{color:var(--accent)}
.ladder .lk.quality{color:var(--warm)}
.ladder .lk.tiebreak{color:var(--ink-3)}
.ladder li.diversity{background:linear-gradient(90deg,var(--accent-soft),transparent 62%)}
.ladder li.inert{background:linear-gradient(90deg,var(--warm-soft),transparent 62%)}

/* --- pick ledger --------------------------------------------------------- */
.picks{display:flex;flex-direction:column;gap:14px}
.pick{background:var(--panel);border:1px solid var(--rule);border-radius:4px;
  box-shadow:var(--shadow);display:grid;grid-template-columns:1fr;overflow:hidden}
@media (min-width:820px){.pick{grid-template-columns:132px minmax(0,1fr)}}
.rail{background:var(--panel-2);border-bottom:1px solid var(--rule-2);padding:16px 18px;
  display:flex;flex-direction:column;gap:9px}
@media (min-width:820px){.rail{border-bottom:0;border-right:1px solid var(--rule-2)}}
.pnum{font-family:var(--disp);font-size:32px;font-weight:800;line-height:1;
  font-variant-numeric:tabular-nums;letter-spacing:-.03em}
.pnum span{font-size:14px;font-weight:500;color:var(--ink-3);margin-left:3px}
.keyt{font-family:var(--mono);font-size:11px;color:var(--ink-3);line-height:1.5;word-break:break-all}
.keyt b{color:var(--accent);font-weight:500}
.pbody{padding:16px 20px 18px;min-width:0}
.pname{font-family:var(--disp);font-size:19px;font-weight:700;line-height:1.25;
  word-break:break-word;display:block}
.pname a{color:inherit;text-decoration:none;border-bottom:1px solid var(--rule)}
.pname a:hover{color:var(--accent);border-bottom-color:var(--accent)}
.tid{font-family:var(--mono);font-size:11px;color:var(--ink-3);word-break:break-all;display:block;
  margin-top:3px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:11px 0 0}
.chip{font-family:var(--mono);font-size:11px;letter-spacing:.03em;padding:3px 8px;border-radius:3px;
  border:1px solid var(--rule);color:var(--ink-2);background:var(--sunk);white-space:nowrap}
.chip.fresh{border-color:color-mix(in srgb,var(--accent) 45%,transparent);color:var(--accent);
  background:var(--accent-soft)}
.chip.traded{border-color:color-mix(in srgb,var(--warm) 40%,transparent);color:var(--warm);
  background:var(--warm-soft)}
.chip.forced{border-color:var(--rule);color:var(--ink-2);background:var(--sunk)}
.chip .gl{margin-right:5px}
.chip.unknown{border-style:dashed}
.why{margin:12px 0 0;color:var(--ink);max-width:70ch}
.why b{color:var(--ink);font-weight:600}
.decided{margin:7px 0 0;color:var(--ink-2);font-size:14.5px;max-width:70ch}
.decided .rname{font-family:var(--mono);font-size:12.5px}
.pstats{display:flex;flex-wrap:wrap;gap:2px 26px;margin-top:14px;padding-top:12px;
  border-top:1px solid var(--rule-2)}
.pstat{display:flex;gap:7px;align-items:baseline;font-size:13.5px}
.pstat .k{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3)}
.pstat .v{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink)}
.pstat .v.none{color:var(--ink-3)}

/* --- charts -------------------------------------------------------------- */
.grid2{display:grid;grid-template-columns:1fr;gap:14px}
.grid2>*{min-width:0}
@media (min-width:820px){.grid2{grid-template-columns:1fr 1fr}}
.chart{background:var(--panel);border:1px solid var(--rule);border-radius:4px;
  padding:18px 20px 16px;min-width:0}
.chart h3{font-size:15px;font-weight:700;margin-bottom:14px}
.scroll{overflow-x:auto;overflow-y:hidden;max-width:100%;min-width:0}
svg{display:block;max-width:100%;height:auto}
.cbar{width:100%;height:30px}
.hbar{min-width:660px}
.scatter{min-width:640px}
.grid{stroke:var(--rule-2);stroke-width:1}
.rule-cap{stroke:var(--warm);stroke-width:1.5;stroke-dasharray:none;opacity:.75}
.axis{stroke:var(--rule);stroke-width:1}
.axl{font-family:var(--mono);font-size:11px;fill:var(--ink-3)}
.axt{font-family:var(--mono);font-size:11px;letter-spacing:.1em;fill:var(--ink-3);
  text-transform:uppercase}
.rowlab{font-family:var(--mono);font-size:12px;fill:var(--ink-2)}
.val{font-family:var(--mono);font-size:12px;fill:var(--ink);font-variant-numeric:tabular-nums}
.bar{fill:var(--s1)}
.bar.over{fill:var(--warm)}
.bar:hover{fill:var(--accent-2)}
.dot{fill:var(--s1);stroke:var(--panel);stroke-width:2}
.dotn{font-family:var(--mono);font-size:9px;fill:var(--panel);pointer-events:none}
.pt{font-family:var(--mono);font-size:11.5px;fill:var(--ink-2)}
.legend{list-style:none;display:flex;flex-wrap:wrap;gap:6px 20px;margin:12px 0 0;padding:0}
.legend li{display:flex;align-items:center;gap:7px;font-size:13px}
.sw{width:11px;height:11px;border-radius:2px;flex:none}
.lg-n{color:var(--ink)}
.lg-v{font-family:var(--mono);font-size:12px;color:var(--ink-3);font-variant-numeric:tabular-nums}
.mono-hero{display:flex;flex-direction:column;gap:2px;padding:6px 0 4px}
.mh-v{font-family:var(--disp);font-size:32px;font-weight:700;letter-spacing:-.02em}
.mh-s{font-size:13.5px;color:var(--ink-2)}
.note{font-size:13px;color:var(--ink-2);margin:12px 0 0;max-width:70ch}
.note.warn{color:var(--warm)}
.empty{font-size:14px;color:var(--ink-3);margin:0}

/* --- ribbon -------------------------------------------------------------- */
.ribbon-wrap{overflow-x:auto;max-width:100%;width:fit-content;background:var(--panel);
  border:1px solid var(--rule);border-radius:4px;padding:16px 18px}
.ribbon{display:flex;gap:0;align-items:stretch;min-width:min-content}
.rlab{display:flex;flex-direction:column;gap:0;padding-right:14px;position:sticky;left:0;
  background:var(--panel);z-index:1}
.rlab span{height:26px;display:flex;align-items:center;font-family:var(--mono);font-size:11px;
  color:var(--ink-3);white-space:nowrap}
.rcol{display:flex;flex-direction:column;width:24px;flex:none}
.rcol .rn,.rlab .rn{height:26px;display:flex;align-items:center;justify-content:center;
  font-family:var(--mono);font-size:10px;color:var(--ink-3)}
.cell{height:26px;display:flex;align-items:center;justify-content:center;font-size:13px}
.cell.fresh{color:var(--accent)}
.cell.traded{color:var(--warm)}
.cell.forced{color:var(--ink-3)}
.rkey{gap:8px 22px}
.gl{font-size:13px;line-height:1}
.gl.fresh{color:var(--accent)}
.gl.traded{color:var(--warm)}
.gl.forced{color:var(--ink-3)}

/* --- gaps ---------------------------------------------------------------- */
.gaps{display:flex;flex-direction:column;gap:1px;background:var(--rule-2);
  border:1px solid var(--rule-2);border-radius:4px;overflow:hidden;min-width:0}
.gap{min-width:0}
.gap{background:var(--panel);padding:17px 20px}
.gap h3{font-size:16px;font-weight:700;margin-bottom:5px}
.gap p{margin:0;color:var(--ink-2);max-width:76ch;font-size:14.5px}
.gap p + p{margin-top:7px}
.blk.wide{min-width:820px}
.blk.wide td{white-space:nowrap}
.blk .gls{letter-spacing:2px}
.gap .flag{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--warm);display:block;margin-bottom:5px}
.gap.good .flag{color:var(--accent)}
table.blk{width:100%;border-collapse:collapse;margin-top:12px;font-size:14px}
table.blk th{text-align:left;font-family:var(--mono);font-size:10.5px;letter-spacing:.11em;
  text-transform:uppercase;color:var(--ink-3);font-weight:400;padding:0 12px 7px 0;
  border-bottom:1px solid var(--rule)}
table.blk td{padding:7px 12px 7px 0;border-bottom:1px solid var(--rule-2);vertical-align:top;
  color:var(--ink-2)}
table.blk td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink);
  text-align:right;width:1%;padding-right:18px}
table.blk td.t{color:var(--ink);font-family:var(--mono);font-size:12.5px;word-break:break-all}
footer{margin-top:56px;border-top:1px solid var(--rule);padding-top:20px;font-size:13px;
  color:var(--ink-3);font-family:var(--mono);line-height:1.7}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


# ----------------------------------------------------------------------- build

def pick_stats(r: dict) -> str:
    p6, den = num(r, "pass6"), num(r, "pass6_denominator")
    solved = "—" if (p6 is None or not den) else f"{p6}/{den}"
    cells = [
        ("rollouts", fmt(num(r, "rollouts_n"))),
        ("turns med", fmt(num(r, "turns_median"))),
        ("turns max", fmt(num(r, "turns_max"))),
        ("solved", solved),
        ("argus main", r.get("argus_main") or "—"),
        ("ai rubrics", r.get("ai_rubrics") or "—"),
    ]
    out = []
    for k, v in cells:
        none = v in ("—", "None")
        out.append(f'<span class="pstat"><span class="k">{esc(k)}</span>'
                   f'<span class="v{" none" if none else ""}">{esc(v)}</span></span>')
    return f'<div class="pstats">{"".join(out)}</div>'


def build(data: dict, enrich: dict | None, mod, import_note: str | None) -> str:
    rows = data.get("tasks") or []
    generated = data.get("generated_at") or "unknown"
    rules = data.get("rules") or {}

    result = mod.diverse_order(rows) if mod else local_order(rows)
    # The strategy module returns (ordered, diagnostics); tolerate a bare list
    # so a change on their side degrades to less detail rather than a crash.
    if isinstance(result, tuple):
        ordered, diag = result[0], (result[1] if len(result) > 1 else {})
    else:
        ordered, diag = result, {}
    picks, faithful = replay(ordered)
    diag_axes = {a.get("field"): a for a in (diag.get("axes") or [])}
    eligible = len(picks)
    # The golden set is the first TARGET picks; anything past the cut is a ranked
    # reserve, shown compactly rather than dropped.
    taken, reserve = picks[:TARGET], picks[TARGET:]
    n = len(taken)
    analysed = len(rows)
    unfit = [r for r in rows if r.get("fit") != "YES"]

    langs = collections.Counter(axis(p["row"], "lang_key") for p in taken)
    shapes = collections.Counter(axis(p["row"], "shape") for p in taken)
    repos = collections.Counter(axis(p["row"], "repo_key") for p in taken)
    turns = [num(p["row"], "turns_median") for p in taken]
    turns_ok = sorted(t for t in turns if t is not None)
    rolls = [num(p["row"], "rollouts_n") for p in taken]
    rolls_ok = [x for x in rolls if x is not None]

    def med(xs):
        if not xs:
            return None
        k = len(xs)
        return xs[k // 2] if k % 2 else (xs[k // 2 - 1] + xs[k // 2]) // 2

    # Where each axis first repeated, and how the repeats broke down -- measured
    # over the tasks actually taken, not over the whole eligible pool.
    spend, why_n = {}, {}
    for field, _ in AX:
        spend[field] = next((i for i, p in enumerate(taken, 1)
                             if p["row"].get(f"_{field}_why", "fresh") != "fresh"), None)
        why_n[field] = collections.Counter(p["row"].get(f"_{field}_why", "fresh") for p in taken)
    # ---- how much variety each axis actually holds, and which terms are inert
    # A term with one distinct value scores every candidate identically: it can
    # never separate two tasks, so it is dead weight in the key. Saying which
    # terms are live is more useful than restating the policy.
    distinct_by_field = {}
    for field, _ in AX:
        a = diag_axes.get(field)
        counts = {"lang_key": langs, "shape": shapes, "repo_key": repos}[field]
        distinct_by_field[field] = a.get("distinct") if a else len(counts)
    degenerate = [(f, lab, distinct_by_field[f]) for f, lab in AX
                  if (distinct_by_field[f] or 0) <= 1]

    busiest = max(repos.values()) if repos else 0
    over_cap = sorted(((k, v) for k, v in repos.items() if v > REPO_CAP),
                      key=lambda kv: (-kv[1], kv[0]))
    shortfall = max(TARGET - n, 0)

    # ---- tiles
    tiles = [
        stat_tile("Selected", f"{n}", f"of a target {TARGET} — "
                  + (f"<b>{shortfall} short</b>" if shortfall else "target met")),
        stat_tile("Eligible pool", f"{eligible}",
                  f"fit tasks out of {analysed} analysed"
                  + (f"; {len(reserve)} held in reserve" if reserve else "")),
        stat_tile("Languages", f"{len(langs)}",
                  esc(", ".join(f"{UNKNOWN_LABEL if k == 'unknown' else k} {v}"
                                for k, v in langs.most_common(3))) or "—"),
        stat_tile("Repositories", f"{len(repos)}",
                  (f"<b>busiest holds {busiest}, over the stated cap of {REPO_CAP}</b>"
                   if busiest > REPO_CAP else f"busiest holds {busiest} of {REPO_CAP} allowed")
                  if repos else "—"),
        stat_tile("Median turns", fmt(med(turns_ok)),
                  (f"across {len(turns_ok)} of {n} picks; "
                   f"{fmt(turns_ok[0] if turns_ok else None)}–{fmt(turns_ok[-1] if turns_ok else None)}")
                  if turns_ok else "not measured on any pick"),
        stat_tile("Eligible rollouts", fmt(sum(rolls_ok)) if rolls_ok else "—",
                  (f"total; {fmt(min(rolls_ok))}–{fmt(max(rolls_ok))} per task")
                  if rolls_ok else "not measured on any pick"),
    ]

    # ---- verdict band
    if shortfall:
        verdict = (
            '<div class="verdict">'
            f'<h2>The strategy is not the constraint — the pool is.</h2>'
            f'<p>The selector returns <b>{n} task{"s" if n != 1 else ""}</b> against a target of '
            f'{TARGET}. It is not filtering a large pool down to {n}: {n} '
            f'{"is" if n == 1 else "are"} every task in this dataset that clears the pilot gate, '
            f'so all {n} {"is" if n == 1 else "are"} taken and the order is the only decision left. '
            f'Filling the remaining <b>{shortfall}</b> needs more tasks measured or more tasks '
            f'fixed, not a different key.</p></div>')
    else:
        verdict = ('<div class="verdict ok">'
                   f'<h2>Target met: the top {n} of {eligible} eligible tasks.</h2>'
                   f'<p>The pool is large enough that the key is genuinely choosing between '
                   f'candidates. The {len(reserve)} tasks past the cut are still ordered and are '
                   f'listed as reserves — they are the next ones the pilot would take, in order.'
                   f'</p></div>')

    # ---- a diversity axis with nothing to rotate through is a finding, and it
    # belongs at the top: it silently removes a term from the key.
    degen_band = ""
    if degenerate:
        names = []
        for field, lab, _d in degenerate:
            counts = {"lang_key": langs, "shape": shapes, "repo_key": repos}[field]
            only = next(iter(counts), None)
            only = UNKNOWN_LABEL if only == "unknown" else only
            names.append(f'<b>{esc(lab)}</b> (every eligible task is <b>{esc(only)}</b>)')
        term_no = ", ".join(str(i + 1) for i, (f, _l) in enumerate(AX)
                            if f in {d[0] for d in degenerate})
        plural = len(degenerate) != 1
        degen_band = (
            '<div class="verdict flat">'
            f'<h2>{"Two axes have" if plural else "One axis has"} nothing to rotate through</h2>'
            f'<p>{" and ".join(names)}. With a single value in the pool, '
            f'{"those terms score" if plural else "that term scores"} every candidate identically, '
            f'so term {term_no} of the key can never separate two tasks — the ordering is really '
            f'being decided by the other {6 - len(degenerate)} terms. The tags below still mark '
            f'{"them" if plural else "it"} as spent from pick 2 onward, because that is what '
            f'happened; it is not a weakness of the ordering but a fact about the candidate '
            f'pool.</p></div>')

    fidelity = ""
    if import_note:
        fidelity = ('<div class="verdict"><h2>Ordering source</h2><p>The shipped '
                    f'<code>diverse_order()</code> could not be imported ({esc(import_note)}), so this '
                    'page ordered the pool with its own copy of the six-term key. Treat the order as '
                    'unverified against the strategy module.</p></div>')
    elif not faithful:
        fidelity = ('<div class="verdict"><h2>Order and explanation disagree</h2><p>The order below is '
                    'the one <code>render_report.diverse_order()</code> produced and is authoritative. '
                    'Replaying the six-term key described here did not reproduce it, which means the '
                    'shipped key has moved on. The axis rationale still holds; the per-pick '
                    '&ldquo;which term decided it&rdquo; line is suppressed rather than guessed.</p></div>')

    # ---- ladder, annotated with the variety each term has to work with
    ladder = []
    for i, (name, desc, kind) in enumerate(TERMS, 1):
        note, dead = "", ""
        if i <= 3:
            field = AX[i - 1][0]
            d = distinct_by_field[field]
            if d is None:
                note = ""
            elif d <= 1:
                note, dead = f"1 value in the pool — inert", " inert"
            else:
                note = f"{d} values in the pool"
        ladder.append(f'<li class="{kind}{dead}"><span class="ln">{i}</span>'
                      f'<span class="lt">{esc(name)}<em>{esc(desc)}</em></span>'
                      f'<span class="lr">'
                      + (f'<span class="lv{dead}">{esc(note)}</span>' if note else "")
                      + f'<span class="lk {kind}">{esc(kind)}</span></span></li>')

    # ---- picks
    pick_html = []
    for i, p in enumerate(taken, 1):
        r = p["row"]
        why, decided = rationale(p, i, faithful and not import_note)
        chips = []
        for _f, label, val, wy, nth, unk in axis_state(r):
            txt = (f"new {label} · {val}" if wy == "fresh" else f"{label} {val} · use #{nth}")
            chips.append(f'<span class="chip {wy}{" unknown" if unk else ""}" '
                         f'title="{esc(STATE_NOTE.get(wy, wy))}">'
                         f'<span class="gl {wy}" aria-hidden="true">{GLYPH.get(wy, "·")}</span>'
                         f'{esc(txt)}</span>')
        k = p["key"]
        kt = (f'<span class="keyt">key<br><b>{k[0]}</b> <b>{k[1]}</b> <b>{k[2]}</b>'
              f'<br>{k[3]} {k[4]} {k[5]}</span>')
        tid = r.get("task_id") or ""
        link = (f'<a href="{HORIZON}{esc(tid)}" target="_blank" rel="noopener noreferrer">'
                f'{esc(r.get("name") or "unnamed task")}</a>') if tid else esc(r.get("name"))
        pick_html.append(
            '<article class="pick">'
            f'<div class="rail"><span class="pnum">{i}<span>/{n}</span></span>{kt}</div>'
            f'<div class="pbody"><span class="pname">{link}</span>'
            f'<span class="tid">{esc(tid) or "task id missing"}</span>'
            f'<div class="chips">{"".join(chips)}</div>'
            f'<p class="why">{why}</p>'
            + (f'<p class="decided">{decided}</p>' if decided else "")
            + pick_stats(r) + '</div></article>')

    # ---- blockers on the tasks that did not make it
    blk_rows = ""
    if unfit:
        reasons = collections.Counter()
        named = 0
        for r in unfit:
            rs = mod.blockers(r) if (mod and hasattr(mod, "blockers")) else []
            if rs:
                named += 1
            seen = set()
            for entry in rs:
                for part in entry.split(";"):
                    label = norm_reason(part.strip())
                    if label and label not in seen:
                        seen.add(label)
                        reasons[label] += 1
        body = "".join(f'<tr><td class="n">{c}</td><td>{esc(k)}</td></tr>'
                       for k, c in reasons.most_common())
        if not body:
            body = ('<tr><td class="n">—</td><td>No blocker detail is available for these '
                    'tasks, so this page does not guess at one.</td></tr>')
        unnamed = len(unfit) - named
        blk_rows = (f'<table class="blk"><thead><tr><th>Tasks</th>'
                    f'<th>Why they are not eligible</th></tr></thead><tbody>{body}</tbody></table>'
                    f'<p class="note">One task can fail several gates, so these counts add up to '
                    f'more than the {len(unfit)} excluded tasks.'
                    + (f' {unnamed} of them record no reason at all.' if unnamed else "") + '</p>')

    # ---- coverage from the enrichment sheet
    cover = ""
    if enrich:
        et = enrich.get("tasks") or []
        known = len(et)
        e_lang = collections.Counter(x.get("lang_key") or "unknown" for x in et)
        e_repo = {x.get("repo_key") for x in et}
        seen = {r.get("task_id") for r in rows}
        overlap = sum(1 for x in et if x.get("task_id") in seen)
        missing_langs = [f"{k} ({v})" for k, v in e_lang.most_common() if k not in langs]
        cover = (
            '<div class="gap"><span class="flag">coverage</span>'
            f'<h3>{analysed} of {known} known tasks have been analysed</h3>'
            f'<p>The enrichment sheet lists <b>{known}</b> tasks across <b>{len(e_repo)}</b> '
            f'repositories and <b>{len(e_lang)}</b> languages '
            f'({esc(", ".join(f"{k} {v}" for k, v in e_lang.most_common()))}); '
            f'{overlap} of them appear in this analysis. The selection can only ever be as diverse '
            f'as the slice that has been measured.</p>'
            + (f'<p>Languages present in the sheet but absent from every selected task: '
               f'<b>{esc(", ".join(missing_langs))}</b>. Their absence here is a measurement gap, '
               f'not a statement about those tasks.</p>' if missing_langs else "")
            + '</div>')

    # ---- axis exhaustion narrative
    # A repeat at pick k is only a weakness if the pool held enough distinct
    # values to avoid it. With d distinct values the earliest possible repeat is
    # pick d+1, so the floor is the honest yardstick, not the raw position.
    ex_lines = []
    for field, label in AX:
        lab = label.capitalize()
        a = diag_axes.get(field) or {}
        counts = {"lang_key": langs, "shape": shapes, "repo_key": repos}[field]
        d = a.get("distinct", len(counts))
        at = spend[field]
        unk = a.get("unknown") or 0
        tail = (f' {unk} selected task{"s" if unk != 1 else ""} carry no recorded {label} and share '
                f'a single <i>unknown</i> bucket.' if unk else "")
        floor = a.get("floor", d + 1)
        forced_c, traded_c = why_n[field]["forced"], why_n[field]["traded"]
        if at is None:
            ex_lines.append(f'<p><b>{lab}</b> never repeated: all {n} picks opened a distinct value '
                            f'({d} available in the eligible pool).{tail}</p>')
        elif d <= 1:
            ex_lines.append(f'<p><b>{lab}</b> was spent at <b>pick {at}</b>, and could not have gone '
                            f'further: the eligible pool holds exactly one {label}, so every pick '
                            f'after the first repeats it. This is a property of the pool, not of the '
                            f'ordering.{tail}</p>')
        else:
            judged = ("as late as arithmetic allows — with "
                      f"{d} distinct values in the pool, the earliest possible repeat is pick "
                      f"{floor}" if at >= floor
                      else f"earlier than the floor of pick {floor}, because keeping it fresh "
                           "would have cost a higher-priority axis")
            ex_lines.append(f'<p><b>{lab}</b> first repeated at <b>pick {at}</b>, {judged}. '
                            f'Across the {n} taken, {forced_c} repeat'
                            f'{"s were" if forced_c != 1 else " was"} forced and {traded_c} '
                            f'{"were" if traded_c != 1 else "was"} traded.{tail}</p>')

    # ---- honest gaps: fields that are absent rather than zero
    absent = []
    for field, label in (("turns_median", "median turns"), ("rollouts_n", "eligible rollouts"),
                         ("pass6_denominator", "pass@6 denominator")):
        miss = [p["row"].get("name") for p in taken if num(p["row"], field) is None]
        if miss:
            absent.append(f'{len(miss)} pick{"s" if len(miss) != 1 else ""} carry no {label}')
    absent_html = ("; ".join(absent) + "."
                   if absent else
                   "Every selected task carries a value for each figure shown above.")

    per_model_gap = []
    for p in taken:
        pm = p["row"].get("per_model") or {}
        for mname, mv in pm.items():
            if not isinstance(mv, dict):
                continue
            if not num(mv, "denominator"):
                per_model_gap.append(mname)
    pm_counts = collections.Counter(per_model_gap)

    # ---- reserves: everything the key ranked below the cut
    reserve_html = ""
    if reserve:
        rrows = []
        for i, p in enumerate(reserve, n + 1):
            r = p["row"]
            glyphs = "".join(f'<span class="gl {w}" title="{esc(lab)}: {esc(STATE_NOTE[w])}">'
                             f'{GLYPH[w]}</span>'
                             for _f, lab, _v, w, _nth, _u in axis_state(r) if w in GLYPH)
            tid = r.get("task_id") or ""
            nm = (f'<a href="{HORIZON}{esc(tid)}" target="_blank" rel="noopener noreferrer">'
                  f'{esc(r.get("name"))}</a>') if tid else esc(r.get("name"))
            rrows.append(
                f'<tr><td class="n">{i}</td><td class="t">{nm}</td>'
                f'<td>{esc(axis(r, "lang_key")).replace("unknown", UNKNOWN_LABEL)}</td>'
                f'<td>{esc(axis(r, "shape"))}</td>'
                f'<td class="t">{esc(short_repo(r.get("repo_key")))}</td>'
                f'<td class="n">{fmt(num(r, "rollouts_n"))}</td>'
                f'<td class="n">{fmt(num(r, "turns_median"))}</td>'
                f'<td class="gls">{glyphs}</td></tr>')
        reserve_html = (
            f'<h2>{len(reserve)} more, ranked below the cut</h2>'
            f'<p class="sub">These clear the same gate; the pilot simply stops at {TARGET}. They '
            f'are in the order the key would take them, so raising the target adds them from the '
            f'top down.</p>'
            f'<div class="scroll"><table class="blk wide"><thead><tr><th>#</th><th>Task</th>'
            f'<th>Language</th><th>Shape</th><th>Repository</th><th>Rollouts</th>'
            f'<th>Turns</th><th>Axes</th></tr></thead><tbody>{"".join(rrows)}</tbody></table></div>')

    ribbon_html = ribbon(taken)
    rule_bits = ", ".join(f"{k}={v}" for k, v in rules.items()) if rules else "not recorded"

    def sec(head, inner):
        return (f'<section><div class="sec-grid"><div class="sec-head">{esc(head)}</div>'
                f'<div class="sec-body">{inner}</div></div></section>')

    picked_names = ", ".join(esc(p["row"].get("name") or "?") for p in taken)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Golden Set</title>
<meta name="description" content="The pilot tasks the diversity-first selector picks, in pick order, with the reason for each position.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=DM+Mono:wght@400;500&family=Literata:ital,opsz,wght@0,7..72,400;0,7..72,500;0,7..72,600;1,7..72,400&display=swap">
<style>{CSS}</style></head>
<body><div class="wrap">

<header class="mast">
  <div class="kicker"><span>Voyager · AlphaRecon</span><span class="dotsep"></span>
    <span>pilot selection</span><span class="dotsep"></span><span>read-only</span></div>
  <h1>Golden Set<br><span class="thin">{n} picks, in order</span></h1>
  <p class="lede">These are the tasks the pilot takes, and the order it takes them in.
  Diversity leads the sort key, so variety is only ever spent when nothing unused is left;
  the quality terms decide between candidates that are equally fresh.
  <b>Every pick below carries the reason it sits where it does.</b></p>
  <div class="stamp"><span>analysis generated {esc(generated)}</span>
    <span>{analysed} tasks analysed · {n} eligible</span>
    <span>ordering: {"render_report.diverse_order()" if mod else "local fallback"}</span></div>
</header>

{fidelity}
{verdict}
{degen_band}

{sec("At a glance", f'<div class="tiles">{"".join(tiles)}</div>')}

{sec("The key", '''<h2>Six terms, read left to right</h2>
<p class="sub">A pick wins on the first term where it beats every rival. The three diversity terms
come first, so a task that opens an unused language outranks a better-measured task that does not.
The quality terms only ever break ties between equally fresh candidates.</p>'''
  + f'<ul class="ladder">{"".join(ladder)}</ul>'
  + '<p class="note">Each pick shows its own key as a tuple in the left rail — the three '
    'diversity counts, then rollouts and turns as negatives (bigger is better, so they sort '
    'ascending), then the row&rsquo;s original rank. Reading the tuples down the page reproduces '
    'the order.</p>')}

{sec("The picks", f'<h2>{n} task{"s" if n != 1 else ""}, in pick order</h2>'
  + f'<p class="sub">Each links to its Horizon record. The chips say what the pick did to each '
    'axis: <b>&#9679; opened</b> it, <b>&#9680; repeated it by choice</b> to protect a '
    'higher-priority axis, or <b>&#9675; repeated it because nothing unused was left</b>.</p>'
  + f'<div class="picks">{"".join(pick_html)}</div>')}

{sec("Reserves", reserve_html) if reserve_html else ""}

{sec("Freshness", '''<h2>Where each axis ran out</h2>
<p class="sub">One column per pick, one row per axis. Reading left to right shows exactly when the
strategy stopped being able to buy variety &mdash; and whether each repeat was unavoidable or a
deliberate trade.</p>'''
  + ribbon_html + "".join(ex_lines))}

{sec("Shape of the set", '<h2>What the selection looks like as a whole</h2>'
  + '<p class="sub">Composition of the selected tasks only — not of the analysed pool.</p>'
  + '<div class="grid2">'
  + composition("Language mix", langs, n,
                "One segment means one language: the set is a monoculture on this axis."
                if len(langs) == 1 else "Segments are counts of selected tasks.")
  + composition("Shape mix", shapes, n,
                "One segment means every selected task is the same shape."
                if len(shapes) == 1 else "Segments are counts of selected tasks.")
  + '</div>'
  + '<div class="chart" style="margin-top:14px"><h3>Tasks per repository, against the cap of '
  + f'{REPO_CAP}</h3><div class="scroll">' + repo_bars(repos) + '</div>'
  + f'<p class="note">{len(repos)} distinct repositor{"ies" if len(repos) != 1 else "y"}; '
  + (f'the busiest holds {busiest}. <b>{len(over_cap)} bar'
     + ("s cross" if len(over_cap) != 1 else " crosses")
     + f' the cap line and {"are" if len(over_cap) != 1 else "is"} marked in the warning colour — '
     + 'the ordering defers a repeated repository but never caps one. '
     if over_cap else f'the busiest holds {busiest}. ')
  + ('Headroom under the cap is not a reason to add tasks from an already-used repository — the '
     'key would spend a repository only after languages and shapes are gone.'
     if not over_cap else
     'The cap is a pilot rule, not something this ordering enforces.')
  + '</p></div>'
  + '<div class="chart" style="margin-top:14px"><h3>Turn depth against evidence</h3>'
  + '<p class="note" style="margin:0 0 10px">Horizontal: how long a run typically takes. Vertical: '
  + 'how many eligible rollouts back that number. Dots low on the chart are real tasks with thin '
  + 'evidence, not weak tasks.</p><div class="scroll">' + evidence_plot(taken) + '</div></div>')}

{sec("Honest gaps", '<h2>What this page cannot tell you</h2>'
  + '<div class="gaps">'
  + ('<div class="gap"><span class="flag">shortfall</span>'
     f'<h3>{shortfall} of {TARGET} seats cannot be filled from this data</h3>'
     f'<p>{n} of {analysed} analysed tasks clear the pilot gate. The other {len(unfit)} are '
     f'excluded for the reasons below — and until they are fixed or more tasks are measured, no '
     f'ordering key can produce {TARGET} tasks.</p>' + blk_rows + '</div>' if shortfall else "")
  + cover
  + ('<div class="gap"><span class="flag">exhaustion</span>'
     '<h3>Variety ran out before the pool did</h3>' + "".join(ex_lines) + '</div>')
  + (f'<div class="gap"><span class="flag">over the cap</span>'
     f'<h3>{len(over_cap)} repositor{"ies exceed" if len(over_cap) != 1 else "y exceeds"} '
     f'the stated cap of {REPO_CAP}</h3>'
     f'<p>The key <i>defers</i> a repeated repository; it does not cap one. Once every repository '
     f'in the pool has been used, further picks come back round, and nothing in the ordering stops '
     f'a repository going past {REPO_CAP}: '
     + ", ".join(f"<b>{esc(short_repo(k))}</b> {v}" for k, v in over_cap[:8])
     + (f" and {len(over_cap) - 8} more" if len(over_cap) > 8 else "")
     + '. Enforcing the cap is a policy decision that has to be made outside this ordering.</p>'
       '</div>' if over_cap else "")
  + '<div class="gap"><span class="flag">missing values</span>'
    '<h3>Absent figures are shown as absent</h3>'
    f'<p>{esc(absent_html)} Nothing on this page substitutes a zero for a missing measurement: a '
    'task with no reviews reads <b>None</b>, a task with no rollouts reads <b>—</b>, and neither '
    'is a pass.</p>'
  + (f'<p>Per-model coverage is uneven: '
     + "; ".join(f"<b>{esc(k)}</b> has no eligible rollouts on {v} of the {n} picks"
                 for k, v in sorted(pm_counts.items()))
     + '.</p>' if pm_counts else "")
  + '</div>'
  + '<div class="gap good"><span class="flag">provenance</span>'
    '<h3>Where these numbers come from</h3>'
    f'<p>Every figure is read from the analysis JSON generated at {esc(generated)}; this page runs '
    'no queries and writes nothing. Eligibility rules recorded with that run: '
    f'<span style="font-family:var(--mono);font-size:12.5px">{esc(rule_bits)}</span>.</p></div>'
  + '</div>')}

<footer>
  Golden set: {picked_names}<br>
  Ordering imported from render_report.diverse_order() · rationale replayed and self-checked ·
  source pilot-report.json generated {esc(generated)}
</footer>
</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("report", nargs="?", default="/tmp/pilot-full.json")
    ap.add_argument("-o", "--out", default="/tmp/golden-set.html")
    ap.add_argument("--enrich", default="/tmp/pilot_enrich.json")
    a = ap.parse_args()

    data = json.loads(Path(a.report).read_text())
    enrich = None
    ep = Path(a.enrich)
    if ep.exists():
        try:
            enrich = json.loads(ep.read_text())
        except Exception:
            enrich = None
    mod, note = load_strategy(Path(__file__).resolve().parent)
    if note:
        print(f"warning: {note}", file=sys.stderr)
    html_doc = build(data, enrich, mod, note)
    Path(a.out).write_text(html_doc)
    print(f"wrote {a.out}  ({len(html_doc):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
