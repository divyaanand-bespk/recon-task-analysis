---
name: pilot-analysis
description: Run the Voyager pilot readiness and golden-set analysis - measure which Horizon tasks are fit for the pilot, pick a maximally diverse subset, and render the two HTML reports. Use when asked to refresh the pilot report, analyse a new task sheet, check task diversity, or pick the golden N tasks.
---

# Pilot analysis

Measures Horizon tasks against the pilot bar, then orders the ones that pass
so the selection is as diverse as the pool allows. Produces two HTML reports.

**This pipeline never writes to Horizon.** Read-only throughout: no pushes, no
rubric fires, no evaluations. If a task looks wrong, report it; do not fix it here.

## Before anything: three things must be true

Check all three FIRST. Each has failed in a way that looks like something else.

**1. The interpreter needs Python 3.11+, the `horizon` package, and `psql`.**
macOS system `python3` is 3.9 and has no `tomllib`, so `diversity_enrich.py`
dies on its import line. Find an interpreter that satisfies both rather than
assuming one — it is usually a venv belonging to whichever checkout of
`horizon` you have:

```bash
for c in $(command -v python3.13 python3.12 python3.11) \
         "$(git rev-parse --show-toplevel 2>/dev/null)/.venv/bin/python" \
         ~/*/horizon/.venv/bin/python ~/*/*/horizon/.venv/bin/python; do
  [ -x "$c" ] && "$c" -c "import horizon, tomllib" 2>/dev/null && { PY="$c"; break; }
done
echo "${PY:?no interpreter has both python>=3.11 and the horizon package}"
```

`psql` must also be on PATH. `psycopg` is optional and, when absent, EVERY
query falls back to `psql` — so a missing `psql` fails the whole run with
`Required command not found: psql` even though the database is reachable. On
macOS the libpq formula is keg-only and off PATH by default:

```bash
command -v psql || export PATH="$(brew --prefix libpq)/bin:$PATH"
```

**2. The proxy must be listening, and the port is 15433 — not 5432.**
Checking the wrong port reads as "proxy is down" when it is up:

```bash
pgrep -fl cloud-sql-proxy
nc -z 127.0.0.1 15433 && echo open
```

If it is not running, ASK THE USER to start it in their own terminal. Do not
start it yourself — it is long-lived and belongs to their session:

```bash
cloud-sql-proxy --private-ip --port 15433 apex-485220:us-central1:horizon-db
```

Use that ADC form. The `--token "$(gcloud auth print-access-token)"` variant
expires after about an hour and **the proxy does not exit when it does** — every
later connection fails with `server closed the connection unexpectedly` while
the process is still running and still listening. That reads exactly like a
database outage and has cost two runs.

**3. `HORIZON_API_KEY` must be set.** `diversity_enrich.py` needs it (API only,
no database), and the measurement pass needs it AGAIN as the R/P worker key —
the script does not mint one, it exits with `Set HORIZON_WORKER_KEY, set
HORIZON_WORKER_KEY_FILE, or create /tmp/hzkey`. Export it under BOTH names.

Check for an existing `.env` in the pipeline checkout before asking anyone for
a key; if there is none, ask the user rather than hunting, and suggest they put
it in a file instead of pasting it into the conversation.

## The run

Enrichment first — the analysis reads its output. Rendering last, and repeatable
on its own with no Horizon access at all.

```bash
# PY from step 1; CSV is the sheet you were given; ids extracted from it
export HORIZON_API_KEY=...            # or: set -a; . path/to/.env; set +a
export HORIZON_WORKER_KEY="$HORIZON_API_KEY"   # same key, second name
export HORIZON_DB_ROLE=horizon_claude_ro
export HORIZON_DB_SECRET=horizon-claude-ro-password

# 1. repo + language + content fingerprint, from the API (the DB holds NEITHER)
$PY diversity_enrich.py --task-file /tmp/task-ids.txt --out /tmp/enrich.json

# 2. the measurement pass: rubrics, Argus, rollouts, turns, R/P labels
$PY generate_pilot_analysis.py \
  --task-csv "$CSV" \
  --enrich /tmp/enrich.json \
  --port 15433 --jobs 12 --label-concurrency 16 \
  --output /tmp/pilot.html

# 3. the two reports, from the JSON sidecar
$PY render_report.py /tmp/pilot.json -o /tmp/pilot-readiness.html --target 50
$PY golden_app.py    /tmp/pilot.json -o /tmp/golden-set.html
```

## The caches are local, and that matters for handover

