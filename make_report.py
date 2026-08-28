#!/usr/bin/env python3
"""Render the handover report: every task in pick order, plus the mix.

This is the page that gets shared, so it lives in the repo rather than in
someone's scratch directory -- a report nobody can regenerate is a report that
goes stale the moment the sheet changes.

It deliberately shows NO pass/fail verdict per task. The list includes every
task in the sheet, so a red "Fail" chip beside an included task only invites the
question the list has already answered. Rollouts, turns and research-and-planning
stay, because they are measurements rather than judgements.

    python make_report.py runs/pilot.json -o out/handover.html
"""
from __future__ import annotations
import argparse, collections, html, importlib.util, json, time
from pathlib import Path

CSS = """
:root{
  --ground:#fbfcfc; --raised:#ffffff; --sunk:#f2f5f5;
  --ink:#141c1c; --ink-2:#3d4a4a; --ink-3:#69797a;
  --rule:#dde4e4; --rule-2:#eaeeee;
  --accent:#0f5f59; --accent-soft:#e2efee;
  --pass:#2c6344; --pass-bg:#e6f1ea;
  --warn:#8a5313; --warn-bg:#f8eddd;
  --fail:#8f322a; --fail-bg:#f8e6e3;
  --none:#5c6a6b; --none-bg:#eceff0;
  --shadow:0 1px 2px rgba(20,28,28,.05),0 8px 24px -16px rgba(20,28,28,.25);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#0e1414; --raised:#151d1d; --sunk:#111818;
    --ink:#e6ecec; --ink-2:#b3c0c0; --ink-3:#7e8d8e;
    --rule:#26312f; --rule-2:#1d2726;
    --accent:#5fbfb2; --accent-soft:#16302e;
    --pass:#7fc79b; --pass-bg:#152520;
    --warn:#d8a463; --warn-bg:#2a2117;
    --fail:#e08f83; --fail-bg:#2b1a18;
    --none:#8b9899; --none-bg:#1b2323;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -16px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"]{
  --ground:#0e1414; --raised:#151d1d; --sunk:#111818;
  --ink:#e6ecec; --ink-2:#b3c0c0; --ink-3:#7e8d8e;
  --rule:#26312f; --rule-2:#1d2726;
  --accent:#5fbfb2; --accent-soft:#16302e;
  --pass:#7fc79b; --pass-bg:#152520;
  --warn:#d8a463; --warn-bg:#2a2117;
  --fail:#e08f83; --fail-bg:#2b1a18;
  --none:#8b9899; --none-bg:#1b2323;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -16px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font-family:"IBM Plex Sans",system-ui,-apple-system,sans-serif;font-size:16px;line-height:1.62;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:74rem;margin:0 auto;padding:clamp(2rem,5vw,4.5rem) clamp(1.1rem,4vw,2.5rem) 6rem}
.prose{max-width:40rem}
h1,h2,h3{font-family:Newsreader,Georgia,serif;font-weight:500;text-wrap:balance;margin:0}
h1{font-size:clamp(2.1rem,5.2vw,3.15rem);line-height:1.1;letter-spacing:-.015em}
h2{font-size:clamp(1.45rem,3vw,1.85rem);line-height:1.2;letter-spacing:-.01em}
h3{font-size:1.08rem;line-height:1.35;font-weight:600;margin-top:1.6rem}
p{margin:0 0 1.05rem}
a{color:var(--accent);text-underline-offset:3px;text-decoration-thickness:1px}
a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:3px}
code{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:.875em;
  background:var(--sunk);padding:.12em .38em;border-radius:3px;border:1px solid var(--rule-2)}
strong{font-weight:600;color:var(--ink)}
.num{font-variant-numeric:tabular-nums}
header{border-bottom:1px solid var(--rule);padding-bottom:2.25rem;margin-bottom:2.75rem}
.eyebrow{font-family:"IBM Plex Mono",monospace;font-size:.7rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--accent);margin:0 0 1.1rem}
.standfirst{font-size:1.14rem;color:var(--ink-2);margin-top:1.15rem;max-width:44rem}
.meta{font-family:"IBM Plex Mono",monospace;font-size:.76rem;color:var(--ink-3);
  margin-top:1.6rem;display:flex;flex-wrap:wrap;gap:.5rem 1.4rem}
section{margin:3.5rem 0 0}
section>h2{padding-bottom:.7rem;border-bottom:1px solid var(--rule-2);margin-bottom:1.5rem}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));gap:.85rem;margin:2rem 0 0}
.tile{background:var(--raised);border:1px solid var(--rule);border-radius:9px;
  padding:1.05rem 1.15rem;box-shadow:var(--shadow);display:block}
.tile .v{font-family:Newsreader,Georgia,serif;font-size:2.1rem;line-height:1;
  font-variant-numeric:tabular-nums;display:block}
.tile .k{font-family:"IBM Plex Mono",monospace;font-size:.68rem;letter-spacing:.09em;
  text-transform:uppercase;color:var(--ink-3);display:block;margin-top:.5rem}
.tile .s{font-size:.83rem;color:var(--ink-2);display:block;margin-top:.35rem}
.tile.flag{border-color:var(--fail);background:var(--fail-bg)}
.tile.flag .v{color:var(--fail)}
.callout{border-left:3px solid var(--fail);background:var(--fail-bg);
  padding:1.15rem 1.35rem;border-radius:0 8px 8px 0;margin:1.6rem 0}
.callout.ok{border-left-color:var(--pass);background:var(--pass-bg)}
.callout.note{border-left-color:var(--accent);background:var(--accent-soft)}
.callout h3{margin:0 0 .5rem}
.callout p:last-child{margin-bottom:0}
.scroll{overflow-x:auto;margin:1.2rem 0;border:1px solid var(--rule);border-radius:9px;background:var(--raised)}
.scroll.tall{max-height:36rem;overflow-y:auto}
.scroll.tall thead th{position:sticky;top:0;z-index:2}
table{border-collapse:collapse;width:100%;font-size:.875rem}
th,td{text-align:left;padding:.62rem .85rem;border-bottom:1px solid var(--rule-2);vertical-align:top}
thead th{font-family:"IBM Plex Mono",monospace;font-size:.66rem;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-3);background:var(--sunk);
  border-bottom:1px solid var(--rule);white-space:nowrap;font-weight:500}
tbody tr:last-child td{border-bottom:none}
td.n{text-align:right;font-variant-numeric:tabular-nums;font-family:"IBM Plex Mono",monospace}
td .sub,.sub{font-size:.74rem;color:var(--ink-3);font-family:"IBM Plex Mono",monospace}
td .sub{margin-top:.15rem}
td.repo{font-family:"IBM Plex Mono",monospace;font-size:.79rem;color:var(--ink-2)}
td.axes{white-space:nowrap}
.ax{display:inline-block;font-family:"IBM Plex Mono",monospace;font-size:.63rem;
  padding:.1rem .34rem;border-radius:3px;margin-right:.22rem}
.ax.new{background:var(--pass-bg);color:var(--pass)}
.ax.rep{background:var(--none-bg);color:var(--none)}
.pill{display:inline-block;font-family:"IBM Plex Mono",monospace;font-size:.68rem;font-weight:500;
  padding:.14rem .45rem;border-radius:20px;white-space:nowrap}
.pill.pass{background:var(--pass-bg);color:var(--pass)}
.pill.fail{background:var(--fail-bg);color:var(--fail)}
.pill.warn{background:var(--warn-bg);color:var(--warn)}
.pill.none{background:var(--none-bg);color:var(--none)}
.bar{height:.4rem;border-radius:3px;background:var(--sunk);overflow:hidden;min-width:6rem;margin-top:.35rem}
.bar>i{display:block;height:100%;background:var(--accent)}
.bar.over>i{background:var(--fail)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(20rem,1fr));gap:0 2.2rem}
.grid2 h3:first-child{margin-top:0}
.links{display:grid;gap:.7rem;margin:1.5rem 0}
.link{display:flex;gap:1rem;align-items:baseline;background:var(--raised);border:1px solid var(--rule);
  border-radius:9px;padding:1rem 1.2rem;text-decoration:none;color:inherit;box-shadow:var(--shadow)}
.link:hover{border-color:var(--accent)}
.link .t{font-weight:600;color:var(--accent);text-decoration:underline;text-underline-offset:3px}
.link .d{color:var(--ink-2);font-size:.88rem}
.defect{border:1px solid var(--rule);border-radius:9px;background:var(--raised);margin:.85rem 0;box-shadow:var(--shadow)}
.defect summary{padding:1rem 1.2rem;cursor:pointer;display:flex;gap:.85rem;align-items:baseline;list-style:none}
.defect summary::-webkit-details-marker{display:none}
.defect summary::before{content:"\25B8";color:var(--ink-3);font-size:.8rem;flex:none}
.defect .body{padding:0 1.2rem 1.2rem 2.9rem;color:var(--ink-2);font-size:.93rem;max-width:44rem}
.defect .body p{margin-bottom:.75rem}
.defect .body p:last-child{margin-bottom:0}
ul.plain{margin:0 0 1.05rem;padding-left:1.2rem;max-width:40rem}
ul.plain li{margin-bottom:.5rem}
.foot{margin-top:4.5rem;padding-top:1.5rem;border-top:1px solid var(--rule);
  font-family:"IBM Plex Mono",monospace;font-size:.74rem;color:var(--ink-3);max-width:50rem}
"""

