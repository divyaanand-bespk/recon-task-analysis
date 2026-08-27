#!/usr/bin/env python3
"""Generate the self-contained Pilot Task Analysis report from Horizon task IDs.

The script uses the read-only Horizon PostgreSQL account for task, review, and
rollout data. It uses the existing research/planning annotation pipeline for one
representative rollout per task.
"""

from __future__ import annotations

import argparse
import csv
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlparse
import uuid


DEFAULT_BASE_HTML = Path(__file__).resolve().with_name("pilot-task-analysis-base.html")
DEFAULT_PIPELINE_ROOT = Path.home() / "voyager-alpharecon-rp"
DEFAULT_TOOL_ROOT = Path.home() / "tool-call-clustering"
DEFAULT_PORT = 15434
MODEL_FILTER_SQL = "(e.model LIKE 'starfall%' OR e.model LIKE 'router-16a8dce2a6e7%')"
GLOBAL_REVIEW_NAMES = {
    "Grader Coverage",
    "Argus Lite",
    "Reward Hack",
    "Static Checklist",
    "Environment & Grading Lint",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add Horizon task analysis rows and create a self-contained HTML report."
    )
    parser.add_argument(
        "task_ids",
        nargs="*",
        help="Horizon task UUIDs or full Horizon task URLs.",
    )
    parser.add_argument(
        "--task-file",
        type=Path,
        help="Text file containing one task UUID or Horizon URL per line.",
    )
    parser.add_argument(
        "--base-html",
        type=Path,
        help="Existing report whose rows should be kept. Defaults to --output when it exists, otherwise the current report.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Create a report containing only the supplied task IDs.",
    )
    parser.add_argument(
        "--render-existing",
        action="store_true",
        help="Regenerate the report from the existing HTML without querying Horizon.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / "Downloads" / "pilot-task-analysis-generated.html",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Directory for exported and annotated trajectories.",
    )
    parser.add_argument(
        "--pipeline-root",
        type=Path,
        default=DEFAULT_PIPELINE_ROOT,
    )
    parser.add_argument(
        "--tool-root",
        type=Path,
        default=DEFAULT_TOOL_ROOT,
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--keep-run-files",
        action="store_true",
        help="Keep exported trajectories and annotation files after a successful run.",
    )
    return parser.parse_args()


def command_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"Required command not found: {name}")
    return path


def run_checked(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=capture,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-1200:]
        raise RuntimeError(f"Command failed: {command[0]}\n{detail}")
    return result


def normalize_task_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Empty task ID")
    if "://" in value:
        parsed = urlparse(value)
        value = parsed.path.rstrip("/").split("/")[-1]
    return str(uuid.UUID(value))


