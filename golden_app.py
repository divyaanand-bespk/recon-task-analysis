#!/usr/bin/env python3
"""Render the GOLDEN SET -- the pilot tasks the diversity-first selector picks --
as a standalone, shareable HTML page.

Separate deliverable from render_report.py (the operational readiness report).
This page answers one question: *which tasks does the strategy pick, and why
each at that position?*  It is presentation only; it never touches Horizon.

    python3 golden_app.py ~/Downloads/pilot-report.json -o /tmp/golden-set.html

The ordering itself is imported from render_report.diverse_order() so that this
page always presents the shipped policy rather than a private copy of it.  The
per-pick *explanation* needs the runner-up at each step, which diverse_order()
does not expose, so the six-term key is re-stated here and then SELF-CHECKED:
if replaying it does not reproduce the imported order exactly, the page says so
and falls back to axis-only rationale.  A silent divergence is not possible.
"""
from __future__ import annotations

import argparse, collections, html, importlib.util, json, math, sys
from pathlib import Path

TARGET = 50          # pilot size we are trying to fill
REPO_CAP = 3         # per-repository cap the pilot rules impose
HORIZON = "https://horizon.bespokelabs.ai/tasks/"

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
    tuple and the runner-up it beat. Returns (picks, key_reproduces_order)."""
    pool = [dict(r) for r in ordered]
    for i, r in enumerate(pool):
        r.setdefault("_rank", i)
    lang_used, shape_used, repo_used = (collections.Counter() for _ in range(3))
    picks, faithful = [], True
    remaining = list(pool)
    for i, expected in enumerate(ordered):
        scored = sorted(remaining, key=lambda r: key_tuple(r, lang_used, shape_used, repo_used))
        chosen = next(r for r in remaining if r.get("task_id") == expected.get("task_id"))
        if scored[0].get("task_id") != chosen.get("task_id"):
            faithful = False
        k = key_tuple(chosen, lang_used, shape_used, repo_used)
        runner = next((r for r in scored if r.get("task_id") != chosen.get("task_id")), None)
        rk = key_tuple(runner, lang_used, shape_used, repo_used) if runner else None
        decided = next((j for j in range(6) if rk and k[j] != rk[j]), None)
        picks.append({"row": expected, "key": k, "runner": runner, "runner_key": rk,
                      "decided": decided})
        lang_used[axis(chosen, "lang_key")] += 1
        shape_used[axis(chosen, "shape")] += 1
        repo_used[axis(chosen, "repo_key")] += 1
        remaining = [r for r in remaining if r.get("task_id") != chosen.get("task_id")]
    return picks, faithful


def local_order(rows: list[dict]) -> list[dict]:
    """Fallback ordering, used only if render_report.py cannot be imported."""
    pool = [dict(r) for r in rows if r.get("fit") == "YES"]
    for i, r in enumerate(pool):
        r["_rank"] = i
    lang_used, shape_used, repo_used = (collections.Counter() for _ in range(3))
    out = []
    while pool:
        pick = min(pool, key=lambda r: key_tuple(r, lang_used, shape_used, repo_used))
        la, sh, rp = axis(pick, "lang_key"), axis(pick, "shape"), axis(pick, "repo_key")
        pick["_new_lang"] = lang_used[la] == 0
        pick["_new_shape"] = shape_used[sh] == 0
        pick["_new_repo"] = repo_used[rp] == 0
        lang_used[la] += 1; shape_used[sh] += 1; repo_used[rp] += 1
        pick["_lang_n"], pick["_shape_n"], pick["_repo_n"] = lang_used[la], shape_used[sh], repo_used[rp]
        pick["_fresh"] = pick["_new_lang"] and pick["_new_shape"] and pick["_new_repo"]
        out.append(pick)
        pool = [r for r in pool if r.get("task_id") != pick.get("task_id")]
    return out


# ------------------------------------------------------------------ rationale

AXIS_LABEL = {"lang": "language", "shape": "shape", "repo": "repository"}


def axis_state(row: dict) -> list[tuple]:
    """(axis, value, is_new, nth) for each of the three diversity axes."""
    out = []
    for short, field in (("lang", "lang_key"), ("shape", "shape"), ("repo", "repo_key")):
        val = axis(row, field)
        if short == "repo":
            val = short_repo(val)
        nth = row.get(f"_{short}_n")
        new = row.get(f"_new_{short}")
        out.append((short, val, bool(new), nth))
    return out


def opened_spent(row: dict):
    st = axis_state(row)
    return ([s for s in st if s[2]], [s for s in st if not s[2]])


def rationale(pick: dict, position: int, total: int, faithful: bool) -> tuple[str, str]:
    """(what this pick did to the axes, what decided it over the runner-up)."""
    row = pick["row"]
    opened, spent = opened_spent(row)

    def phrase(items, with_nth=False):
        parts = []
        for short, val, _new, nth in items:
            label = AXIS_LABEL[short]
            parts.append(f"{label} <b>{esc(val)}</b>" + (f" (use #{nth})" if with_nth and nth else ""))
        if len(parts) == 1:
            return parts[0]
        return ", ".join(parts[:-1]) + " and " + parts[-1]

    if position == 1:
        why = f"First pick, so nothing is spent: it opens {phrase(opened)}."
    elif len(opened) == 3:
        why = f"Fully fresh — opens {phrase(opened)} without repeating anything."
    elif opened:
        why = (f"{phrase(spent, with_nth=True).capitalize()} had to be spent; in exchange this "
               f"pick opens {phrase(opened)}.")
    else:
        why = (f"No unused variety was left to buy: it repeats {phrase(spent, with_nth=True)}. "
               "From here the quality terms are doing the ordering.")

    runner, k, rk, j = pick["runner"], pick["key"], pick["runner_key"], pick["decided"]
    if not faithful:
        decided = ""
    elif runner is None:
        decided = "Last task left in the eligible pool — no contest to settle."
    elif j is None:
        decided = ""
    else:
        name, _desc, _kind = TERMS[j]
        a, b = k[j], rk[j]
        if j < 3:
            detail = f"{AXIS_LABEL[['lang','shape','repo'][j]]} used {a}× vs {b}× so far"
        elif j == 3:
            detail = f"{-a:,} eligible rollouts vs {-b:,}"
        elif j == 4:
            detail = f"median {-a:,} turns vs {-b:,}"
        else:
            detail = "identical on every measured term; earlier row wins"
        decided = (f"Beat <span class=\"rname\">{esc(runner.get('name'))}</span> at "
                   f"<b>term {j+1} · {name}</b> — {detail}.")
    return why, decided


# ---------------------------------------------------------------------- charts

SERIES = 6  # validated categorical slots; anything past this folds into "Other"


def cat_colors(names: list[str]) -> dict:
    """Assign hue slots in a fixed, count-independent order (alphabetical), so a
    category keeps its colour no matter how the counts move between runs."""
    ordered = sorted(names)
    out = {}
    for i, n in enumerate(ordered):
        out[n] = f"var(--s{i+1})" if i < SERIES else "var(--s-other)"
    return out


def stat_tile(label, value, sub, wide=False):
    return (f'<div class="tile{" wide" if wide else ""}"><span class="t-l">{esc(label)}</span>'
            f'<span class="t-v">{esc(value)}</span><span class="t-s">{sub}</span></div>')


def composition(title, counter: collections.Counter, total: int, note: str) -> str:
    """Stacked composition bar -- or a stat tile when there is only one category,
    because a one-segment bar is a number pretending to be a chart."""
    if total == 0:
        return (f'<div class="chart"><h3>{esc(title)}</h3>'
                f'<p class="empty">No selected tasks to describe.</p></div>')
    items = counter.most_common()
    if len(items) == 1:
        name, n = items[0]
        return (f'<div class="chart"><h3>{esc(title)}</h3>'
                f'<div class="mono-hero"><span class="mh-v">{esc(name)}</span>'
                f'<span class="mh-s">every one of the {n} selected tasks · 1 distinct value</span></div>'
                f'<p class="note">{note}</p></div>')
    colors = cat_colors([n for n, _ in items])
    W, H, GAP = 1000.0, 30.0, 2.0
    segs, legend, x = [], [], 0.0
    span = W - GAP * (len(items) - 1)
    for name, n in items:
        w = span * n / total
        segs.append(f'<rect x="{x:.2f}" y="0" width="{max(w,1):.2f}" height="{H}" rx="2" '
                    f'fill="{colors[name]}"><title>{esc(name)}: {n} of {total} '
                    f'({n*100/total:.0f}%)</title></rect>')
        legend.append(f'<li><span class="sw" style="background:{colors[name]}"></span>'
                      f'<span class="lg-n">{esc(name)}</span>'
                      f'<span class="lg-v">{n} · {n*100/total:.0f}%</span></li>')
        x += w + GAP
    return (f'<div class="chart"><h3>{esc(title)}</h3>'
            f'<svg class="cbar" viewBox="0 0 {W:.0f} {H:.0f}" preserveAspectRatio="none" '
            f'role="img" aria-label="{esc(title)} composition of {total} tasks">'
            f'{"".join(segs)}</svg>'
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
        out.append(f'<rect class="bar" x="{PAD_L:.0f}" y="{y:.1f}" width="{w:.1f}" height="{BAR}" rx="3">'
                   f'<title>{esc(short_repo(name))}: {c} selected task{"s" if c != 1 else ""} '
                   f'(cap {REPO_CAP})</title></rect>')
        out.append(f'<text class="val" x="{PAD_L+w+8:.1f}" y="{y+BAR-2:.1f}">{c}</text>')
    out.append('</svg>')
    if tail:
        ones = sum(1 for _, c in tail if c == 1)
        out.append(f'<p class="note">{len(tail)} further repositories not plotted '
                   f'({ones} of them holding a single task).</p>')
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
    for i, r, t, n in pts:
        cx, cy = sx(t), sy(n)
        out.append(f'<circle class="dot" cx="{cx:.1f}" cy="{cy:.1f}" r="7">'
                   f'<title>#{i} {esc(r.get("name"))} — median {t:,} turns, '
                   f'{n:,} eligible rollouts</title></circle>')
        out.append(f'<text class="dotn" x="{cx:.1f}" y="{cy+3.5:.1f}" text-anchor="middle">{i}</text>')
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


def ribbon(picks: list[dict]) -> str:
    """One column per pick, one row per axis: filled = axis opened, hollow = spent."""
    cols = []
    for i, p in enumerate(picks, 1):
        cells = []
        for short, val, new, nth in axis_state(p["row"]):
            cls = "open" if new else "spent"
            glyph = "●" if new else "○"
            tip = (f"pick {i} · {AXIS_LABEL[short]} {val} — "
                   + ("opened (first use)" if new else f"repeat, use #{nth}"))
            cells.append(f'<span class="cell {cls}" title="{esc(tip)}"><span aria-hidden="true">'
                         f'{glyph}</span><span class="sr">{esc(tip)}</span></span>')
        cols.append(f'<div class="rcol"><span class="rn">{i}</span>{"".join(cells)}</div>')
    return ('<div class="ribbon-wrap"><div class="ribbon">'
            '<div class="rlab"><span class="rn">&nbsp;</span><span>language</span>'
            '<span>shape</span><span>repository</span></div>'
            + "".join(cols) + '</div></div>')