`~/.cache/pilot-analysis/` holds two caches: `enrich/` (per task+version) and
`rp/` (research-and-planning labels, ~29MB). **Neither travels with this repo**,
so a fresh clone pays a cold start. Neither is required for correctness — they
only avoid re-paying. `/tmp/pilot_enrich.json` does not travel either; it is
easy to assume that sidecar counts as a cache, and it does not.

Measured on the 53-group / 99-task-id sheet:

| step | cold | warm | bills you |
|---|---|---|---|
| enrichment | 99 API fetches, ~50s at `--workers 16` | cached | no |
| database pass | 13s | 3s | no |
| R/P labelling | ~250 chunks over 59 rollouts, **~$11** | cached | **yes** |

The whole 13s-vs-3s database difference is exporting 59 trajectories (36MB);
`load_task_metadata` and `load_rollouts` cost the same either way because they
touch neither cache. Only 59 of the 99 tasks have a representative rollout, so
only those are ever exported or labelled.

**R/P labelling is the only step that costs money.** Re-runs are near-free —
labels are cached per rollout id, so adding five tasks to the sheet costs five
tasks, not 99.

## Reading the sheet

The CSV groups VARIANTS of one task across columns: a binary version and a
partial version, each with its own Horizon task id. `read_variant_csv()` parses
by HEADER TEXT, never by position — the columns have been reordered before, and
positional parsing silently produced owners named `3`, `4`, `5` and empty ids.

Signals resolve per-signal across variants: within a variant take the LATEST
Horizon run; across variants in CSV order, the first PASS settles it. The
`[Blocking] Grader coverage` rubric is WAIVED for the binary variant and
ENFORCED for the partial one.

## Fit for pilot

`fit_for_pilot()` is the original author's and must not be changed without asking:
`ai_rubrics == "Pass"` AND `argus_main == "Pass"` AND `pass6 < 2` AND
`pass6_denominator > 0` AND `rp_gate > 20`.

## The selection strategy

`diverse_order()` in `render_report.py` orders the fit pool. Five terms:

1. **Breadth** — how many of language / shape / repository the pick leaves
   unused so far. Maximised. **The three axes are NOT ranked against each other.**
2. **Pressure** — summed relative over-use, each axis against its own fair share.
3. Rollouts, 4. median turns, 5. original rank.

An earlier version ranked the axes (language, then shape, then repository) and
that was wrong: language became absolutely dominant, so a task opening a new
language won even when it repeated both the shape and the repository. With many
tasks in ONE repository across many languages — and repositories are
multi-language — it would cycle languages forever and never leave that repo.
On a stress pool it returned to the same repo at pick 8 with fresh repos still
available; the current key cannot repeat before pick 22, the floor.

`golden_app.py` keeps an INDEPENDENT copy of this key to explain each pick, and
self-checks that replaying it reproduces the order. **If you change the key in
one file you must change it in both**, then confirm the page does not say
"local fallback" or "did not reproduce".

## Shape

Owner first, mini-batch name as fallback. Each author publishes exactly one
family: Robert → DeepSWE, Umer/Avi → Optimization, Zac/Kunj/Divya → Diagnosis,
Kartik → Migration. Owner is authoritative because variants sit in PAIRED
batches and one of each pair is a delivery batch (`alpharecon-gemini-binary`)
that names no shape at all.

Two earlier sources were both wrong and are worth not re-inventing:
`shape_from_reviews()` keyed on the NUMBER of applicable rubrics, which broke
when rubrics were merged across variants; `task.toml`'s `category` describes the
change type (bugfix / feature_request), not the family.

## Traps that have actually bitten

- **A broken query returns empty, and empty reads as good news.** Any filter or
  scan that comes back with nothing needs a positive control before you report it.
- **`local_task_id` is not always a UUID** — 122 rows hold junk, one literally
  `"hello-world"`. The regex guard before the `::uuid` cast is load-bearing.
- **The statement timeout is 120s and cannot be raised** — it comes from
  `ALTER ROLE horizon_claude_ro`, and the role is SELECT-only.
- **CSV parsing must preserve line terminators.** `splitlines()` on a `COPY`
  result destroys `\r\n` inside quoted fields; every exported trajectory was
  silently ~1.8% short until this was fixed. Use `io.StringIO(text, newline="")`.
- **Infra zeros read as real 0.0 scores.** A rollout that never ran is not a
  rollout that scored zero.
- **Match task identity on UUID, never name or prefix** — names collide across
  source and delivered copies.

## Verify before reporting

- `uv run --with pytest python -m pytest test_generator.py -q` — 10 tests.
- The golden page must not say "local fallback" or "did not reproduce".
- Sanity-check the axis table: `optimal` should be true on every axis unless the
  pool genuinely forced a repeat.
