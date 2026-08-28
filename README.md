# Pilot analysis generator

## Quick start

```bash
./run_pilot.sh "/path/to/Voyager Status and Milestones - Final List of tasks-N.csv"
```

That is the whole pipeline: preconditions, task ids, enrichment, version
materiality, the measurement pass, and all three reports into `out/`.
`./preflight.sh` on its own checks the preconditions and prints the exact fix
for anything missing.

The run needs two things you must provide:

- **the proxy**, in its own terminal, because it is long-lived:
  `cloud-sql-proxy --private-ip --port 15433 apex-485220:us-central1:horizon-db`
- **`HORIZON_API_KEY`**, exported or in a `.env` beside the repo. Preflight finds
  either. The same key is reused as the R/P worker key automatically.

Everything else is discovered: the interpreter, `psql`, the database password.

`run_pilot.sh` includes every task in the sheet and computes only the ORDER,
which is what the handover shipped. Pass `--gated` to apply the fit gates
(rubrics, Argus Main, pass6, research-and-planning) instead.

Outputs, in `out/`:

| file | what it is |
|---|---|
| `handover.html` | the shareable page: every task in pick order, plus the mix |
| `golden-set.html` | the same order with the reasoning behind each pick |
| `pilot-readiness.html` | every measured signal per task, filterable |

Re-runs are cheap: enrichment, R/P labels and version digests are all cached, so
adding five tasks to the sheet costs five tasks. Only R/P labelling ever costs
money (about $11 from a completely cold start, near zero warm).

The script accepts Horizon task IDs or task links and creates a self-contained HTML report. It keeps the tasks already present in the output file and adds or refreshes the supplied tasks. The first run starts with only the supplied tasks unless you pass an existing report with `--base-html`.

Three further scripts sit around it. `diversity_enrich.py` attaches the repository and language of each task, which the database does not hold. `render_report.py` turns the JSON sidecar into a report without querying Horizon again. `select_pilot.py` picks the best N tasks under diversity caps. All four read every credential from the environment and none of them write to Horizon.

## Requirements

- Run `gcloud auth login` before the first run. Your Google account needs access to the read-only Horizon database secret, and to the IAP tunnel if you take that route.
- Connect the WARP VPN. The `cloud-sql-proxy` route reaches the instance over WARP and fails without it.
- Install `cloud-sql-proxy` if you take that route.
- Install libpq for `psql`. On macOS the formula is keg-only, so put it on the path yourself:

  ```bash
  brew install libpq
  export PATH="/opt/homebrew/opt/libpq/bin:$PATH"
  ```

  The scripts run `COPY ... TO STDOUT` and parse the CSV. `psycopg` is used when it is
  importable, because a persistent connection removes a process spawn and a TLS handshake
  per query, but it is optional: without it every query falls back to `psql`, which is why
  `psql` is still required. Both routes send the identical statement and are verified to
  return identical rows. Set `PILOT_NO_PSYCOPG=1` to force the `psql` path.
- Keep the transcript analysis pipeline at `~/voyager-alpharecon-rp` and the annotation tool at `~/tool-call-clustering`. A symlink is enough:

  ```bash
  ln -s /path/to/voyager-alpharecon-rp ~/voyager-alpharecon-rp
  ln -s /path/to/tool-call-clustering  ~/tool-call-clustering
  ```

  Both paths are required even when you pass `--pipeline-root` and `--tool-root`. `label_rp.py` resolves `~/tool-call-clustering` itself, so the flags alone do not move it.
- Put the Horizon worker key in `/tmp/hzkey`, or set `HORIZON_WORKER_KEY`, or set `HORIZON_WORKER_KEY_FILE`. A key the script creates itself is deleted when the run ends.
- Set `HORIZON_API_KEY` for `diversity_enrich.py`. That script talks only to the API and needs no database access at all.

The worker key is used only for research and planning labels. The script does not save the key in the HTML or JSON output.

## Database access

Two read-only routes reach the same instance. The script picks a route by checking whether a proxy is already listening on `--port`; if one is, it uses it and opens no tunnel.

