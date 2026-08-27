# Pilot analysis generator

The script accepts Horizon task IDs or task links and creates a self-contained HTML report. It keeps the tasks already present in the output file and adds or refreshes the supplied tasks. The first run starts with only the supplied tasks unless you pass an existing report with `--base-html`.

## Requirements

- Run `gcloud auth login` before the first run. Your Google account needs access to the read-only Horizon database secret and IAP tunnel.
- Keep the existing transcript analysis pipeline at `~/voyager-alpharecon-rp`.
- Keep the annotation tool at `~/tool-call-clustering`.
- Put the Horizon worker key in `/tmp/hzkey`, or set `HORIZON_WORKER_KEY`.

The worker key is used only for research and planning labels. The script does not save the key in the HTML or JSON output.

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

Use `--base-html existing-report.html` to add the new tasks to an existing report. Use `--fresh` when the report should contain only the supplied tasks.

## Rules applied

- Pass@6 uses the latest six completed Starfall or `router-16a8dce2a6e7` rollouts with more than 10 assistant turns.
- Only a rollout score of exactly `1` counts as a pass.
- The representative trajectory is the median eligible rollout by tool-call count.
- AI rubrics exclude Grader Coverage and general review rubrics.
- Argus Main passes when the review passes, or when its findings contain only INFO or WARNING severity.
- Fit for Pilot requires AI rubrics Pass, Pass@6 below 2, at least one eligible rollout, Argus Main Pass, and more than 20 leading research or planning calls.

The script creates a JSON file beside the HTML. The JSON records the calculated values and makes later checks easier.
