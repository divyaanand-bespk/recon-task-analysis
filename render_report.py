#!/usr/bin/env python3
"""Render the pilot-analysis JSON as a standalone, shareable HTML report.

Separate from generate_pilot_analysis.py on purpose: that script owns the
measurement, this one owns presentation, so the report can be restyled without
touching the code that talks to Horizon.

    python3 render_report.py pilot-report.json -o pilot-report.html
"""
from __future__ import annotations
import argparse, collections, html, json, time
from pathlib import Path

PASS, FAIL, WARN = "ok", "bad", "wait"


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def pill(text: str, kind: str) -> str:
    return f'<span class="pill {kind}">{esc(text)}</span>'


def state_kind(value: str) -> str:
    v = (value or "").lower()
    if v.startswith("pass") or v == "yes":
        return PASS
    if v in ("fail", "no", "missing"):
        return FAIL
    return WARN


# The three variety axes, in the priority order the policy states. The order of
# this tuple IS the policy: language is spent last, repository first.
AXES = (("lang_key", "Language"), ("shape", "Shape"), ("repo_key", "Repository"))
UNKNOWN = "unknown"


def axis_value(row: dict, field: str) -> str:
    """The bucket a row occupies on one axis.

    Rows missing the field all share a single ``unknown`` bucket rather than
    each counting as its own fresh value. That is the conservative reading: an
    unmeasured language is not evidence of a *new* language, and letting five
    unlabelled tasks read as five distinct ones would make absence look like
    diversity. The report names the bucket so the reader can see it happened.
    """
    v = row.get(field)
    return UNKNOWN if v in (None, "") else str(v)


def diverse_order(rows: list[dict]) -> tuple[list[dict], dict]:
    """Order fit-for-pilot tasks so every axis of variety survives as long as possible.

    The key is lexicographic, and the order of its terms IS the policy:

      1. least-used LANGUAGE      rotate every language before any repeats
      2. least-used SHAPE         then every shape before any repeats
      3. least-used REPOSITORY    then every repository before any repeats
      4. most ELIGIBLE ROLLOUTS   among equally-varied picks, better-measured wins
      5. highest MEDIAN TURNS     then the longer-horizon task
      6. existing rank            exact ties only

    Diversity terms lead, so variety is only ever spent when nothing unused is
    left; the quality terms decide which of several equally-fresh candidates to
    take. Once every axis is exhausted the remaining fit tasks still appear --
    ranked by evidence -- rather than being dropped.

    Greedy round-robin is deliberate, not incidental. Because it always takes
    the least-used value, every PREFIX of the result maximises coverage: the
    top-50 cut gets as many languages, shapes and repositories as any ordering
    could put in 50 picks. Spreading a scarce language proportionally across the
    whole list would read as more even, but it pushes half of that language past
    the cut, so the pilot never sees it.

    A repeat on a low-priority axis is not always a failure, and the two cases
    have to be told apart:

      FORCED  no task left in the pool carries an unused value on that axis.
              Nothing could have been done; the pool simply ran out.
      TRADED  an unused value did exist, but taking it would have spent a
              HIGHER-priority axis, so the policy paid the cheaper repeat.

    Returns the ordered rows and a diagnostics dict describing, per axis, how
    much variety the pool actually held and when it ran out -- because "repeated
    at pick 3" means something very different when the pool holds two languages
    than when it holds twenty.
    """
    pool = [dict(r) for r in rows if r.get("fit") == "YES"]
    for rank, row in enumerate(pool):
        row["_rank"] = rank

    # Duplicate ids are a real hazard here: names and prefixes collide across
    # source and delivered copies of a task. Removing the pick by id would drop
    # every row sharing it, silently shrinking the golden set, so the pick is
    # removed by identity and the collision is reported instead of swallowed.
    ids = collections.Counter(r.get("task_id") for r in pool)
    duplicate_ids = sorted(str(k) for k, n in ids.items() if n > 1 and k)

    used = {field: collections.Counter() for field, _ in AXES}
    distinct = {field: len({axis_value(r, field) for r in pool}) for field, _ in AXES}
    unknown_n = {field: sum(1 for r in pool if axis_value(r, field) == UNKNOWN)
                 for field, _ in AXES}
    first_repeat: dict[str, int | None] = {field: None for field, _ in AXES}
    forced_n = {field: 0 for field, _ in AXES}
    traded_n = {field: 0 for field, _ in AXES}
    ordered: list[dict] = []

    def key(r: dict):
        return (used["lang_key"][axis_value(r, "lang_key")],
                used["shape"][axis_value(r, "shape")],
                used["repo_key"][axis_value(r, "repo_key")],
                -int(r.get("rollouts_n") or 0),
                -int(r.get("turns_median") or 0),
                r["_rank"])

    while pool:
        pick = min(pool, key=key)
        seq = len(ordered) + 1
        for field, _ in AXES:
            val = axis_value(pick, field)
            fresh = used[field][val] == 0
            pick[f"_{field}_fresh"] = fresh
            pick[f"_{field}_unknown"] = val == UNKNOWN
            if fresh:
                pick[f"_{field}_why"] = "fresh"
            else:
                # Could any remaining candidate have kept this axis fresh?
                avoidable = any(used[field][axis_value(c, field)] == 0 for c in pool)
                pick[f"_{field}_why"] = "traded" if avoidable else "forced"
                (traded_n if avoidable else forced_n)[field] += 1
                if first_repeat[field] is None:
                    first_repeat[field] = seq
            used[field][val] += 1
            pick[f"_{field}_n"] = used[field][val]
        pick["_fresh"] = all(pick[f"_{f}_fresh"] for f, _ in AXES)
        pick["_traded"] = any(pick[f"_{f}_why"] == "traded" for f, _ in AXES)
        ordered.append(pick)
        pool = [r for r in pool if r is not pick]

    axes = []
    for field, label in AXES:
        n = distinct[field]
        # With n distinct values the earliest a repeat can possibly occur is
        # pick n+1. Measuring the first repeat against that floor separates a
        # weak ordering from a pool that never held the variety to begin with.
        floor = n + 1
        got = first_repeat[field]
        axes.append({
            "field": field, "label": label, "distinct": n, "floor": floor,
            "first_repeat": got, "forced": forced_n[field], "traded": traded_n[field],
            "unknown": unknown_n[field],
            "optimal": got is None or got >= floor,
            "degenerate": n <= 1,
        })
    spent = [a for a in axes if a["first_repeat"]]
    diag = {
        "axes": axes,
        "total": len(ordered),
        "duplicate_ids": duplicate_ids,
        "traded_picks": sum(1 for g in ordered if g["_traded"]),
        # The first axis to run out, and the pick it ran out on -- the number a
        # reader actually needs, rather than "something repeated at pick 2".
        "first_spent": min(spent, key=lambda a: a["first_repeat"]) if spent else None,
        "all_fresh_through": next((i for i, g in enumerate(ordered, 1)
                                   if not g["_fresh"]), len(ordered) + 1) - 1,
    }
    return ordered, diag