SHORT = {"Language": "Lang", "Shape": "Shape", "Repository": "Repo"}
HORIZON = "https://horizon.bespokelabs.ai/tasks/"


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def load_strategy():
    """The ordering comes from render_report.diverse_order() so this page can
    never disagree with the golden-set page about what the order is."""
    spec = importlib.util.spec_from_file_location(
        "render_report", Path(__file__).resolve().parent / "render_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def chips(task: dict) -> str:
    out = [f'<span class="ax new">{SHORT[a]}</span>' for a in task["fresh"]]
    for a in task["spent"]:
        label, n = a.rsplit(" #", 1)
        out.append(f'<span class="ax rep" title="{esc(a)}">{SHORT[label]}&middot;{n}</span>')
    return "".join(out)


def bar_rows(counter: dict, total: int) -> str:
    rows = []
    for name, count in sorted(counter.items(), key=lambda kv: -kv[1]):
        pct = count / total if total else 0
        rows.append(
            f'<tr><td>{esc(name)}</td><td class="n">{count}</td>'
            f'<td class="n">{pct:.1%}</td>'
            f'<td><div class="bar"><i style="width:{pct*100:.0f}%"></i></div></td></tr>')
    return "".join(rows)


def build(data: dict, gated: bool) -> str:
    rr = load_strategy()
    rows = data["tasks"]
    ordered, diag = rr.diverse_order(rows)
    n = len(ordered)
    axis = rr.axis_value

    tasks = [{
        "n": i, "name": r.get("name"), "task_id": r.get("task_id"),
        "owner": r.get("responsible"), "lang": axis(r, "lang_key"),
        "shape": r.get("shape"), "repo": axis(r, "repo_key").replace("github.com/", ""),
        "rollouts": r.get("rollouts_n"), "turns": r.get("turns_median"),
        "rp": r.get("leading_rp"),
        "pass6": (f'{r.get("pass6")}/{r.get("pass6_denominator")}'
                  if r.get("pass6_denominator") else "\u2014"),
        "fresh": r.get("_fresh_axes") or [], "spent": r.get("_spent_axes") or [],
    } for i, r in enumerate(ordered, 1)]

    trs = "".join(
        f'<tr><td class="n">{t["n"]}</td>'
        f'<td><a href="{HORIZON}{esc(t["task_id"])}" target="_blank" '
        f'rel="noopener noreferrer">{esc(t["name"])}</a>'
        f'<div class="sub">{esc(t["owner"])}</div></td>'
        f'<td>{esc(t["lang"])}</td><td>{esc(t["shape"])}</td>'
        f'<td class="repo">{esc(t["repo"])}</td>'
        f'<td class="axes">{chips(t)}</td>'
        f'<td class="n">{esc(t["rollouts"] if t["rollouts"] is not None else "\u2014")}</td>'
        f'<td class="n">{esc(t["turns"] if t["turns"] is not None else "\u2014")}</td>'
        f'<td class="n">{esc(t["rp"])}</td><td class="n">{esc(t["pass6"])}</td></tr>'
        for t in tasks)

    langs = collections.Counter(t["lang"] for t in tasks)
    shapes = collections.Counter(t["shape"] for t in tasks)
    owners = collections.Counter(t["owner"] for t in tasks)
    repos = collections.Counter(t["repo"] for t in tasks)

    axes = "".join(
        f'<tr><td>{esc(a["label"])}</td><td class="n">{a["distinct"]}</td>'
        f'<td class="n">{a["floor"]}</td>'
        f'<td class="n">{a["first_repeat"] or "\u2014"}</td>'
        f'<td><span class="pill {"pass" if a["optimal"] else "warn"}">'
        f'{"optimal" if a["optimal"] else "early repeat"}</span></td></tr>'
        for a in diag["axes"])
    all_optimal = all(a["optimal"] for a in diag["axes"])

    basis = ("Tasks are filtered by the fit gates (AI rubrics, Argus Main, pass6 and "
             "research-and-planning) and then ordered by diversity."
             if gated else
             "The sheet was vetted by its owners, so <strong>every task in it is "
             "included</strong> and only the ORDER is computed. Quality signals were still "
             "measured; they simply do not exclude anything, so no pass/fail verdict is "
             "shown against a task that is on the list either way.")

    optimal_note = (
        f'<div class="callout ok"><h3>The ordering is optimal on every axis</h3>'
        f'<p>The floor is the earliest a repeat could possibly occur once every value has been '
        f'used once. Each axis first repeats exactly at its floor, so every repeat in this list '
        f'was forced by the pool rather than chosen, and nothing was traded away.</p></div>'
        if all_optimal else
        '<div class="callout"><h3>At least one axis repeated earlier than it had to</h3>'
        '<p>See the floor column below: a first repeat before the floor means the ordering '
        'spent diversity it did not have to spend. Worth investigating.</p></div>')

    return f"""<title>Pilot Task Selection</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style>

<div class="wrap">
<header>
  <p class="eyebrow">Voyager pilot</p>
  <h1>The pilot task list</h1>
  <p class="standfirst">All {n} tasks, ordered so each one differs from everything before it on as
  many of language, shape and repository as the pool still allows. Where nothing fresh remains,
  the next best task is taken rather than stopping.</p>
  <div class="meta"><span>{time.strftime("%d %B %Y")}</span>
    <span>{n} tasks</span><span>{len(repos)} repositories</span>
    <span>{"gated" if gated else "selection: diversity only"}</span></div>
</header>

<div class="tiles">
  <a class="tile" href="#tasks" style="text-decoration:none;color:inherit">
    <span class="v num">{n}</span><span class="k">Tasks</span><span class="s">in pick order &darr;</span></a>
  <div class="tile"><span class="v num">{len(repos)}</span><span class="k">Repositories</span>
    <span class="s">most-used appears {max(repos.values()) if repos else 0}&times;</span></div>
  <div class="tile"><span class="v num">{len(shapes)}</span><span class="k">Shapes</span>
    <span class="s">{esc(", ".join(sorted(shapes)))}</span></div>
  <div class="tile"><span class="v num">{sum(1 for a in diag["axes"] if a["optimal"])}/{len(diag["axes"])}</span>
    <span class="k">Axes optimal</span><span class="s">no avoidable repeat</span></div>
</div>

<section>
  <h2>What this list is</h2>
  <div class="prose">
    <p>{basis}</p>
    {optimal_note}
  </div>
</section>

<section id="tasks">
  <h2>All {n} tasks, in pick order</h2>
  <div class="prose"><p>The <span class="ax new">Lang</span> chips mark an axis this task opened;
  <span class="ax rep">Repo&middot;2</span> means it was the second task on that value. Rollouts,
  turns and research-and-planning are measurements, shown for context.</p></div>
  <div class="scroll tall"><table>
    <thead><tr><th class="n">#</th><th>Task</th><th>Lang</th><th>Shape</th><th>Repository</th>
      <th>Opened</th><th class="n">Rollouts</th><th class="n">Turns</th>
      <th class="n">R/P</th><th class="n">pass6</th></tr></thead>
    <tbody>{trs}</tbody>
  </table></div>
</section>

<section>
  <h2>The mix</h2>
  <div class="grid2">
    <div>
      <h3>Language</h3>
      <div class="scroll"><table><thead><tr><th>Language</th><th class="n">Tasks</th>
        <th class="n">Share</th><th></th></tr></thead>
        <tbody>{bar_rows(langs, n)}</tbody></table></div>
      <h3>Shape</h3>
      <div class="scroll"><table><thead><tr><th>Shape</th><th class="n">Tasks</th>
        <th class="n">Share</th><th></th></tr></thead>
        <tbody>{bar_rows(shapes, n)}</tbody></table></div>
    </div>
    <div>
      <h3>Author</h3>
      <div class="scroll"><table><thead><tr><th>Author</th><th class="n">Tasks</th>
        <th class="n">Share</th><th></th></tr></thead>
        <tbody>{bar_rows(owners, n)}</tbody></table></div>
      <h3>How the ordering did</h3>
      <div class="scroll"><table><thead><tr><th>Axis</th><th class="n">Distinct</th>
        <th class="n">Floor</th><th class="n">First repeat</th><th></th></tr></thead>
        <tbody>{axes}</tbody></table></div>
      <p class="sub" style="margin-top:.6rem">Floor = the earliest a repeat could happen
      once every value on that axis has been used once.</p>
    </div>
  </div>
</section>

<p class="foot">Generated by make_report.py from the measurement sidecar. Every figure is computed
from the live Horizon database or the task files themselves, read-only.</p>
</div>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("json", help="the sidecar written by generate_pilot_analysis.py")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()
    data = json.loads(Path(args.json).read_text())
    gated = any(r.get("fit") != "YES" for r in data["tasks"])
    page = build(data, gated)
    Path(args.out).write_text(page)
    print(f"wrote {args.out}  ({len(data['tasks'])} tasks, "
          f"{'gated' if gated else 'all included'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