### The IAP tunnel

The original route. It needs the `iap.tunnelResourceAccessor` and `compute.viewer` roles, and it is what runs when nothing is listening on the port.

```bash
python3 generate_pilot_analysis.py --task-file new-task-ids.txt --output ~/Downloads/pilot.html
```

It connects as `grafana_ro` and reads the password from the `grafana-postgres-ro-password` secret. Those are the defaults and need no environment variables.

### The cloud-sql-proxy route

This route needs no IAP roles, only permission to read the role's password from Secret Manager, and WARP for the private IP. Start the proxy in its own shell:

Authenticate once with application default credentials, then start the proxy with no token at all:

```bash
gcloud auth application-default login
cloud-sql-proxy --private-ip --port 15433 apex-485220:us-central1:horizon-db
```

**Prefer this form.** The `--token "$(gcloud auth print-access-token)"` variant works, but the token expires after about an hour and the proxy does not exit when it does. Every subsequent connection fails with `server closed the connection unexpectedly` while the process is still running and still listening, which reads exactly like a database outage. It cost two runs before it was recognised. ADC refreshes itself and does not have this failure mode.

If you do use `--token`, never paste a literal token into a script or a shell history: pass it as the command substitution above, because a token on the command line is readable by every process on the machine through `ps`.

Then point the script at that proxy and at the role it serves:

```bash
export HORIZON_DB_ROLE=horizon_claude_ro
export HORIZON_DB_SECRET=horizon-claude-ro-password
python3 generate_pilot_analysis.py --task-file new-task-ids.txt --port 15433 --output ~/Downloads/pilot.html
```

`HORIZON_DB_PASSWORD` overrides both and skips Secret Manager. Use it only for a one-off; prefer the secret.

## The run sequence

Enrichment first, because the analysis reads its output. Rendering last, and repeatable on its own.

```bash
# 1. Repository, language and content fingerprint per task, from the API.
export HORIZON_API_KEY=...
# Cached per (task, version) under ~/.cache/pilot-analysis/enrich, so re-runs are near-free.
python3 diversity_enrich.py --task-file new-task-ids.txt --out ~/Downloads/enrich.json

# 2. The measurement pass. Talks to the database, exports trajectories, labels R/P.
export HORIZON_DB_ROLE=horizon_claude_ro
export HORIZON_DB_SECRET=horizon-claude-ro-password
python3 generate_pilot_analysis.py \
  --task-csv ~/Downloads/pilot-sheet.csv \
  --enrich ~/Downloads/enrich.json \
  --port 15433 --jobs 12 --label-concurrency 16 \
  --output ~/Downloads/pilot-report.html

# 3. Re-render the report from the sidecar. No Horizon access.
python3 render_report.py ~/Downloads/pilot-report.json -o ~/Downloads/pilot-report.html

# 4. Optional: pick the pilot set under the diversity caps.
python3 select_pilot.py \
  --analysis ~/Downloads/pilot-report.json \
  --enrich ~/Downloads/enrich.json \
  --target 50 --max-per-repo 3 --max-lang-share 0.5 \
  --out ~/Downloads/pilot-50.html

# 5. Optional: render the golden set and why each task was picked at its position.
python3 golden_app.py ~/Downloads/pilot-report.json \
  --enrich ~/Downloads/enrich.json \
  -o ~/Downloads/golden-set.html
```

Step 2 writes a JSON sidecar beside the HTML. Steps 3, 4 and 5 consume that sidecar, so all three are cheap to re-run while only step 2 costs money and time.

## Run for one task

```bash
python3 generate_pilot_analysis.py \
  4372a4af-85b5-40a5-a715-193f750486c2 \
  --output ~/Downloads/pilot-task-analysis-generated.html
```

You can provide a full Horizon task link instead of the UUID.

## Run for several tasks

```bash
python3 generate_pilot_analysis.py \
  --task-file new-task-ids.txt \
  --output ~/Downloads/pilot-task-analysis-generated.html
```

Use `--base-html existing-report.html` to add the new tasks to an existing report. Use `--fresh` when the report should contain only the supplied tasks. Use `--render-existing` to rebuild the HTML from a previous report without touching Horizon.