def blockers(r: dict) -> list[str]:
    """Every unmet fit_for_pilot condition, in the order the gate checks them.

    Mirrors ``fit_for_pilot`` term for term:

        ai_rubrics == "Pass" and pass6 < 2 and pass6_denominator > 0
        and argus_main == "Pass" and (rp_complete_step or leading_rp) > 20

    Derived from all five gates rather than from failing rubric names alone: a
    task blocked by ABSENCE -- no reviews, no eligible rollouts -- has no failing
    rubric to name, and reporting an empty reason next to a NO makes the verdict
    look arbitrary. Absence is a blocker and has to say so.

    Every comparison is deliberately the gate's own comparison. Argus is matched
    exactly, not by prefix: the gate accepts only the literal "Pass", so a
    prefix test here would let some future "Pass (advisory)" render a NO with no
    reason beside it. A missing pass@6 count is reported, never defaulted to a
    passing 0, for the same reason.
    """
    out: list[str] = []
    rub = r.get("ai_rubrics")
    if rub in (None, "", "None"):
        out.append("no AI rubric reviews on any variant")
    elif rub != "Pass":
        failing = r.get("failing_rubrics") or []
        out.append("; ".join(failing) if failing else "AI rubrics fail")

    argus = str(r.get("argus_main") or "")
    if argus != "Pass":
        out.append(f"Argus Main {argus or 'missing'}")

    denom = int(r.get("pass6_denominator") or 0)
    solved = r.get("pass6")
    if denom == 0:
        out.append("no eligible rollouts")
    elif solved is None:
        out.append("pass@6 not measured")
    elif int(solved) >= 2:
        out.append(f"solved {solved}/{denom} (needs <2)")

    step = r.get("rp_complete_step")
    gate = int(step) if step is not None else int(r.get("leading_rp") or 0)
    if gate <= 20:
        out.append(f"R/P gate {gate} (needs >20)")
    return out


def model_cell(m: dict | None) -> str:
    if not m or not m.get("denominator"):
        return '<td class="num dim">—</td>'
    solved, n = m["pass6"], m["denominator"]
    kind = PASS if solved < 2 else FAIL
    turns = m.get("median_turns")
    t = f'<span class="turns">{turns}t</span>' if turns is not None else ""
    return (f'<td class="num"><span class="ratio {kind}">{solved}/{n}</span>{t}</td>')


CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --paper:#ffffff; --raise:#f7f8fa; --ink:#14181d; --muted:#5b6672; --faint:#8a939e;
  --rule:#e3e7ec; --accent:#1a5f7a; --accent-soft:#e8f0f3;
  --ok:#0f7b4f; --ok-bg:#e7f4ee; --bad:#c0392b; --bad-bg:#fbecea; --wait:#9a6a00; --wait-bg:#fdf3e0;
  --shadow:0 1px 2px rgba(20,24,29,.05),0 1px 8px rgba(20,24,29,.04);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#101418; --raise:#171c22; --ink:#e8ecf1; --muted:#9aa5b1; --faint:#6b7681;
  --rule:#252c34; --accent:#5fa8c4; --accent-soft:#16303a;
  --ok:#4cc38a; --ok-bg:#12291f; --bad:#f07167; --bad-bg:#2c1715; --wait:#e0a72c; --wait-bg:#2a2113;
  --shadow:0 1px 2px rgba(0,0,0,.4);
}}
:root[data-theme="dark"]{
  --paper:#101418; --raise:#171c22; --ink:#e8ecf1; --muted:#9aa5b1; --faint:#6b7681;
  --rule:#252c34; --accent:#5fa8c4; --accent-soft:#16303a;
  --ok:#4cc38a; --ok-bg:#12291f; --bad:#f07167; --bad-bg:#2c1715; --wait:#e0a72c; --wait-bg:#2a2113;
  --shadow:0 1px 2px rgba(0,0,0,.4);
}
body{margin:0;background:var(--paper);color:var(--ink);
  font:15px/1.55 "IBM Plex Sans",ui-sans-serif,system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;}
.wrap{max-width:1500px;margin:0 auto;padding:36px 28px 64px;display:flex;flex-direction:column;gap:26px}
header{display:flex;flex-direction:column;gap:5px}
.eyebrow{font:600 11px/1 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.14em;
  text-transform:uppercase;color:var(--accent)}
h1{margin:0;font-family:Archivo,ui-sans-serif,system-ui,sans-serif;font-weight:700;
  font-size:clamp(26px,3vw,34px);letter-spacing:-.022em;text-wrap:balance}
.lede{margin:0;color:var(--muted);font-size:14.5px;max-width:68ch}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px}
.kpi{background:var(--raise);border:1px solid var(--rule);border-radius:11px;padding:14px 16px;
  display:flex;flex-direction:column;gap:3px;box-shadow:var(--shadow)}
.kpi .k{font:600 11px/1 "IBM Plex Mono",monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.kpi .v{font-family:Archivo,sans-serif;font-weight:700;font-size:29px;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;line-height:1.1}
.kpi .s{font-size:12.5px;color:var(--faint)}
.kpi.lead .v{color:var(--accent)}
.bar{background:var(--raise);border:1px solid var(--rule);border-radius:11px;padding:14px 16px;
  display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px 16px}
.f{display:flex;flex-direction:column;gap:5px;min-width:0}
.f span{font:600 10.5px/1 "IBM Plex Mono",monospace;letter-spacing:.09em;text-transform:uppercase;color:var(--muted)}
.f select{font:inherit;font-size:13.5px;color:var(--ink);background:var(--paper);
  border:1px solid var(--rule);border-radius:8px;padding:7px 10px;min-width:126px;max-width:210px}
.f select:focus-visible,#reset:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.grow{flex:1 1 auto}
.tools{display:flex;align-items:center;gap:12px}
#count{font:500 13px/1 "IBM Plex Mono",monospace;color:var(--muted);white-space:nowrap;
  font-variant-numeric:tabular-nums}
#reset{font:inherit;font-size:13px;color:var(--ink);background:var(--paper);border:1px solid var(--rule);
  border-radius:8px;padding:8px 13px;cursor:pointer}
#reset:hover:not([disabled]){border-color:var(--accent);color:var(--accent)}
#reset[disabled]{opacity:.4;cursor:default}
.railtop{overflow-x:auto;overflow-y:hidden;height:12px;border:1px solid var(--rule);
  border-bottom:0;border-radius:11px 11px 0 0;background:var(--raise)}
.railtop > div{height:1px}
.railtop + .tablecard{border-radius:0 0 11px 11px}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--rule);margin-bottom:-8px}
.tab{font:600 13px/1 "IBM Plex Sans",sans-serif;color:var(--muted);background:none;border:0;
  border-bottom:2px solid transparent;padding:11px 15px;cursor:pointer;border-radius:7px 7px 0 0}
.tab:hover{color:var(--ink)}
.tab[aria-selected="true"]{color:var(--accent);border-bottom-color:var(--accent);background:var(--accent-soft)}
.tab:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.panel[hidden]{display:none}
.panel{display:flex;flex-direction:column;gap:18px}
.note{color:var(--muted);font-size:13.5px;max-width:78ch;margin:0}
.seq{display:inline-block;min-width:22px;font:600 12px/1 "IBM Plex Mono",monospace;color:var(--faint)}
.tag{display:inline-block;font:600 10.5px/1.6 "IBM Plex Mono",monospace;letter-spacing:.05em;
  text-transform:uppercase;padding:1px 7px;border-radius:5px;margin-left:6px}
