#!/usr/bin/env python3
"""Pick the best N pilot tasks under diversity constraints, and report why.

Consumes the two upstream artefacts rather than forking either one:
  * the JSON sidecar written by generate_pilot_analysis.py  (fit / pass6 /
    ai_rubrics / argus_main / leading_rp / rp_complete_step)
  * the JSON written by diversity_enrich.py                 (repo / language /
    fingerprint)

DIVERSITY IS A POOL PROPERTY, NOT A TASK PROPERTY. That is why it cannot live
inside fit_for_pilot(): two individually perfect tasks can still be a bad pair.
So eligibility is decided per task upstream, and the constraints below are
applied while walking the eligible list.

GATES
  repo cap      no repository may contribute more than --max-per-repo tasks
  language cap  no single language may exceed --max-lang-share of the selection
  duplicates    identical content fingerprints are collapsed, extras reported

A shortfall is REPORTED, never silently traded away: if N cannot be reached
without breaching a cap, the selection stops short and says so. Quietly
breaching a cap to hit a round number is the failure this file exists to avoid.

    python3 select_pilot.py --analysis pilot.json --enrich enrich.json \
                            --target 50 --out pilot-50.html
"""
from __future__ import annotations
import argparse, collections, html, json, math, sys, time


def load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def order_key(row: dict):
    # Harsh's HTML orders fit=YES first, then leading_rp desc, then name.
    # Reused verbatim so this tool cannot disagree with the report it extends.
    return (0 if row.get("fit") == "YES" else 1,
            -int(row.get("leading_rp") or 0),
            str(row.get("name") or "").lower())


def select(rows: list[dict], target: int, max_per_repo: int, max_lang_share: float):
    chosen: list[dict] = []
    repo_count: collections.Counter = collections.Counter()
    lang_count: collections.Counter = collections.Counter()
    seen_fp: dict[str, str] = {}
    rejected: list[dict] = []
    lang_cap = math.floor(max_lang_share * target)

    for row in rows:
        if len(chosen) >= target:
            rejected.append({**row, "reject_reason": "target already met"})
            continue
        fp = row.get("fingerprint") or ""
        if fp and fp in seen_fp:
            rejected.append({**row, "reject_reason": f"exact duplicate of {seen_fp[fp]}"})
            continue
        # An UNKNOWN repo is not a repo. Bucketing every unreadable task under
        # one "(unknown)" key makes them collide with each other and rejects the
        # third onward for a constraint that was never evaluated. Unknowns skip
        # the cap and are counted separately so the report can say how much of
        # the pool went unchecked.
        repo = row.get("repo_key") or ""
        if repo and repo_count[repo] >= max_per_repo:
            rejected.append({**row, "reject_reason":
                             f"repo cap: {repo} already has {max_per_repo}"})
            continue
        lang = row.get("lang_key") or ""
        if lang and lang_count[lang] >= lang_cap:
            rejected.append({**row, "reject_reason":
                             f"language cap: {lang} already at {lang_cap} of {target}"})
            continue
        chosen.append(row)
        repo_count[repo or "(unknown)"] += 1
        lang_count[lang or "(unknown)"] += 1
        if fp:
            seen_fp[fp] = row.get("name") or row.get("task_id", "")
    return chosen, rejected, repo_count, lang_count, lang_cap