## Run from the sheet

`--task-csv` reads the tracking sheet, where one logical task is shipped as up to three Horizon tasks: `final`, `binary` and `partial`. Every variant is analysed and the results are then collapsed to one row per logical task.

The parser is driven by the header text, not by column position. It matches "ready to go" for `final`, "binary" for `binary` and "partial" for `partial`, case-insensitively. A sheet carrying only two variants simply has no `final`.

## Rules applied

- Pass@6 uses the latest six completed rollouts with more than 10 assistant turns, on Starfall, `router-16a8dce2a6e7` or cipher-omni.
- Pass@6 is also reported per model. A pass rate is a property of the pair of artifact and model, and pooling three models hides a task that one model solved 3/6 and another solved 0/6.
- Only a rollout score of exactly `1` counts as a pass.
- The representative trajectory is the median eligible rollout by tool-call count.
- If no completed eligible trajectory exists, R/P analysis uses the most recent failed or cancelled rollout with more than 10 assistant turns and flags it as a fallback.
- AI rubrics exclude Grader Coverage and general review rubrics.
- Argus Main passes when the review passes, or when its findings contain only INFO or WARNING severity.

### Fit for pilot

All five gates must hold.

1. AI rubrics `Pass`.
2. Pass@6 below 2.
3. At least one eligible rollout, so a task nothing ran against cannot pass on an empty denominator.
4. Argus Main `Pass`.
5. R/P complete step above 20. When no completion step is available, more than 20 leading research or planning calls satisfies the gate instead.

### How variants resolve

Two different things are called a version, and only one of them inherits.

- A Horizon **task version** never inherits. The latest version is always the one read. An older version passing does not excuse the current one.
- A CSV **variant** does inherit, because the same rubric is re-fired against each shipped variant.

Rubrics and rollouts resolve independently, because a task routinely carries its rubrics on one variant and its rollouts on another. Within a variant, only the latest run of a rubric counts; retired rubric sets leave stale rows behind, and one task in this batch carries 31 rubric rows of which 19 are history. Across variants, in CSV order `final` then `binary` then `partial`, the first variant whose latest run passes settles that rubric. If none passes, the most recent failing run is reported. Every borrowed signal records the variant it came from, so any verdict can be audited back to the task ID that supplied the evidence.

### Diversity

Diversity is a property of the pool, not of a task, so it is not part of `fit_for_pilot`. Two individually perfect tasks can still be a bad pair. Eligibility is decided per task, then `select_pilot.py` applies the caps while walking the eligible list: no repository above `--max-per-repo`, no language above `--max-lang-share` of the selection, and identical content fingerprints collapsed. A shortfall is reported rather than traded away. If N cannot be reached without breaching a cap, the selection stops short and says so.

## Caches

Two caches make a re-run cheap. Both are on by default and both are keyed so that a stale entry cannot be served for changed content.

| Cache | Location | Key | Off with |
| --- | --- | --- | --- |
| R/P labels | `~/.cache/pilot-analysis/rp` | rollout ID | `--no-rp-cache` |
| Enrichment | `~/.cache/pilot-analysis/enrich` | task ID and version | `--no-cache` |

R/P labelling is the only step that costs money, so its cache is the one that matters: adding 5 tasks to a 156-task sheet pays for 5 tasks of labelling, not 156. Point `--rp-cache` elsewhere to use a different directory, and `--seed-rp-cache` adopts annotations already sitting in `runs/` so work done before the cache existed is not paid for twice.

The enrichment cache is keyed on task **and version**, so a task edited on Horizon re-fetches while everything else is served locally. `--file-workers` sets the per-task fetch concurrency and `--workers` the task concurrency; `--workers` now defaults to 16.

## Performance notes

Both matter if you touch the data path.