.tag.fresh{background:var(--ok-bg);color:var(--ok)}
/* A traded repeat is a decision the ordering made, a forced one is the pool
   running dry. Only the first is something a reader can act on, so only the
   first is painted as a warning; forced stays neutral. */
.tag.traded{background:var(--wait-bg);color:var(--wait)}
.tag.forced{background:var(--rule);color:var(--muted)}
h2.sub{margin:6px 0 -6px;font-family:Archivo,ui-sans-serif,system-ui,sans-serif;
  font-weight:700;font-size:17px;letter-spacing:-.015em}
.warnline{border-left:3px solid var(--wait);background:var(--wait-bg);color:var(--ink);
  padding:10px 14px;border-radius:0 8px 8px 0;max-width:none}
tr.cutrow td{background:var(--accent-soft);color:var(--accent);font:600 12px/1.5 "IBM Plex Mono",monospace;
  white-space:normal;position:static}
tr.cutrow td:first-child{position:static}
tr.past-cut td{opacity:.62}
tbody tr.past-cut:hover td{opacity:1}
.tablecard{border:1px solid var(--rule);border-radius:11px;overflow:hidden;box-shadow:var(--shadow)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:13.5px}
th,td{padding:11px 13px;text-align:left;border-bottom:1px solid var(--rule);white-space:nowrap;
  background:var(--paper)}
thead th{position:sticky;top:0;z-index:3;background:var(--raise);color:var(--muted);
  font:600 10.5px/1.3 "IBM Plex Mono",monospace;letter-spacing:.07em;text-transform:uppercase;
  border-bottom:1px solid var(--rule)}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--accent-soft)}
th:first-child,td:first-child{position:sticky;left:0;z-index:2;min-width:260px;white-space:normal}
thead th:first-child{z-index:4}
.name{font-weight:600;display:block;letter-spacing:-.005em}
.tid{font:400 11.5px/1.4 "IBM Plex Mono",monospace;color:var(--accent);text-decoration:none;
  border-bottom:1px solid transparent}
.tid:hover{border-bottom-color:currentColor}
.prov{display:block;font:400 11px/1.4 "IBM Plex Mono",monospace;color:var(--faint);margin-top:2px}
.pill{display:inline-block;padding:2.5px 9px;border-radius:999px;font:600 11.5px/1.5 "IBM Plex Sans",sans-serif}
.pill.ok{background:var(--ok-bg);color:var(--ok)} .pill.bad{background:var(--bad-bg);color:var(--bad)}
.pill.wait{background:var(--wait-bg);color:var(--wait)}
.num{text-align:right;font-variant-numeric:tabular-nums;font-family:"IBM Plex Mono",monospace}
.ratio{font-weight:600}.ratio.ok{color:var(--ok)}.ratio.bad{color:var(--bad)}
.turns{color:var(--faint);margin-left:6px;font-size:12px}
.dim{color:var(--faint)}
.why{white-space:normal;max-width:260px;color:var(--muted);font-size:12.5px}
.repo{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--muted)}
footer{color:var(--faint);font-size:12.5px;display:flex;flex-direction:column;gap:6px;
  border-top:1px solid var(--rule);padding-top:16px}
footer b{color:var(--muted);font-weight:600}
.empty{padding:40px;text-align:center;color:var(--faint)}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""


