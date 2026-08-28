#!/usr/bin/env bash
# One command, whole pipeline. Reproduces the shipped handover exactly.
#
#   ./run_pilot.sh "/path/to/Voyager Status and Milestones - Final List of tasks-N.csv"
#
# Options:
#   --gated     apply the fit gates (rubrics, Argus, pass6, R/P) instead of
#               including every task. The shipped handover did NOT use this:
#               the sheet was already vetted by its owners, so every task is in
#               and only the ORDER is computed.
#   --out DIR   where to write the reports (default ./out)
set -euo pipefail
cd "$(dirname "$0")"

CSV="${1:-}"; shift || true
ASSUME="--assume-fit"; OUT="out"
while [ $# -gt 0 ]; do
  case "$1" in
    --gated) ASSUME=""; shift ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$CSV" ] || [ ! -f "$CSV" ]; then
  echo "usage: $0 <task-sheet.csv> [--gated] [--out DIR]" >&2; exit 2
fi

# shellcheck source=preflight.sh
source ./preflight.sh || exit 1
mkdir -p "$OUT"
W="$OUT/work"; mkdir -p "$W"

echo
echo "1/5  task ids from the sheet"
"$PY" - "$CSV" > "$W/task-ids.txt" <<'PYEOF'
import importlib.util, sys
from pathlib import Path
sp = importlib.util.spec_from_file_location("g", "generate_pilot_analysis.py")
g = importlib.util.module_from_spec(sp); sp.loader.exec_module(g)
ids = set()
for grp in g.read_variant_csv(Path(sys.argv[1])):
    for slot in ("final", "binary", "partial"):
        t = grp.get(slot)
        if t:
            ids.add(t if isinstance(t, str) else t.get("task_id"))
print("\n".join(sorted(i for i in ids if i)))
PYEOF
echo "     $(wc -l < "$W/task-ids.txt" | tr -d ' ') task ids"

echo "2/5  enrichment  (repository + language; cached per task+version)"
"$PY" diversity_enrich.py --task-file "$W/task-ids.txt" --out "$W/enrich.json"

echo "3/5  version materiality  (which superseded versions still count)"
"$PY" - "$W/task-ids.txt" > "$W/pairs.json" <<'PYEOF'
import importlib.util, json, sys
sp = importlib.util.spec_from_file_location("g", "generate_pilot_analysis.py")
g = importlib.util.module_from_spec(sp); sp.loader.exec_module(g)
ids = [l.strip() for l in open(sys.argv[1]) if l.strip()]
lit = ",".join(f"'{i}'" for i in ids)
with g.HorizonDatabase(port=15433) as db:
    rows = db.csv(f"""SELECT task_id::text AS task_id, current_task_version AS cur,
                      max(task_version) AS ev
                      FROM rubric_ai_reviews_with_staleness
                      WHERE task_id::text IN ({lit})
                      GROUP BY 1,2
                      HAVING count(*) FILTER (WHERE task_version=current_task_version)=0""")
print(json.dumps([{"task_id": r["task_id"], "evidence_version": int(r["ev"]),
                   "current_version": int(r["cur"])} for r in rows]))
PYEOF
"$PY" version_materiality.py --pairs "$W/pairs.json" --out "$W/materiality.json"

echo "4/5  measurement pass  (rubrics, Argus, rollouts, turns, R/P)"
"$PY" generate_pilot_analysis.py \
  --task-csv "$CSV" --enrich "$W/enrich.json" --materiality "$W/materiality.json" $ASSUME \
  --port 15433 --jobs 12 --label-concurrency 16 --output "$W/pilot.html"

echo "5/5  reports"
"$PY" render_report.py "$W/pilot.json" -o "$OUT/pilot-readiness.html" --target 50
"$PY" golden_app.py    "$W/pilot.json" -o "$OUT/golden-set.html"
"$PY" make_report.py   "$W/pilot.json" -o "$OUT/handover.html"

echo
echo "done. open:"
for f in handover.html golden-set.html pilot-readiness.html; do echo "  $OUT/$f"; done