def collect_task_ids(args: argparse.Namespace) -> list[str]:
    values = list(args.task_ids)
    if args.task_file:
        values.extend(
            line.strip()
            for line in args.task_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        task_id = normalize_task_id(value)
        if task_id not in seen:
            seen.add(task_id)
            result.append(task_id)
    return result


class FrameParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.srcdoc: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "iframe":
            self.srcdoc = dict(attrs).get("data-srcdoc")


def extract_js_json(source: str, variable: str) -> Any:
    match = re.search(
        rf"const\s+{re.escape(variable)}\s*=\s*(.+?);(?:\n|$)", source
    )
    if not match:
        raise ValueError(f"Could not find {variable} in base report")
    return json.loads(match.group(1))


def load_existing_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    parser = FrameParser()
    parser.feed(path.read_text(encoding="utf-8"))
    if not parser.srcdoc:
        raise ValueError(f"No embedded report found in {path}")
    source = parser.srcdoc
    try:
        generated_rows = extract_js_json(source, "DATA")
    except ValueError:
        generated_rows = None
    if isinstance(generated_rows, list) and all(
        isinstance(row, dict) for row in generated_rows
    ):
        return generated_rows
    rows = extract_js_json(source, "rows")
    pass6 = extract_js_json(source, "pass6ByTask")
    denominators = extract_js_json(source, "pass6DenominatorByTask")
    eligible = extract_js_json(source, "eligibleCompletedByTask")
    incomplete = extract_js_json(source, "incompleteByTask")
    argus = extract_js_json(source, "argusByTask")
    try:
        plan_steps = extract_js_json(source, "planStepByTask")
    except ValueError:
        plan_steps = {}

    result = []
    for row in rows:
        name = row[0]
        ai_count = int(row[4])
        shape = shape_from_reviews(ai_count, [])
        item = {
            "name": name,
            "task_id": row[1],
            "shape": shape,
            "ai_rubrics": row[3],
            "ai_count": ai_count,
            "pass6": int(pass6[name]),
            "pass6_denominator": int(denominators[name]),
            "eligible_rollouts": int(eligible[name]),
            "incomplete_rollouts": int(incomplete.get(name, 0)),
            "argus_main": argus[name],
            "leading_rp": int(row[9]),
            "rp_complete_step": plan_steps.get(name),
            "total_rp": int(row[10]),
            "tool_calls": int(row[11]),
        }
        item["fit"] = fit_for_pilot(item)
        result.append(item)
    return result


class HorizonDatabase:
    def __init__(self, port: int) -> None:
        self.port = port
        self.password = ""
        self.tunnel: subprocess.Popen[str] | None = None

    def __enter__(self) -> "HorizonDatabase":
        command_path("gcloud")
        command_path("psql")
        self.password = os.environ.get("HORIZON_DB_PASSWORD", "")
        if not self.password:
            try:
                self.password = run_checked(
                    [
                        "gcloud",
                        "secrets",
                        "versions",
                        "access",
                        "latest",
                        "--secret=grafana-postgres-ro-password",
                        "--project=apex-485220",
                    ]
                ).stdout.strip()
            except RuntimeError as exc:
                raise SystemExit(
                    "Google authentication is required. Run `gcloud auth login`, then retry."
                ) from exc
        if not self.password:
            raise SystemExit("The read-only database password was empty")
        if not self.is_ready():
            self.open_tunnel()
        return self

    def __exit__(self, *_: object) -> None:
        if self.tunnel is not None:
            self.tunnel.terminate()
            try:
                self.tunnel.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.tunnel.kill()

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PGPASSWORD"] = self.password
        return env

    def is_ready(self) -> bool:
        result = subprocess.run(
            [
                "psql",
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                "grafana_ro",
                "-d",
                "horizon",
                "-tAc",
                "SELECT 1",
            ],
            env=self.environment(),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def open_tunnel(self) -> None:
        log = tempfile.TemporaryFile(mode="w+")
        self.tunnel = subprocess.Popen(
            [
                "gcloud",
                "compute",
                "ssh",
                "cloudflare-tunnel-horizon",
                "--zone=us-central1-a",
                "--project=apex-485220",
                "--tunnel-through-iap",
                "--",
                "-L",
                f"{self.port}:172.25.1.3:5432",
                "-N",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ServerAliveInterval=30",
            ],
            env=self.environment(),
            text=True,
            stdout=log,
            stderr=log,
        )
        for _ in range(20):
            if self.is_ready():
                return
            if self.tunnel.poll() is not None:
                break
            time.sleep(3)
        log.seek(0)
        detail = log.read()[-1200:]
        raise SystemExit(f"Could not open the read-only Horizon database tunnel.\n{detail}")

    def csv(self, sql: str) -> list[dict[str, str]]:
        statement = sql.strip().rstrip(";")
        result = run_checked(
            [
                "psql",
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                "grafana_ro",
                "-d",
                "horizon",
                "-c",
                f"COPY ({statement}) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)",
            ],
            env=self.environment(),
        )
        return list(csv.DictReader(result.stdout.splitlines()))


def sql_ids(task_ids: list[str]) -> str:
    return ",".join(f"'{value}'::uuid" for value in task_ids)


def result_is_pass(raw: Any) -> bool:
    if raw is True:
        return True
    if raw is None:
        return False
    text = str(raw).strip().strip('"').lower()
    if text == "pass":
        return True
    try:
        parsed = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return False
    if isinstance(parsed, dict):
        for key in ("result", "verdict", "status"):
            if str(parsed.get(key, "")).lower() == "pass":
                return True
    return False


def severities(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in {"severity", "level"} and isinstance(nested, str):
                result.append(nested.lower())
            else:
                result.extend(severities(nested))
    elif isinstance(value, list):
        for nested in value:
            result.extend(severities(nested))
    return result


def classify_argus(record: dict[str, Any] | None) -> str:
    if not record:
        return "In Progress"
    status = str(record.get("status", "")).lower()
    if status != "completed":
        return "In Progress"
    if result_is_pass(record.get("result")):
        return "Pass"
    levels = severities(record)
    if levels and set(levels) <= {"info", "warning", "warn"}:
        return "Pass"
    return "Fail"


def shape_from_reviews(count: int, names: list[str]) -> str:
    exact = {7: "Migration", 11: "Diagnosis", 13: "Optimization"}
    if count in exact:
        return exact[count]
    joined = " ".join(names).lower()
    if any(word in joined for word in ("speedup", "performance", "benchmark", "optimization")):
        return "Optimization"
    if any(word in joined for word in ("migration", "port", "equivalence")):
        return "Migration"
    return "Diagnosis"


def load_task_metadata(db: HorizonDatabase, task_ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = sql_ids(task_ids)
    tasks = db.csv(
        f"SELECT id::text AS task_id, name FROM task WHERE id IN ({ids})"
    )
    found = {row["task_id"]: {"name": row["name"]} for row in tasks}
    missing = [task_id for task_id in task_ids if task_id not in found]
    if missing:
        raise SystemExit(f"Task IDs not found in Horizon: {', '.join(missing)}")

    reviews = db.csv(
        f"""
        SELECT DISTINCT ON (ar.task_id, ar.rubric_id)
               ar.task_id::text AS task_id,
               r.name AS rubric_name,
               ar.status::text AS status,
               ar.result::text AS result,
               row_to_json(ar)::text AS review_json
        FROM rubric_ai_reviews ar
        JOIN rubrics r ON r.id = ar.rubric_id
        WHERE ar.task_id IN ({ids})
        ORDER BY ar.task_id, ar.rubric_id, ar.created_at DESC
        """
    )
    grouped: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    for row in reviews:
        record = json.loads(row["review_json"])
        record["rubric_name"] = row["rubric_name"]
        record["status"] = row["status"]
        record["result"] = row["result"]
        grouped[row["task_id"]].append(record)

    for task_id, item in found.items():
        all_reviews = grouped[task_id]
        ai_reviews = [
            review
            for review in all_reviews
            if review["rubric_name"] not in GLOBAL_REVIEW_NAMES
        ]
        ai_names = [review["rubric_name"] for review in ai_reviews]
        item["ai_count"] = len(ai_reviews)
        item["ai_rubrics"] = (
            "Pass"
            if ai_reviews
            and all(
                str(review.get("status", "")).lower() == "completed"
                and result_is_pass(review.get("result"))
                for review in ai_reviews
            )
            else "Fail"
        )
        item["shape"] = shape_from_reviews(len(ai_reviews), ai_names)
        argus = next(
            (
                review
                for review in all_reviews
                if review["rubric_name"] == "Environment & Grading Lint"
            ),
            None,
        )
        item["argus_main"] = classify_argus(argus)
    return found


def numeric_score_is_one(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return float(value) == 1.0
    except ValueError:
        return False


def load_rollouts(db: HorizonDatabase, task_ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = sql_ids(task_ids)
    rows = db.csv(
        f"""
        SELECT r.local_task_id::text AS task_id,
               r.id::text AS rollout_id,
               e.model,
               e.status::text AS evaluation_status,
               r.extracted_score::text AS extracted_score,
               r.created_at::text AS created_at,
               count(*) FILTER (WHERE m.role='assistant') AS assistant_turns,
               coalesce(sum(
                 CASE WHEN m.role='assistant'
                       AND jsonb_typeof((m.content_json::jsonb)->'tool_calls')='array'
                      THEN jsonb_array_length((m.content_json::jsonb)->'tool_calls')
                      ELSE 0 END
               ), 0) AS tool_calls
        FROM rollouts r
        JOIN evaluations e ON e.id = r.evaluation_id
        LEFT JOIN messages m ON m.rollout_id = r.id
        WHERE r.local_task_id IN ({ids}) AND {MODEL_FILTER_SQL}
        GROUP BY r.local_task_id, r.id, e.model, e.status,
                 r.extracted_score, r.created_at
        """
    )
    grouped: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    for row in rows:
        row["assistant_turns"] = int(row["assistant_turns"] or 0)
        row["tool_calls"] = int(row["tool_calls"] or 0)
        grouped[row["task_id"]].append(row)

    result: dict[str, dict[str, Any]] = {}
    for task_id, candidates in grouped.items():
        long_enough = [row for row in candidates if row["assistant_turns"] > 10]
        completed = [
            row
            for row in long_enough
            if row["evaluation_status"].lower() == "completed"
        ]
        completed.sort(key=lambda row: (row["created_at"], row["rollout_id"]), reverse=True)
        latest = completed[:6]
        median_candidates = sorted(
            completed,
            key=lambda row: (row["tool_calls"], row["rollout_id"]),
        )
        representative = (
            median_candidates[len(median_candidates) // 2]
            if median_candidates
            else None
        )
        result[task_id] = {
            "pass6": sum(numeric_score_is_one(row["extracted_score"]) for row in latest),
            "pass6_denominator": len(latest),
            "eligible_rollouts": len(completed),
            "incomplete_rollouts": len(long_enough) - len(completed),
            "representative": representative,
        }
    return result


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:80] or "task"


def export_messages(
    db: HorizonDatabase,
    rollout_id: str,
    task_dir: Path,
) -> None:
    rollout_id = str(uuid.UUID(rollout_id))
    rows = db.csv(
        f"""
        SELECT sequence_number::text AS sequence_number,
               role,
               coalesce(content, '') AS content,
               coalesce(content_json::text, '') AS content_json
        FROM messages
        WHERE rollout_id='{rollout_id}'::uuid
        ORDER BY sequence_number
        """
    )
    instruction = next(
        (row["content"] for row in rows if row["role"] == "user" and row["content"].strip()),
        None,
    )
    if not instruction:
        raise RuntimeError(f"Rollout {rollout_id} has no user instruction")
    task_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for row in rows:
        record: dict[str, Any] = {
            "rollout_id": rollout_id,
            "sequence_number": int(row["sequence_number"]),
            "role": row["role"],
            "content": row["content"],
        }
        if row["content_json"]:
            try:
                parsed = json.loads(row["content_json"])
                if isinstance(parsed, dict):
                    record["content_json"] = parsed
            except json.JSONDecodeError:
                pass
        records.append(json.dumps(record, ensure_ascii=False))
    (task_dir / "instruction.md").write_text(
        instruction.strip() + "\n", encoding="utf-8"
    )
    (task_dir / "trajectory-001.jsonl").write_text(
        "\n".join(records) + "\n", encoding="utf-8"
    )


def prepare_worker_key() -> tuple[Path, bool]:
    target = Path("/tmp/hzkey")
    if target.is_file() and target.read_text(encoding="utf-8").strip():
        return target, False
    source = os.environ.get("HORIZON_WORKER_KEY_FILE")
    key = ""
    if source:
        key = Path(source).expanduser().read_text(encoding="utf-8").strip()
    if not key:
        key = os.environ.get("HORIZON_WORKER_KEY", "").strip()
    if not key:
        raise SystemExit(
            "Set HORIZON_WORKER_KEY, set HORIZON_WORKER_KEY_FILE, or create /tmp/hzkey."
        )
    target.write_text(key + "\n", encoding="utf-8")
    target.chmod(0o600)
    return target, True


def prepare_and_label(
    task_names: list[str],
    run_dir: Path,
    pipeline_root: Path,
    tool_root: Path,
) -> dict[str, Path]:
    annotation_tool = tool_root / ".venv/bin/annotation-tool"
    labeler = pipeline_root / "label_rp.py"
    if not annotation_tool.is_file():
        raise SystemExit(f"Annotation tool not found: {annotation_tool}")
    if not labeler.is_file():
        raise SystemExit(f"R/P labeler not found: {labeler}")
    outputs = run_dir / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    for name in task_names:
        run_checked(
            [
                str(annotation_tool),
                "research-planning",
                "prepare",
                "--task-dir",
                str(run_dir / "tasks" / name),
                "--output-dir",
                str(outputs / name),
            ],
            cwd=tool_root,
        )

    key_path, remove_key = prepare_worker_key()
    try:
        run_checked(
            [
                sys.executable,
                str(labeler),
                "--outputs",
                str(outputs),
                "--model",
                "starfall",
                "--chunk",
                "40",
                "--concurrency",
                "8",
            ],
            capture=False,
        )
    finally:
        if remove_key and key_path.exists():
            key_path.unlink()

    result: dict[str, Path] = {}
    for name in task_names:
        output = outputs / name
        merge_and_finalize(output, annotation_tool, tool_root)
        result[name] = output / "annotated_trajectories.json"
    return result


def merge_and_finalize(output: Path, annotation_tool: Path, tool_root: Path) -> None:
    packet = json.loads((output / "task_packet.json").read_text(encoding="utf-8"))
    order = [
        action["action_id"]
        for trajectory in packet["trajectories"]
        for action in trajectory["actions"]
    ]
    layer = output / "layers/research_planning"
    labels: dict[str, dict[str, Any]] = {}
    for chunk in sorted((layer / "chunks").glob("chunk_*.json")):
        for item in json.loads(chunk.read_text(encoding="utf-8")):
            labels[item["action_id"]] = item
    missing = [action_id for action_id in order if action_id not in labels]
    if missing:
        raise RuntimeError(f"{output.name} has {len(missing)} missing R/P labels")
    annotation_path = layer / "annotation.json"
    annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
    annotation["annotator"] = "headless-starfall (pilot-analysis-generator)"
    annotation["actions"] = [labels[action_id] for action_id in order]
    annotation_path.write_text(
        json.dumps(annotation, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    run_checked(
        [
            str(annotation_tool),
            "research-planning",
            "finalize",
            "--output-dir",
            str(output),
        ],
        cwd=tool_root,
    )


WRITE_PATTERN = re.compile(
    r"(?:write_text\s*\(|open\s*\(|cat\s+<<|base64\s+-d\s*>|"
    r">\s*(?:/app/|/workspace/repo/)?RESEARCH_AND_(?:PLANNING|IMPLEMENTATION)\.md|"
    r"python3\s+(?:/tmp/|make_))",
    re.IGNORECASE,
)


def rp_metrics(annotated_path: Path) -> dict[str, Any]:
    data = json.loads(annotated_path.read_text(encoding="utf-8"))
    trajectories = data.get("trajectories") or []
    if not trajectories:
        raise RuntimeError(f"No trajectory in {annotated_path}")
    actions = trajectories[0].get("actions") or []
    flags = []
    for action in actions:
        label = (
            (action.get("annotations") or {}).get("research_planning")
            or action.get("research_planning")
            or {}
        )
        flags.append(bool(label.get("research") or label.get("planning")))
    leading = 0
    for flag in flags:
        if not flag:
            break
        leading += 1
    completion_steps = []
    for action in actions:
        command = str((action.get("arguments") or {}).get("keystrokes", ""))
        if (
            re.search(r"RESEARCH_AND_(?:PLANNING|IMPLEMENTATION)\.md", command)
            and WRITE_PATTERN.search(command)
        ):
            completion_steps.append(int(action.get("ordinal", 0)))
    return {
        "leading_rp": leading,
        "total_rp": sum(flags),
        "tool_calls": len(actions),
        "rp_complete_step": max(completion_steps) if completion_steps else None,
    }


def fit_for_pilot(item: dict[str, Any]) -> str:
    return (
        "YES"
        if item.get("ai_rubrics") == "Pass"
        and int(item.get("pass6", 99)) < 2
        and int(item.get("pass6_denominator", 0)) > 0
        and item.get("argus_main") == "Pass"
        and int(item.get("leading_rp", 0)) > 20
        else "NO"
    )


def analyse_tasks(
    db: HorizonDatabase,
    task_ids: list[str],
    run_dir: Path,
    pipeline_root: Path,
    tool_root: Path,
) -> list[dict[str, Any]]:
    metadata = load_task_metadata(db, task_ids)
    rollout_data = load_rollouts(db, task_ids)
    names: dict[str, str] = {}
    for task_id in task_ids:
        representative = rollout_data[task_id]["representative"]
        if not representative:
            raise RuntimeError(
                f"{metadata[task_id]['name']} has no completed Starfall/router rollout "
                "with more than 10 assistant turns"
            )
        dirname = safe_name(metadata[task_id]["name"])
        if dirname in names.values():
            dirname = f"{dirname}__{task_id[:8]}"
        names[task_id] = dirname
        export_messages(
            db,
            representative["rollout_id"],
            run_dir / "tasks" / dirname,
        )

    paths = prepare_and_label(
        list(names.values()), run_dir, pipeline_root, tool_root
    )
    result = []
    for task_id in task_ids:
        item = {
            "task_id": task_id,
            **metadata[task_id],
            **{
                key: value
                for key, value in rollout_data[task_id].items()
                if key != "representative"
            },
            **rp_metrics(paths[names[task_id]]),
        }
        item["fit"] = fit_for_pilot(item)
        result.append(item)
    return result


def js_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def make_inner_html(rows: list[dict[str, Any]], updated: str) -> str:
    data = js_json(rows)
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>Pilot task analysis</title>
<style>
:root {{ color-scheme: light dark; --bg: light-dark(#fff,#181818); --fg: light-dark(#171717,#f4f4f5); --muted: light-dark(#666,#a1a1aa); --border: light-dark(#dedede,#3f3f46); --card: light-dark(#fafafa,#222); --green:#16803c; --red:#c93838; --orange:#b66a00; }}
* {{ box-sizing:border-box; }}
html,body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.35 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
body {{ padding:20px; }}
main {{ display:grid; gap:20px; }}
h1,p {{ margin:0; }}
.muted {{ color:var(--muted); }}
.small {{ font-size:.88em; }}
.summary {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }}
.card {{ border:1px solid var(--border); background:var(--card); border-radius:10px; padding:14px; display:grid; gap:4px; }}
.value {{ font-size:1.8rem; font-weight:650; }}
.toolbar {{ display:flex; align-items:center; gap:8px; }}
select {{ font:inherit; color:inherit; background:var(--bg); border:1px solid var(--border); border-radius:7px; padding:6px 9px; }}
.table-wrap {{ overflow-x:clip; }}
table {{ width:100%; min-width:0; table-layout:fixed; border-collapse:collapse; font-size:clamp(12px,.95vw,15px); }}
th,td {{ padding:7px clamp(2px,.45vw,8px); border-bottom:1px solid var(--border); overflow-wrap:anywhere; vertical-align:middle; }}
th {{ color:var(--muted); font-weight:600; text-align:left; vertical-align:bottom; }}
th:nth-child(1),td:nth-child(1) {{ width:23%; }}
th:nth-child(2),td:nth-child(2) {{ width:10%; }}
th:nth-child(3),td:nth-child(3) {{ width:8%; }}
th:nth-child(4),td:nth-child(4) {{ width:10%; }}
th:nth-child(5),td:nth-child(5) {{ width:9%; }}
th:nth-child(6),td:nth-child(6) {{ width:7%; }}
th:nth-child(7),td:nth-child(7) {{ width:8%; }}
th:nth-child(8),td:nth-child(8) {{ width:7%; }}
th:nth-child(9),td:nth-child(9) {{ width:7%; }}
th:nth-child(10),td:nth-child(10) {{ width:11%; }}
.center {{ text-align:center; }} .right {{ text-align:right; }}
.task-name {{ display:block; font-weight:600; }}
.task-id {{ display:block; margin-top:2px; color:var(--muted); font-size:.8em; word-break:break-all; }}
.pass,.fit-yes,.pass6-good {{ color:var(--green); font-weight:650; }}
.fail,.pass6-bad {{ color:var(--red); font-weight:650; }}
.progress {{ color:var(--orange); font-weight:650; }}
.fit-no {{ color:var(--muted); font-weight:650; }}
footer {{ display:grid; gap:4px; border-top:1px solid var(--border); padding-top:8px; color:var(--muted); font-size:.88em; }}
@media(max-width:680px) {{ .summary {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} body {{ padding:10px; }} }}
</style>
</head>
<body>
<main>
  <header><h1>Pilot task analysis</h1><p class="muted small">{len(rows)} tasks. Updated from Horizon on {html.escape(updated)}.</p></header>
  <section class="summary" aria-label="Pilot selection summary">
    <div class="card"><span class="muted">Fit for pilot</span><span id="fit-count" class="value">0</span><span class="muted small">of {len(rows)} tasks</span></div>
    <div class="card"><span class="muted">AI rubrics pass</span><span id="ai-count" class="value">0</span><span class="muted small">of {len(rows)} tasks</span></div>
    <div class="card"><span class="muted">Pass@6 below 2</span><span id="pass6-count" class="value">0</span><span class="muted small">of {len(rows)} tasks</span></div>
    <div class="card"><span class="muted">Argus Main pass</span><span id="argus-count" class="value">0</span><span class="muted small">of {len(rows)} tasks</span></div>
  </section>
  <div class="toolbar"><label for="shape-filter" class="muted">Shape</label><select id="shape-filter"><option value="all">All shapes</option><option>Diagnosis</option><option>Migration</option><option>Optimization</option></select></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Task</th><th>Shape</th><th class="center">AI rubrics</th><th class="center">Gemini 3.7 Pass@6</th><th class="center">Argus Main</th><th class="right">Leading R/P</th><th class="center" title="Final successful RESEARCH_AND_PLANNING.md write step">R/P complete step</th><th class="right">Total R/P</th><th class="right">Tool calls</th><th class="center">Fit for pilot</th></tr></thead>
    <tbody id="rows"></tbody>
  </table></div>
  <footer>
    <p>Fit requires AI rubrics Pass, fewer than 2 passes among available completed eligible Gemini 3.7 rollouts, Argus Main Pass, and more than 20 leading R/P calls. A denominator from 1 to 6 is valid.</p>
    <p>Pass@6 uses up to the latest six completed Starfall or router-16a8dce2a6e7 rollouts with more than 10 reconstructed assistant turns. Only a score of exactly 1 counts as Pass.</p>
    <p>R/P complete step is the final write step for RESEARCH_AND_PLANNING.md or RESEARCH_AND_IMPLEMENTATION.md in the representative trajectory. A dash means no write was found.</p>
    <p>AI rubrics exclude Grader Coverage and general review rubrics. INFO or WARNING only Argus Main findings count as Pass.</p>
  </footer>
</main>
<script>
const DATA={data};
const esc=value=>String(value).replace(/[&<>"']/g,ch=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
const statusClass=value=>value==='Pass'?'pass':value==='Fail'?'fail':'progress';
const ordered=[...DATA].sort((a,b)=>(a.fit==='YES'?0:1)-(b.fit==='YES'?0:1)||b.leading_rp-a.leading_rp||a.name.localeCompare(b.name));
document.querySelector('#fit-count').textContent=ordered.filter(row=>row.fit==='YES').length;
document.querySelector('#ai-count').textContent=ordered.filter(row=>row.ai_rubrics==='Pass').length;
document.querySelector('#pass6-count').textContent=ordered.filter(row=>row.pass6_denominator>0&&row.pass6<2).length;
document.querySelector('#argus-count').textContent=ordered.filter(row=>row.argus_main==='Pass').length;
const tbody=document.querySelector('#rows');
const filter=document.querySelector('#shape-filter');
function render(){{
  tbody.innerHTML=ordered.filter(row=>filter.value==='all'||row.shape===filter.value).map(row=>`<tr>
    <td><span class="task-name">${{esc(row.name)}}</span><a class="task-id" href="https://horizon.bespokelabs.ai/tasks/${{encodeURIComponent(row.task_id)}}" target="_blank" rel="noopener noreferrer">${{esc(row.task_id)}}</a></td>
    <td>${{esc(row.shape)}}</td>
    <td class="center"><span class="${{statusClass(row.ai_rubrics)}}" title="${{row.ai_count}} applicable reviews">${{esc(row.ai_rubrics)}}</span></td>
    <td class="center"><span class="${{row.pass6<2?'pass6-good':'pass6-bad'}}" title="Latest ${{row.pass6_denominator}} of ${{row.eligible_rollouts}} completed eligible rollouts. ${{row.incomplete_rollouts}} incomplete excluded.">${{row.pass6}}/${{row.pass6_denominator}}</span></td>
    <td class="center"><span class="${{statusClass(row.argus_main)}}">${{esc(row.argus_main)}}</span></td>
    <td class="right">${{row.leading_rp}}</td>
    <td class="center">${{row.rp_complete_step??'—'}}</td>
    <td class="right">${{row.total_rp}}</td>
    <td class="right">${{row.tool_calls}}</td>
    <td class="center"><span class="${{row.fit==='YES'?'fit-yes':'fit-no'}}">${{row.fit}}</span></td>
  </tr>`).join('');
  parent.postMessage({{type:'pilot-report-height',height:document.documentElement.scrollHeight}},'*');
}}
filter.addEventListener('change',render); render();
new ResizeObserver(()=>parent.postMessage({{type:'pilot-report-height',height:document.documentElement.scrollHeight}},'*')).observe(document.body);
</script>
</body></html>"""


def make_outer_html(inner: str) -> str:
    encoded = html.escape(inner, quote=True)
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; connect-src 'none'; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>Pilot task analysis</title>
<style>html,body{{margin:0;background:#fff}}iframe{{display:block;width:100%;height:100vh;border:0}}</style>
</head>
<body>
<iframe id="report" sandbox="allow-scripts" scrolling="no" referrerpolicy="no-referrer" title="Pilot task analysis" data-srcdoc="{encoded}"></iframe>
<script>
const frame=document.querySelector('#report');
frame.srcdoc=frame.dataset.srcdoc;
addEventListener('message',event=>{{if(event.source===frame.contentWindow&&event.data?.type==='pilot-report-height')frame.style.height=`${{Math.max(600,Number(event.data.height)||0)}}px`;}});
</script>
</body></html>"""


def write_outputs(rows: list[dict[str, Any]], output: Path) -> None:
    rows = sorted(rows, key=lambda row: row["name"].lower())
    updated = time.strftime("%d %B %Y", time.gmtime())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(make_outer_html(make_inner_html(rows, updated)), encoding="utf-8")
    sidecar = output.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "rules": {
                    "rollout_models": ["starfall", "router-16a8dce2a6e7"],
                    "minimum_assistant_turns_exclusive": 10,
                    "completed_only": True,
                    "binary_pass_score": 1,
                    "pass6_max_rollouts": 6,
                },
                "tasks": rows,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"HTML: {output}")
    print(f"Data: {sidecar}")


def main() -> None:
    args = parse_args()
    task_ids = collect_task_ids(args)
    base_html = args.base_html or (
        args.output if args.output.is_file() else DEFAULT_BASE_HTML
    )
    existing = [] if args.fresh else load_existing_rows(base_html)
    if args.render_existing:
        if task_ids:
            raise SystemExit("--render-existing cannot be combined with task IDs")
        write_outputs(existing, args.output)
        return
    if not task_ids:
        raise SystemExit("Provide at least one task ID, task URL, or --task-file")

    stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    run_dir = args.run_dir or Path(__file__).resolve().parent / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        with HorizonDatabase(args.port) as db:
            new_rows = analyse_tasks(
                db,
                task_ids,
                run_dir,
                args.pipeline_root.expanduser(),
                args.tool_root.expanduser(),
            )
        by_id = {row["task_id"]: row for row in existing}
        for row in new_rows:
            by_id[row["task_id"]] = row
        write_outputs(list(by_id.values()), args.output)
    except Exception:
        print(f"Run files kept for debugging: {run_dir}", file=sys.stderr)
        raise
    else:
        if not args.keep_run_files:
            shutil.rmtree(run_dir)


if __name__ == "__main__":
    main()