CSS = """
:root{color-scheme:light dark;--bg:light-dark(#fff,#181818);--fg:light-dark(#171717,#f4f4f5);
--muted:light-dark(#666,#a1a1aa);--border:light-dark(#dedede,#3f3f46);--card:light-dark(#fafafa,#222);
--green:#16803c;--red:#c93838;--orange:#b66a00;}
*{box-sizing:border-box}body{margin:0;padding:26px;background:var(--bg);color:var(--fg);
font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;}
h1{font-size:1.5rem;margin:0 0 2px}h2{font-size:1.05rem;margin:26px 0 8px}
.sub{color:var(--muted);font-size:.9em;margin-bottom:18px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:8px}
.card{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:12px 14px;display:grid;gap:3px}
.card .n{font-size:1.5rem;font-weight:600}.card .l{color:var(--muted);font-size:.85em}
table{width:100%;border-collapse:collapse;font-size:.92em;margin-top:6px}
th,td{padding:6px 8px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{color:var(--muted);font-weight:600;font-size:.85em}
.ok{color:var(--green);font-weight:600}.bad{color:var(--red);font-weight:600}.warn{color:var(--orange);font-weight:600}
.bar{display:flex;height:9px;border-radius:5px;overflow:hidden;border:1px solid var(--border);margin:6px 0 3px}
.bar span{display:block}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}
footer{margin-top:28px;border-top:1px solid var(--border);padding-top:10px;color:var(--muted);font-size:.86em}
"""
PALETTE = ["#4f6bed", "#2aa39a", "#b66a00", "#8d5fd3", "#c93838", "#16803c", "#777"]


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def render(chosen, rejected, repo_count, lang_count, lang_cap, args, pool_n, eligible_n):
    total = len(chosen)
    shortfall = args.target - total
    langs = lang_count.most_common()
    bar = "".join(
        f'<span style="width:{(n/total*100 if total else 0):.4f}%;background:{PALETTE[i%len(PALETTE)]}"'
        f' title="{esc(l)} {n}"></span>' for i, (l, n) in enumerate(langs))
    over_repo = [(r, n) for r, n in repo_count.most_common() if n > args.max_per_repo]
    over_lang = [(l, n) for l, n in langs if total and n / total > args.max_lang_share]

    rows_html = "\n".join(
        f"<tr><td>{i+1}</td><td>{esc(r.get('name'))}<br><code>{esc(r.get('task_id'))}</code></td>"
        f"<td>{esc(r.get('lang_key') or '?')}</td>"
        f"<td>{esc(r.get('repo_key') or '?')}{' <span class=warn>(derived)</span>' if r.get('repo_derived') else ''}</td>"
        f"<td>{esc(r.get('pass6'))}/{esc(r.get('pass6_denominator'))}</td>"
        f"<td>{esc(r.get('leading_rp'))}</td>"
        f"<td class='{'ok' if r.get('argus_main')=='Pass' else 'bad'}'>{esc(r.get('argus_main'))}</td></tr>"
        for i, r in enumerate(chosen))

    rej_html = "\n".join(
        f"<tr><td>{esc(r.get('name'))}<br><code>{esc(r.get('task_id'))}</code></td>"
        f"<td>{esc(r.get('lang_key') or '?')}</td><td>{esc(r.get('repo_key') or '?')}</td>"
        f"<td>{esc(r.get('reject_reason'))}</td></tr>"
        for r in rejected if r.get("reject_reason") != "target already met")

    problems = [r for r in chosen if r.get("problems")]
    prob_html = "\n".join(
        f"<tr><td>{esc(r.get('name'))}</td><td>{esc('; '.join(r.get('problems') or []))}</td></tr>"
        for r in problems) or "<tr><td colspan=2>None — every selected task's repo and language were read from its own task.toml.</td></tr>"

    verdict = ("<span class=ok>PASS</span>" if not over_repo and not over_lang and shortfall <= 0
               else "<span class=bad>CONSTRAINED</span>")
    return f"""<!doctype html><meta charset=utf-8><title>Pilot selection — top {args.target}</title>
<style>{CSS}</style>
<h1>Pilot selection — top {args.target}</h1>
<div class=sub>Generated {esc(time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime()))} ·
pool {pool_n} tasks · {eligible_n} eligible (fit = YES) · selected {total} · verdict {verdict}</div>

<div class=cards>
<div class=card><div class=n>{total}</div><div class=l>selected of {args.target}</div></div>
<div class=card><div class=n>{eligible_n}</div><div class=l>eligible (fit = YES)</div></div>
<div class=card><div class=n>{len([k for k in repo_count if k!="(unknown)"])}</div><div class=l>distinct repositories</div></div>
<div class=card><div class=n>{repo_count.get("(unknown)",0)}</div><div class=l class="{'bad' if repo_count.get("(unknown)") else ''}">repo UNCHECKED</div></div>
<div class=card><div class=n>{len(lang_count)}</div><div class=l>distinct languages</div></div>
<div class=card><div class=n>{max((n for _,n in repo_count.most_common()), default=0)}</div>
<div class=l>max tasks per repo (cap {args.max_per_repo})</div></div>
</div>

<h2>Language mix</h2>
<div class=bar>{bar}</div>
<table><tr><th>language</th><th>tasks</th><th>share</th><th>cap {int(args.max_lang_share*100)}%</th></tr>
{''.join(f"<tr><td>{esc(l)}</td><td>{n}</td><td>{(n/total*100 if total else 0):.1f}%</td>"
         f"<td class='{'bad' if total and n/total>args.max_lang_share else 'ok'}'>"
         f"{'OVER' if total and n/total>args.max_lang_share else 'ok'}</td></tr>" for l,n in langs)}
</table>
<div class=sub>Cap applied during selection as {lang_cap} of {args.target} tasks.</div>

<h2>Selected tasks</h2>
<table><tr><th>#</th><th>task</th><th>language</th><th>repository</th><th>pass@6</th>
<th>leading R/P</th><th>Argus Main</th></tr>{rows_html}</table>

<h2>Not selected, and why</h2>
<table><tr><th>task</th><th>language</th><th>repository</th><th>reason</th></tr>
{rej_html or "<tr><td colspan=4>Nothing was excluded by a diversity gate.</td></tr>"}</table>

<h2>Metadata gaps</h2>
<table><tr><th>task</th><th>problem</th></tr>{prob_html}</table>

<footer>
<div>Eligibility (fit = YES) is decided upstream by <code>generate_pilot_analysis.py</code>:
AI rubrics pass, pass@6 &lt; 2, at least one eligible rollout, Argus Main pass, R/P gate &gt; 20.</div>
<div>This stage adds only the pool-level gates: max {args.max_per_repo} tasks per repository,
no language above {int(args.max_lang_share*100)}%, and exact-duplicate collapse by content fingerprint.</div>
{"<div class=bad>SHORTFALL: selection stopped "+str(shortfall)+" short of "+str(args.target)+
 " because the remaining eligible tasks would have breached a cap. Caps were not traded away to hit the number.</div>" if shortfall>0 else ""}
</footer>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis", required=True, help="sidecar JSON from generate_pilot_analysis.py")
    ap.add_argument("--enrich", required=True, help="JSON from diversity_enrich.py")
    ap.add_argument("--out", required=True)
    ap.add_argument("--target", type=int, default=50)
    ap.add_argument("--max-per-repo", type=int, default=3)
    ap.add_argument("--max-lang-share", type=float, default=0.5)
    ap.add_argument("--include-all", action="store_true",
                    help="consider every task, not only fit == YES")
    args = ap.parse_args()

    analysis = load(args.analysis).get("tasks", [])
    enrich = {r["task_id"]: r for r in load(args.enrich).get("tasks", [])}
    merged = []
    for row in analysis:
        merged.append({**row, **{k: v for k, v in enrich.get(row.get("task_id"), {}).items()
                                 if k not in ("name",)}})
    missing = [r for r in merged if r.get("task_id") not in enrich]
    if missing:
        print(f"WARNING: {len(missing)} task(s) had no enrichment row; "
              f"they cannot be diversity-checked", file=sys.stderr)

    pool = merged if args.include_all else [r for r in merged if r.get("fit") == "YES"]
    pool.sort(key=order_key)
    chosen, rejected, repo_count, lang_count, lang_cap = select(
        pool, args.target, args.max_per_repo, args.max_lang_share)

    with open(args.out, "w") as fh:
        fh.write(render(chosen, rejected, repo_count, lang_count, lang_cap,
                        args, len(merged), len(pool)))
    side = args.out.rsplit(".", 1)[0] + ".json"
    json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "gates": {"max_per_repo": args.max_per_repo,
                         "max_lang_share": args.max_lang_share,
                         "target": args.target, "language_cap_count": lang_cap},
               "selected": chosen, "rejected": rejected,
               "repo_counts": dict(repo_count), "language_counts": dict(lang_count)},
              open(side, "w"), indent=2)
    print(f"selected {len(chosen)}/{args.target}   HTML: {args.out}   Data: {side}")
    if len(chosen) < args.target:
        print(f"SHORTFALL {args.target-len(chosen)}: caps were not breached to reach the target")
    return 0


if __name__ == "__main__":
    sys.exit(main())
