#!/usr/bin/env python3
"""Decide whether a task's version bump could have changed an outcome.

A push does not automatically invalidate the evidence attached to earlier
versions. Adding `tests/rubrics.json` changes nothing an agent sees or a grader
reads, so rollouts and rubric verdicts from the previous version remain true.
Rewriting `grade.py` invalidates every one of them. Treating both the same way
either throws away good evidence or trusts bad evidence, and both are wrong.

So we compare CONTENT, not version numbers. Two versions are EQUIVALENT when
every file that could change an outcome is byte-identical:

  environment/repository.tar.gz   the code the agent works on
  environment/Dockerfile          how that code is built
  tests/grade.py                  the scoring
  tests/test.sh, tests/Dockerfile the harness that runs it
  tests/private_manifest.json     the hidden surface
  instruction.md                  what the agent is told to do
  task.toml                       config that reaches grading

Everything else is presentation or bookkeeping. `tests/rubrics.json` is read by
no grader. `tests/sealed.json` is a CONSEQUENCE of a change, never a cause --
including it would make every cosmetic edit look material.

Measured on the 99-id sheet: of 11 tasks whose evidence sat on a superseded
version, 3 were cosmetic (a rubrics.json addition) and 8 had a changed grade.py.
"""
from __future__ import annotations
import argparse, concurrent.futures as cf, hashlib, json, os, sys, urllib.request
from pathlib import Path

MATERIAL = (
    "environment/repository.tar.gz", "environment/Dockerfile",
    "tests/grade.py", "tests/test.sh", "tests/Dockerfile",
    "tests/private_manifest.json", "instruction.md", "task.toml",
)
CACHE = Path(os.path.expanduser("~/.cache/pilot-analysis/materiality"))


def version_digests(client, task_id: str, version: int, workers: int = 8) -> dict:
    """sha256 of each material file at one version. Cached: content at a
    version is immutable, so a hit is always valid and never needs revalidating."""
    CACHE.mkdir(parents=True, exist_ok=True)
    hit = CACHE / f"{task_id}-v{version}.json"
    if hit.exists():
        try:
            return json.loads(hit.read_text())
        except json.JSONDecodeError:
            pass
    files = {f.path: f.url for f in client.tasks.download_urls(task_id, version=version).files}
    want = {p: u for p, u in files.items() if p in MATERIAL}

    def one(path: str) -> tuple[str, str]:
        data = urllib.request.urlopen(want[path], timeout=180).read()
        return path, hashlib.sha256(data).hexdigest()

    with cf.ThreadPoolExecutor(workers) as pool:
        out = dict(pool.map(one, sorted(want)))
    hit.write_text(json.dumps(out, sort_keys=True))
    return out


def compare(client, task_id: str, old: int, new: int) -> dict:
    """Classify one version bump. `changed` names the material files that moved."""
    if old == new:
        return {"equivalent": True, "changed": [], "reason": "same version"}
    a = version_digests(client, task_id, old)
    b = version_digests(client, task_id, new)
    changed = sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))
    return {"equivalent": not changed, "changed": changed,
            "reason": "no material file differs" if not changed
                      else f"{len(changed)} material file(s) differ"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pairs", required=True,
                    help='JSON: [{"task_id":..., "evidence_version":N, "current_version":M}, ...]')
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from horizon.client import HorizonClient
    client = HorizonClient(api_key=os.environ["HORIZON_API_KEY"])
    pairs = json.loads(Path(args.pairs).read_text())
    out = {}
    for p in pairs:
        tid = p["task_id"]
        try:
            verdict = compare(client, tid, int(p["evidence_version"]), int(p["current_version"]))
        except Exception as exc:                      # a fetch failure is NOT equivalence
            verdict = {"equivalent": False, "changed": [],
                       "reason": f"could not compare ({type(exc).__name__}); treated as material"}
        verdict.update(evidence_version=p["evidence_version"], current_version=p["current_version"])
        out[tid] = verdict
        print(f"{tid[:8]}  v{p['evidence_version']} -> v{p['current_version']}  "
              f"{'EQUIVALENT' if verdict['equivalent'] else 'MATERIAL'}  {verdict['reason']}",
              file=sys.stderr)
    Path(args.out).write_text(json.dumps(out, indent=2, sort_keys=True))
    n = sum(1 for v in out.values() if v["equivalent"])
    print(f"wrote {args.out}: {len(out)} pairs, {n} equivalent, {len(out)-n} material", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