def build(rows: list[dict], generated: str, target: int = 50) -> str:
    fit = sum(1 for r in rows if r.get("fit") == "YES")
    rub = sum(1 for r in rows if r.get("ai_rubrics") == "Pass")
    arg = sum(1 for r in rows if r.get("argus_main") == "Pass")
    p6 = sum(1 for r in rows if (r.get("pass6_denominator") or 0) > 0
             and (r.get("pass6") or 0) < 2)
    langs = collections.Counter(r.get("lang_key") or "unknown" for r in rows if r.get("fit") == "YES")
    repos = collections.Counter(r.get("repo_key") or "unknown" for r in rows if r.get("fit") == "YES")
    top_repo = max(repos.values()) if repos else 0

    golden, diag = diverse_order(rows)
    g_lang = collections.Counter(axis_value(g, "lang_key") for g in golden)
    g_repo = collections.Counter(axis_value(g, "repo_key") for g in golden)
    cut = min(target, len(golden))
    short = max(0, target - len(golden))
    top = golden[:cut]
    t_lang = len({axis_value(g, "lang_key") for g in top})
    t_repo = len({axis_value(g, "repo_key") for g in top})
    t_shape = len({axis_value(g, "shape") for g in top})

    def axis_tag(g: dict, field: str) -> str:
        """Fresh / traded / forced -- three states, because they mean different things.

        A FORCED repeat is not a defect: the pool held nothing else. Painting it
        the same colour as a repeat the ordering chose would tell the reader to
        go fix something that cannot be fixed.
        """
        why, n = g[f"_{field}_why"], g[f"_{field}_n"]
        if why == "fresh":
            return '<span class="tag fresh">new</span>'
        if why == "traded":
            return f'<span class="tag traded" title="an unused value was available; a higher-priority axis outranked it">#{n} traded</span>'
        return f'<span class="tag forced" title="no unused value remained in the pool">#{n} forced</span>'

    grows = []
    for i, g in enumerate(golden, 1):
        lang = axis_value(g, "lang_key")
        repo = axis_value(g, "repo_key")
        cls = ' class="past-cut"' if i > cut else ""
        grows.append(
            f"<tr{cls}>"
            f'<td><span class="seq">{i}</span><span class="name">{esc(g.get("name"))}</span>'
            f'<a class="tid" href="https://horizon.bespokelabs.ai/tasks/{esc(g.get("task_id"))}"'
            f' target="_blank" rel="noopener noreferrer">{esc(g.get("task_id"))}</a></td>'
            f'<td>{esc("unknown" if lang == UNKNOWN else lang)}{axis_tag(g, "lang_key")}</td>'
            f'<td class="repo">{esc(repo.replace("github.com/", ""))}{axis_tag(g, "repo_key")}</td>'
            f'<td>{pill(g.get("argus_main") or "—", state_kind(g.get("argus_main")))}</td>'
            f'<td class="num">{esc(g.get("rollouts_n") if g.get("rollouts_n") is not None else "—")}</td>'
            f'<td class="num">{esc(g.get("turns_median") if g.get("turns_median") is not None else "—")}</td>'
            f'<td class="num">{esc(g.get("leading_rp"))}</td>'
            f'<td class="dim">{esc(g.get("shape") or "unknown")}{axis_tag(g, "shape")}</td>'
            "</tr>")
        if i == cut and cut < len(golden):
            grows.append(
                f'<tr class="cutrow"><td colspan="8">Pilot target reached &mdash; '
                f'{cut} tasks above this line. The {len(golden) - cut} below stay '
                f'fit for pilot and remain ranked, but fall outside the top {target}.'
                f'</td></tr>')

    # Repeat pressure: per axis, how much variety the pool held and when it ran
    # out, measured against the floor (distinct + 1) rather than against zero.
    arows = []
    for a in diag["axes"]:
        got = a["first_repeat"]
        if got is None:
            verdict, kind = "never repeated", PASS
        elif a["degenerate"]:
            verdict, kind = "only one value in the pool", WARN
        elif a["optimal"]:
            verdict, kind = "optimal — repeat was unavoidable", PASS
        else:
            verdict, kind = f"repeated {a['floor'] - got} pick(s) early", WARN
        unk = (f' <span class="dim">({a["unknown"]} unmeasured, counted as one)</span>'
               if a["unknown"] else "")
        arows.append(
            "<tr>"
            f'<td><span class="name">{esc(a["label"])}</span></td>'
            f'<td class="num">{a["distinct"]}{unk}</td>'
            f'<td class="num">{a["floor"]}</td>'
            f'<td class="num">{got if got else "—"}</td>'
            f'<td class="num">{a["forced"]}</td>'
            f'<td class="num">{a["traded"]}</td>'
            f'<td>{pill(verdict, kind)}</td>'
            "</tr>")

    fs = diag["first_spent"]
    if fs is None:
        pressure = "No axis ever repeated: every pick was fresh on all three."
    else:
        pressure = (f'<b>{esc(fs["label"])} ran out first, at pick {fs["first_repeat"]}</b> '
                    f'&mdash; the pool held {fs["distinct"]} distinct '
                    f'{esc(fs["label"].lower())} value'
                    f'{"" if fs["distinct"] == 1 else "s"}, so pick {fs["floor"]} '
                    f'was the earliest a repeat could occur.')
    dupe = (f'<p class="note warnline"><b>{len(diag["duplicate_ids"])} duplicate task id(s)</b> '
            f'in the fit pool: {esc(", ".join(diag["duplicate_ids"]))}. Each row is still '
            f'ordered and shown; verify they are genuinely distinct tasks.</p>'
            if diag["duplicate_ids"] else "")
    shortline = (f'<p class="note warnline"><b>Short of target by {short}.</b> The pilot wants '
                 f'{target} tasks and only {len(golden)} are fit for pilot. No rule was '
                 f'relaxed to close the gap &mdash; the shortfall is the finding.</p>'
                 if short else "")

    body = []
    for r in rows:
        pm = r.get("per_model") or {}
        why = "; ".join(blockers(r))
        body.append(
            "<tr>"
            f'<td><span class="name">{esc(r.get("name"))}</span>'
            f'<a class="tid" href="https://horizon.bespokelabs.ai/tasks/{esc(r.get("task_id"))}"'
            f' target="_blank" rel="noopener noreferrer">{esc(r.get("task_id"))}</a>'
            f'<span class="prov">rubrics:{esc(r.get("rubrics_source") or "—")}'
            f' · rollouts:{esc(r.get("rollouts_source") or "—")}</span></td>'
            f'<td>{pill(r.get("fit") or "—", state_kind(r.get("fit")))}</td>'
            f'<td>{pill(r.get("ai_rubrics") or "—", state_kind(r.get("ai_rubrics")))}</td>'
            f'<td>{pill(r.get("argus_main") or "—", state_kind(r.get("argus_main")))}</td>'
            f'{model_cell(pm.get("glm"))}{model_cell(pm.get("router"))}{model_cell(pm.get("starfall"))}'
            f'<td class="num">{esc(r.get("turns_median") if r.get("turns_median") is not None else "—")}</td>'
            f'<td class="num dim">{esc(r.get("turns_max") if r.get("turns_max") is not None else "—")}</td>'
            f'<td class="num">{esc(r.get("rollouts_n") if r.get("rollouts_n") is not None else "—")}</td>'
            f'<td class="num">{esc(r.get("leading_rp"))}</td>'
            f'<td class="num">{esc(r.get("rp_complete_step") if r.get("rp_complete_step") is not None else "—")}</td>'
            f'<td class="num">{esc(r.get("total_rp"))}</td>'
            f'<td class="num">{esc(r.get("tool_calls"))}</td>'
            f'<td>{esc(r.get("lang_key") or "—")}</td>'
            f'<td class="repo">{esc((r.get("repo_key") or "—").replace("github.com/",""))}</td>'
            f'<td class="dim">{esc(r.get("shape"))}</td>'
            f'<td class="why">{esc(why)}</td>'
            "</tr>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pilot Readiness</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style></head>
<body><div class="wrap">
<header>
  <div class="eyebrow">Voyager · AlphaRecon</div>
  <h1>Pilot Readiness</h1>
  <p class="lede">{len(rows)} candidate tasks, each shipped as up to three variants.
  Every signal resolves to the latest run on the first variant that carries it, and the
  source is shown on each row. Generated {esc(generated)}.</p>
</header>

<section class="kpis">
  <div class="kpi lead"><span class="k">Fit for pilot</span><span class="v">{fit}</span><span class="s">of {len(rows)} tasks</span></div>
  <div class="kpi"><span class="k">AI rubrics pass</span><span class="v">{rub}</span><span class="s">of {len(rows)}</span></div>
  <div class="kpi"><span class="k">Argus Main pass</span><span class="v">{arg}</span><span class="s">of {len(rows)}</span></div>
  <div class="kpi"><span class="k">Pass@6 below 2</span><span class="v">{p6}</span><span class="s">of {len(rows)}</span></div>
  <div class="kpi"><span class="k">Max per repo</span><span class="v">{top_repo}</span><span class="s">cap is 3 · {len(repos)} repos</span></div>
  <div class="kpi"><span class="k">Languages</span><span class="v">{len(langs)}</span><span class="s">{esc(", ".join(f"{k} {v}" for k,v in langs.most_common(3)))}</span></div>
</section>

<div class="tabs" role="tablist">
  <button class="tab" id="tab-all" role="tab" aria-selected="true" aria-controls="panel-all">All tasks</button>
  <button class="tab" id="tab-div" role="tab" aria-selected="false" aria-controls="panel-div">Maximum diversity</button>
</div>

<div class="panel" id="panel-all" role="tabpanel" aria-labelledby="tab-all">
<div class="bar">
  <label class="f" for="f-fit"><span>Fit</span><select id="f-fit"><option value="all">Any</option><option>YES</option><option>NO</option></select></label>
  <label class="f" for="f-rub"><span>AI rubrics</span><select id="f-rub"><option value="all">Any</option><option>Pass</option><option>Fail</option><option>None</option></select></label>
  <label class="f" for="f-arg"><span>Argus Main</span><select id="f-arg"><option value="all">Any</option><option>Pass</option><option>Fail</option><option>In Progress</option><option>Missing</option></select></label>
  <label class="f" for="f-glm"><span>GLM</span><select id="f-glm"><option value="all">Any</option><option value="fails">Fails &lt;2</option><option value="solves">Solves ≥2</option><option value="none">No rollouts</option></select></label>
  <label class="f" for="f-router"><span>Router</span><select id="f-router"><option value="all">Any</option><option value="fails">Fails &lt;2</option><option value="solves">Solves ≥2</option><option value="none">No rollouts</option></select></label>
  <label class="f" for="f-star"><span>Starfall</span><select id="f-star"><option value="all">Any</option><option value="fails">Fails &lt;2</option><option value="solves">Solves ≥2</option><option value="none">No rollouts</option></select></label>
  <label class="f" for="f-repo"><span>Repository</span><select id="f-repo"><option value="all">All</option></select></label>
  <label class="f" for="f-lang"><span>Language</span><select id="f-lang"><option value="all">All</option></select></label>
  <label class="f" for="f-shape"><span>Shape</span><select id="f-shape"><option value="all">All</option></select></label>
  <span class="grow"></span>
  <span class="tools"><span id="count"></span><button id="reset" type="button">Reset filters</button></span>
</div>

<div class="railtop" id="railtop" aria-hidden="true"><div id="railspacer"></div></div>
<div class="tablecard"><div class="scroll" id="scrollmain"><table id="tbl">
<thead><tr>
<th>Task</th><th>Fit</th><th>AI rubrics</th><th>Argus Main</th>
<th class="num">GLM</th><th class="num">Router</th><th class="num">Starfall</th>
<th class="num">Turns med</th><th class="num">Turns max</th><th class="num">Rollouts</th><th class="num">Leading R/P</th><th class="num">R/P step</th><th class="num">Total R/P</th><th class="num">Tool calls</th>
<th>Language</th><th>Repository</th><th>Shape</th><th>Blocked by</th>
</tr></thead>
<tbody id="rows">{''.join(body)}</tbody>
</table></div></div>
</div>

<div class="panel" id="panel-div" role="tabpanel" aria-labelledby="tab-div" hidden>
  <p class="note">The {len(golden)} fit-for-pilot tasks, ordered so variety survives as long as
  possible. At each step the least-used <b>language</b> wins; ties go to the least-used
  <b>shape</b>, then the least-used <b>repository</b>; among candidates equally fresh on all
  three, the better-measured task wins &mdash; more eligible rollouts first, then higher median
  turns. A repeat only ever happens when the axes above it leave no alternative.
  {pressure}</p>
  {shortline}{dupe}
  <section class="kpis">
    <div class="kpi lead"><span class="k">Pilot cut</span><span class="v">{cut}</span><span class="s">of {len(golden)} fit · target {target}</span></div>
    <div class="kpi"><span class="k">In the cut</span><span class="v">{t_lang}&#8202;/&#8202;{t_shape}&#8202;/&#8202;{t_repo}</span><span class="s">languages / shapes / repos</span></div>
    <div class="kpi"><span class="k">All-fresh streak</span><span class="v">{diag["all_fresh_through"]}</span><span class="s">picks fresh on every axis</span></div>
    <div class="kpi"><span class="k">Traded repeats</span><span class="v">{diag["traded_picks"]}</span><span class="s">of {len(golden)} picks · others fresh or forced</span></div>
    <div class="kpi"><span class="k">Repositories</span><span class="v">{len(g_repo)}</span><span class="s">max {max(g_repo.values()) if g_repo else 0} per repo</span></div>
    <div class="kpi"><span class="k">Languages</span><span class="v">{len(g_lang)}</span><span class="s">{esc(", ".join(f"{k} x{v}" for k, v in g_lang.most_common()))}</span></div>
  </section>

  <h2 class="sub">Repeat pressure</h2>
  <p class="note">How much variety the pool actually held on each axis, and when it ran out.
  A first repeat at pick <i>n</i> means nothing on its own &mdash; it has to be read against
  the <b>floor</b>, the earliest pick at which a repeat becomes arithmetically unavoidable
  (distinct values + 1). <b>Forced</b> counts repeats where nothing unused was left in the
  pool; <b>traded</b> counts repeats the ordering accepted on purpose, to keep a
  higher-priority axis fresh. Only a traded repeat reflects a choice.</p>
  <div class="tablecard"><div class="scroll"><table>
  <thead><tr><th>Axis</th><th class="num">Distinct values</th><th class="num">Floor</th>
  <th class="num">First repeat</th><th class="num">Forced</th><th class="num">Traded</th>
  <th>Verdict</th></tr></thead>
  <tbody>{''.join(arows)}</tbody>
  </table></div></div>

  <h2 class="sub">Ordering</h2>
  <div class="tablecard"><div class="scroll"><table>
  <thead><tr><th>Pick</th><th>Language</th><th>Repository</th><th>Argus Main</th>
  <th class="num">Rollouts</th><th class="num">Turns med</th><th class="num">Leading R/P</th><th>Shape</th></tr></thead>
  <tbody>{''.join(grows)}</tbody>
  </table></div></div>
</div>

<footer>
<div><b>Fit for pilot</b> requires all of: AI rubrics pass · Pass@6 below 2 · at least one eligible rollout · Argus Main pass · R/P gate above 20.</div>
<div><b>Variant resolution</b> — within a variant only the latest run of a rubric counts; across variants, in order final → binary → partial, the first passing run settles it.</div>
<div><b>Absence is not a pass</b> — a task with no reviews reads <em>None</em>, and no rollouts reads <em>—</em>, never as a silent success.</div>
</footer>
</div>
<script>
const rows=[...document.querySelectorAll('#rows tr')];
const data=rows.map(tr=>({{tr,
  fit:tr.children[1].textContent.trim(), rub:tr.children[2].textContent.trim(),
  arg:tr.children[3].textContent.trim(),
  glm:tr.children[4].textContent.trim(), router:tr.children[5].textContent.trim(),
  star:tr.children[6].textContent.trim(),
  lang:tr.children[11].textContent.trim(), repo:tr.children[12].textContent.trim(),
  shape:tr.children[13].textContent.trim()}}));
const F=id=>document.querySelector(id);
const sel={{fit:F('#f-fit'),rub:F('#f-rub'),arg:F('#f-arg'),glm:F('#f-glm'),
  router:F('#f-router'),star:F('#f-star'),repo:F('#f-repo'),lang:F('#f-lang'),shape:F('#f-shape')}};
for(const [key,el] of [['repo',sel.repo],['lang',sel.lang],['shape',sel.shape]])
  [...new Set(data.map(d=>d[key]).filter(v=>v&&v!=='—'))].sort()
    .forEach(v=>{{const o=document.createElement('option');o.textContent=v;el.appendChild(o);}});
const modelOk=(txt,want)=>{{
  if(want==='all')return true;
  if(txt==='—')return want==='none';
  if(want==='none')return false;
  const solved=parseInt(txt,10);
  return want==='fails'?solved<2:solved>=2;
}};
const count=F('#count'), reset=F('#reset');
function render(){{
  let shown=0;
  for(const d of data){{
    const ok=(sel.fit.value==='all'||d.fit===sel.fit.value)
      &&(sel.rub.value==='all'||d.rub===sel.rub.value)
      &&(sel.arg.value==='all'||d.arg===sel.arg.value)
      &&modelOk(d.glm,sel.glm.value)&&modelOk(d.router,sel.router.value)&&modelOk(d.star,sel.star.value)
      &&(sel.repo.value==='all'||d.repo===sel.repo.value)
      &&(sel.lang.value==='all'||d.lang===sel.lang.value)
      &&(sel.shape.value==='all'||d.shape===sel.shape.value);
    d.tr.hidden=!ok; if(ok)shown++;
  }}
  const active=Object.values(sel).filter(s=>s.value!=='all').length;
  count.textContent=active?`${{shown}} of ${{data.length}} · ${{active}} filter${{active>1?'s':''}}`:`${{data.length}} tasks`;
  reset.disabled=!active;
  if(typeof sizeRail==='function')sizeRail();
}}
// A table this wide is unusable if the only scrollbar is below the fold, so a
// mirrored rail sits above it. Each rail writes to the other while guarding
// against the echo the write itself would trigger.
const railTop=document.querySelector('#railtop'), railSpacer=document.querySelector('#railspacer'),
      scrollMain=document.querySelector('#scrollmain'), tbl=document.querySelector('#tbl');
function sizeRail(){{railSpacer.style.width=tbl.scrollWidth+'px';}}
let syncing=false;
const link=(from,to)=>from.addEventListener('scroll',()=>{{
  if(syncing){{syncing=false;return;}} syncing=true; to.scrollLeft=from.scrollLeft;}});
link(railTop,scrollMain); link(scrollMain,railTop);
sizeRail();
new ResizeObserver(sizeRail).observe(tbl);
addEventListener('resize',sizeRail);

const tabs=[['#tab-all','#panel-all'],['#tab-div','#panel-div']];
for(const [tid,pid] of tabs){{
  document.querySelector(tid).addEventListener('click',()=>{{
    for(const [t,p] of tabs){{
      const on=t===tid;
      document.querySelector(t).setAttribute('aria-selected',on);
      document.querySelector(p).hidden=!on;
    }}
    if(tid==='#tab-all')sizeRail();
  }});
}}

Object.values(sel).forEach(s=>s.addEventListener('change',render));
reset.addEventListener('click',()=>{{Object.values(sel).forEach(s=>s.value='all');render();}});
render();
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--target", type=int, default=50,
                    help="pilot target size; the golden tab marks the cut at this "
                         "many tasks and states the shortfall if the pool is smaller")
    a = ap.parse_args()
    payload = json.loads(Path(a.json).read_text())
    rows = payload.get("tasks", [])
    rows.sort(key=lambda r: (r.get("fit") != "YES", -(r.get("leading_rp") or 0),
                             str(r.get("name") or "").lower()))
    Path(a.out).write_text(build(rows, time.strftime("%d %B %Y", time.gmtime()), a.target),
                           encoding="utf-8")
    fit = sum(1 for r in rows if r.get("fit") == "YES")
    note = f", {a.target - fit} SHORT of target {a.target}" if fit < a.target else ""
    print(f"wrote {a.out}  ({len(rows)} tasks, {fit} fit{note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