- **Do not join `messages` inside the rollout query.** That table is 156 million rows and 176 GB, the read-only role has `statement_timeout=120s`, and past roughly 40 tasks the planner abandons the rollout index. The result was not a slow run but a failed one: 20 tasks took 2.8 s and 40, 60, 100 and 156 tasks all timed out. Fetching the rollout IDs first and then counting messages by ID in parallel chunks, against `idx_messages_rollout_id_role`, does 156 tasks in 0.9-1.8 s.
- **`psycopg` is used when importable**, holding one connection per thread, autocommit and read-only, which drops the fixed per-query cost from 652 ms to 82 ms. It falls back to `psql` automatically, and `PILOT_NO_PSYCOPG=1` forces the fallback. Retries are bounded and only on connection failures, so a genuine query error still surfaces immediately.

## Callers of `diverse_order()`

`render_report.diverse_order()` now returns `(ordered, diagnostics)` rather than a bare list. **This is a breaking change.** Anything with its own caller must unpack the tuple. `golden_app.py` imports it and handles both shapes, degrading to less detail rather than crashing if the signature moves again.

The diagnostics distinguish a **forced** pick, where no alternative existed on that axis, from a **traded** one, where diversity was genuinely spent, and report per axis the distinct values, the floor, the first repeat, and whether the result was optimal or degenerate. A target that cannot be filled is reported as a shortfall rather than padded.

## Known gotchas

- **The sheet's columns move.** They have been re-ordered once already. That is why the CSV parser matches on header text; a positional parser silently read numbers as owner names and every task ID as absent.
- **`load_rollouts` used to hit the statement timeout.** It joined `messages` -- 156 million
  rows, 176 GB -- inside the rollout query, and the planner stopped using the rollout index
  somewhere around 40 tasks, so the run FAILED rather than slowed. It now asks for the
  rollout ids first and counts messages by those ids, which is 1.8 s at 156 tasks. Do not
  put that join back.
- **The COPY stream must be parsed with the line terminators intact.** `csv.DictReader(stdout.splitlines())` threw them away, so a `\r\n` inside a quoted message body silently became `\n`. It hit 20 of 20 rollouts and lost 80,508 of 4,498,714 characters, about 1.79%, while row counts and the rendered output stayed correct -- nothing failed, the trajectories were just slightly short, and that content is what the R/P labeller reads. Every trajectory exported before the fix is affected. The parse now goes through `io.StringIO(text, newline="")` and a regression test guards it.
- **`rollouts.local_task_id` is not always a UUID.** 122 rows hold non-UUID values, and casting them aborts the whole query. The query guards with a regex before the `::uuid` cast; do not remove it.
- **R/P labelling costs money.** Roughly $3.40 per 11 tasks, billed to whichever Horizon key
  the caller supplies. Labels are cached per rollout id under `~/.cache/pilot-analysis/rp`,
  so a re-run pays only for rollouts it has not labelled before; `--no-rp-cache` forces a
  relabel and `--seed-rp-cache` adopts annotations left in `runs/`. Re-run steps 3 and 4
  rather than step 2 when you only need a different view of the same measurements.
- **The `cloud-sql-proxy --token` route expires after about an hour.** Every connection then
  fails with "server closed the connection unexpectedly" while the proxy process stays
  alive, so it looks like a database outage. Restart the proxy, or run
  `gcloud auth application-default login` once and drop `--token`.
- **`psql` is keg-only on macOS.** The script fails on a missing `psql` even though libpq is installed. Export the path shown above.
- **The proxy port is not the tunnel port.** The IAP tunnel defaults to 15434 and the proxy example above uses 15433. Pass `--port` to match whichever you started.

## Files

| File | Role |
| --- | --- |
| `generate_pilot_analysis.py` | The measurement pass. Talks to the database, exports trajectories, labels R/P, writes HTML and a JSON sidecar. |
| `diversity_enrich.py` | Repository, language and fingerprint per task, from the API. |
| `render_report.py` | Renders the sidecar as a standalone report. |
| `select_pilot.py` | Diversity-constrained selection of the best N. |
| `golden_app.py` | Renders the selected golden set and why each task was picked at its position. |
| `test_generator.py` | Tests for the generator. |

`runs/` and the generated reports are ignored by git. They hold exported task and rollout content and must not be committed.
