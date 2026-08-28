# Pilot analysis generator

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

  The scripts shell out to `psql` and parse `COPY ... TO STDOUT`. They do not import a Python database driver, so `psycopg` is not needed.
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

```bash
cloud-sql-proxy --private-ip --port 15433 \
  --token "$(gcloud auth print-access-token)" \
  apex-485220:us-central1:horizon-db
```

Never paste a literal token into a script or a shell history. Use the command substitution above, or run `gcloud auth application-default login` and drop `--token` entirely. A token passed on the command line is visible to every process on the machine through `ps`.

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
```

Step 2 writes a JSON sidecar beside the HTML. Steps 3 and 4 consume that sidecar, so both are cheap to re-run while only step 2 costs money and time.

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

## Known gotchas

- **The sheet's columns move.** They have been re-ordered once already. That is why the CSV parser matches on header text; a positional parser silently read numbers as owner names and every task ID as absent.
- **`load_rollouts` hits the statement timeout at roughly 78 tasks.** Split a larger sheet into batches and merge the reports with `--base-html`.
- **`rollouts.local_task_id` is not always a UUID.** 122 rows hold non-UUID values, and casting them aborts the whole query. The query guards with a regex before the `::uuid` cast; do not remove it.
- **R/P labelling costs money.** Roughly $3.40 per 11 tasks, billed to whichever Horizon key the caller supplies. Re-run steps 3 and 4 rather than step 2 when you only need a different view of the same measurements.
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
