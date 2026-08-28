#!/usr/bin/env python3
"""Generate the self-contained Pilot Task Analysis report from Horizon task IDs.

The script uses the read-only Horizon PostgreSQL account for task, review, and
rollout data. It uses the existing research/planning annotation pipeline for one
representative rollout per task.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import csv
import html
import io
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlparse
import uuid

try:  # psycopg removes a process spawn AND a TLS handshake per query.
    import psycopg as _psycopg
except Exception:  # pragma: no cover - the psql path stays fully supported
    _psycopg = None


DEFAULT_BASE_HTML = Path(__file__).resolve().with_name("pilot-task-analysis-base.html")
DEFAULT_PIPELINE_ROOT = Path.home() / "voyager-alpharecon-rp"
DEFAULT_TOOL_ROOT = Path.home() / "tool-call-clustering"
DEFAULT_PORT = 15434
# Two read-only routes reach the same instance. The IAP tunnel needs
# iap.tunnelResourceAccessor + compute.viewer; the cloud-sql-proxy route over
# WARP needs only secretmanager access to the role's password. Setting
# HORIZON_DB_ROLE (and HORIZON_DB_PASSWORD, or HORIZON_DB_SECRET) lets an
# already-running proxy be reused, and is_ready() then skips opening a tunnel.
DB_ROLE = os.environ.get("HORIZON_DB_ROLE", "grafana_ro")
DB_SECRET = os.environ.get("HORIZON_DB_SECRET", "grafana-postgres-ro-password")
MODEL_FILTER_SQL = (
    "(e.model LIKE 'starfall%' OR e.model LIKE 'router-16a8dce2a6e7%'"
    " OR e.model LIKE 'cipher-omni%')"
)
# Pass@6 is reported PER MODEL as well as pooled: a pass rate is a property of
# (artifact x model), and this batch was measured on three of them. Pooling them
# hides that a task solved 3/6 by one model was solved 0/6 by another.
MODEL_COLUMNS = [("glm", "cipher-omni"), ("router", "router-16a8dce2a6e7"),
                 ("starfall", "starfall")]
GLOBAL_REVIEW_NAMES = {
    "grader coverage",
    "argus lite",
    "reward hack",
    "static checklist",
    "environment & grading lint",
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
        "--task-csv",
        type=Path,
        help=(
            "CSV where each row is ONE logical task shipped as up to three Horizon "
            "tasks: final, binary, partial. Every variant is analysed, then each "
            "signal trickles down to the first variant that carries it."
        ),
    )
    parser.add_argument(
        "--enrich",
        type=Path,
        help="diversity_enrich.py output, adding repository and language per task.",
    )
    parser.add_argument("--max-per-repo", type=int, default=3)
    parser.add_argument("--max-lang-share", type=float, default=0.5)
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
        "--jobs", type=int, default=12,
        help="Parallel workers for per-task export/prepare/merge. Each step shells"
             " out once per task, so these loops dominate wall-clock as the sheet grows.")
    parser.add_argument(
        "--label-concurrency", type=int, default=16,
        help="Concurrent chunks for the R/P labelling pass.")
    parser.add_argument(
        "--keep-run-files",
        action="store_true",
        help="Keep exported trajectories and annotation files after a successful run.",
    )
    parser.add_argument(
        "--rp-cache",
        type=Path,
        default=DEFAULT_RP_CACHE,
        help="Directory of finalized R/P annotations keyed by rollout id. The"
             " labelling pass is the run's dominant cost and is deterministic per"
             " rollout, so a re-run only pays for rollouts it has not seen.")
    parser.add_argument(
        "--no-rp-cache",
        action="store_true",
        help="Label every rollout again, ignoring and not writing the cache.")
    parser.add_argument(
        "--seed-rp-cache",
        action="store_true",
        help="Before running, adopt annotations from kept run directories under"
             " runs/ into the cache, then continue normally.")
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


VARIANT_ORDER = ("final", "binary", "partial")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def read_variant_csv(path: Path) -> list[dict[str, Any]]:
    """One row per logical task, with its variant task ids.

    Columns are located BY HEADER TEXT, not position. The sheet has already been
    re-ordered once -- "Responsible" moved from column 2 to column 0 and the
    separate "ready to go" column merged into "Binary Version" -- and positional
    parsing does not fail loudly when that happens: it silently reads version
    numbers as owner names and every task id as absent. Matching on header text
    survives re-ordering and added columns.

    A sheet may carry two variants (binary, partial) or three (final as well);
    whichever are present are returned, and the trickle walks them in order.
    """
    import csv as _csv

    rows = list(_csv.reader(path.read_text(encoding="utf-8").splitlines()))
    if not rows:
        return []
    header = [h.strip().casefold() for h in rows[0]]

    def find(*needles: str, exclude: tuple[str, ...] = ()) -> int | None:
        for i, cell in enumerate(header):
            if any(n in cell for n in needles) and not any(x in cell for x in exclude):
                return i
        return None

    # "binary" also carries the ready-to-go task in the current sheet, so a
    # sheet without a separate final column simply has no final variant.
    idx = {
        "final": find("ready to go", exclude=("binary",)),
        "binary": find("binary"),
        "partial": find("partial"),
    }
    who = find("responsible")
    reviewer = find("reviewer")
    status = find("status")
    if not any(v is not None for v in idx.values()):
        raise SystemExit(
            f"No task-id columns found in {path.name}. Header was: {rows[0]}")

    groups: list[dict[str, Any]] = []
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue

        def grab(i: int | None) -> str | None:
            if i is None or i >= len(row):
                return None
            found = _UUID_RE.findall(row[i])
            return str(uuid.UUID(found[0])) if found else None

        def text(i: int | None) -> str:
            return row[i].strip() if i is not None and i < len(row) else ""

        group = {name: grab(i) for name, i in idx.items()}
        if not any(group.values()):
            continue
        group["responsible"] = text(who)
        group["reviewer"] = text(reviewer)
        group["sheet_status"] = text(status)
        groups.append(group)
    return groups


def collect_task_ids(args: argparse.Namespace) -> list[str]:
    values = list(args.task_ids)
    if getattr(args, "task_csv", None):
        args.variant_groups = read_variant_csv(args.task_csv)
        for group in args.variant_groups:
            values.extend(
                group[key] for key in VARIANT_ORDER if group.get(key)
            )
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
    """Read-only Horizon access over whichever local route is already up.

    Every query used to be its own `psql` process: a spawn plus a fresh TLS
    handshake through the proxy, measured at ~650 ms before the server does any
    work. A psycopg connection, kept per thread, pays that once. The wire
    format is left alone -- both routes run the SAME `COPY (...) TO STDOUT`
    statement, so the server produces identical bytes and the psql path stays a
    working fallback for anyone without psycopg installed.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.password = ""
        self.tunnel: subprocess.Popen[str] | None = None
        self._local = threading.local()
        self._connections: list[Any] = []
        self._connection_lock = threading.Lock()
        self._pool: cf.ThreadPoolExecutor | None = None
        self._pool_size = 0
        # A connection costs ~560 ms and the local proxy handles a burst of them
        # badly: twelve at once measured 16.5 s against 2.0 s once warm. Queries
        # stay fully concurrent; only the handshakes queue.
        self._connect_slots = threading.Semaphore(4)
        # PILOT_NO_PSYCOPG=1 forces the original psql path, which is the
        # comparison used to prove the two routes agree.
        self._use_psycopg = (
            _psycopg is not None
            and os.environ.get("PILOT_NO_PSYCOPG", "").strip() != "1"
        )

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
                        f"--secret={DB_SECRET}",
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
        with self._connection_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)
        with self._connection_lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            try:
                connection.close()
            except Exception:
                pass
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
                DB_ROLE,
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

    def _connection(self) -> Any:
        """One connection per thread; the export pass runs in a thread pool."""
        connection = getattr(self._local, "connection", None)
        if connection is not None and not connection.closed:
            return connection
        with self._connect_slots:
            connection = self._open_connection()
        self._local.connection = connection
        with self._connection_lock:
            self._connections.append(connection)
        return connection

    def _open_connection(self) -> Any:
        connection = _psycopg.connect(
            host="127.0.0.1",
            port=self.port,
            user=DB_ROLE,
            dbname="horizon",
            password=self.password,
            connect_timeout=30,
            # Without autocommit a COPY leaves a transaction open, and the
            # read-only role sets idle_in_transaction_session_timeout=120s,
            # which would kill a pooled connection between batches.
            autocommit=True,
        )
        # The role is already SELECT-only; this is the second lock on the door.
        connection.read_only = True
        return connection

    def pool(self, jobs: int) -> cf.ThreadPoolExecutor:
        """One pool for the whole run, so threads -- and their connections --
        are reused across stages.

        A fresh ThreadPoolExecutor per stage meant fresh threads, and a
        thread-local connection means a fresh CONNECTION per stage: metadata,
        message counts and export each paid for twelve of them. Reusing the pool
        pays once.
        """
        with self._connection_lock:
            if self._pool is None or jobs > self._pool_size:
                if self._pool is not None:
                    self._pool.shutdown(wait=True)
                self._pool_size = max(jobs, self._pool_size)
                self._pool = cf.ThreadPoolExecutor(
                    self._pool_size, thread_name_prefix="horizon")
            return self._pool

    def _csv_psycopg(self, copy_sql: str) -> str:
        connection = self._connection()
        blocks: list[bytes] = []
        with connection.cursor() as cursor:
            with cursor.copy(copy_sql) as copy:
                for block in copy:
                    blocks.append(bytes(block))
        return b"".join(blocks).decode("utf-8", "replace")

    def _csv_psql(self, copy_sql: str) -> str:
        result = run_checked(
            [
                "psql",
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                DB_ROLE,
                "-d",
                "horizon",
                "-c",
                copy_sql,
            ],
            env=self.environment(),
        )
        return result.stdout

    def _query(self, copy_sql: str) -> str:
        if self._use_psycopg:
            try:
                return self._csv_psycopg(copy_sql)
            except _psycopg.OperationalError as exc:
                # The route died, not the query. Drop a dead pooled connection
                # and let the caller decide whether to retry.
                connection = getattr(self._local, "connection", None)
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                    self._local.connection = None
                raise RuntimeError(f"Connection lost: {exc}") from exc
            except _psycopg.Error as exc:
                raise RuntimeError(f"Query failed: {exc}") from exc
        return self._csv_psql(copy_sql)

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            phrase in text
            for phrase in (
                "connection lost",
                "server closed the connection",
                "could not connect",
                "connection refused",
                "connection reset",
                "terminating connection",
                "ssl connection has been closed",
                "the connection is closed",
            )
        )

    def csv(self, sql: str, attempts: int = 4) -> list[dict[str, str]]:
        statement = sql.strip().rstrip(";")
        copy_sql = f"COPY ({statement}) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)"
        # The local proxy flaps -- an expired --token drops every connection at
        # once -- and a run that dies at the export stage has already paid for
        # its R/P labels. Only CONNECTION failures are retried; a statement
        # timeout or a SQL error is reported on the first try, because retrying
        # those just spends the same time again to fail the same way.
        text = ""
        for attempt in range(1, attempts + 1):
            try:
                text = self._query(copy_sql)
                break
            except RuntimeError as exc:
                if attempt == attempts or not self._is_connection_error(exc):
                    raise
                print(f"database connection lost (attempt {attempt}/{attempts}), "
                      f"retrying in {attempt * 2}s", file=sys.stderr, flush=True)
                time.sleep(attempt * 2)
        # newline="" is the csv module's documented contract: it keeps a
        # newline inside a quoted field -- trajectory content is full of them --
        # from being read as a record boundary.
        return list(csv.DictReader(io.StringIO(text, newline="")))


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
    elif isinstance(value, str) and value.lower() in {
        "critical",
        "error",
        "info",
        "warning",
        "warn",
    }:
        result.append(value.lower())
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


def is_global_review(name: str) -> bool:
    base_name = re.sub(r"^\[[^]]+\]\s*", "", name).strip().casefold()
    return base_name in GLOBAL_REVIEW_NAMES


def ai_review_passes(review: dict[str, Any],
                    ignore_grader_coverage: bool = True) -> bool:
    name = str(review.get("rubric_name", ""))
    if (ignore_grader_coverage
            and re.sub(r"^\[[^]]+\]\s*", "", name).strip().casefold() == "grader coverage"):
        return True
    return (
        str(review.get("status", "")).lower() == "completed"
        and result_is_pass(review.get("result"))
    )


def load_task_metadata(db: HorizonDatabase, task_ids: list[str],
                       jobs: int = 12) -> dict[str, dict[str, Any]]:
    # Chunked by task, for the same reason load_rollouts is: an IN list that
    # grows with the sheet is a query whose runtime grows with the sheet, and
    # the ceiling is a hard 120 s statement timeout, not a slow report.
    #
    # This query is NOT currently near that ceiling -- measured against the 2000
    # tasks in Horizon carrying the MOST rubric rows, four times the pilot
    # sheet's density, it returns 45,219 rows in 48 s, and its cost per task
    # FALLS as the list grows because the plan is index-driven throughout
    # (idx_rubric_ai_reviews_task_id, and the findings subquery is memoized at
    # 0.003 ms a row).
    #
    # So this uses a FIXED cap rather than balanced_chunks(): below the cap the
    # whole sheet is one chunk that runs inline on the caller's warm connection,
    # and above it the work splits and goes parallel. Splitting a 156-task sheet
    # across twelve workers measured SLOWER -- 2.6 s against 1.9 s -- because
    # twelve connections cost more than the query saves. Concurrency here is
    # insurance against the timeout, not a speed-up, so it should not be bought
    # until it is needed.
    #
    # Chunking is only sound because DISTINCT ON dedupes WITHIN a task: no task
    # spans two chunks, so each chunk resolves its own tasks completely and the
    # rows for one task keep their relative order.
    chunks = chunked(task_ids, TASK_METADATA_CHUNK)

    def fetch_names(chunk: list[str]) -> list[dict[str, str]]:
        return db.csv(
            f"""SELECT t.id::text AS task_id, t.name,
                       COALESCE(b.name, '') AS batch_name
                FROM task t LEFT JOIN batches b ON b.id = t.batch_id
                WHERE t.id IN ({sql_ids(chunk)})"""
        )

    tasks = [row for rows in map_chunks(db, fetch_names, chunks, jobs) for row in rows]
    found = {row["task_id"]: {"name": row["name"],
                             "batch_name": row.get("batch_name", "")}
             for row in tasks}
    missing = [task_id for task_id in task_ids if task_id not in found]
    if missing:
        raise SystemExit(f"Task IDs not found in Horizon: {', '.join(missing)}")

    def fetch_reviews(chunk: list[str]) -> list[dict[str, str]]:
        ids = sql_ids(chunk)
        return db.csv(
            f"""
            SELECT DISTINCT ON (ar.task_id, ar.rubric_id)
                   ar.task_id::text AS task_id,
                   r.name AS rubric_name,
                   ar.status::text AS status,
                   ar.result::text AS result,
                   coalesce((
                       SELECT jsonb_agg(f.severity::text ORDER BY f.finding_id)
                       FROM rubric_ai_review_findings f
                       WHERE f.rubric_ai_review_id = ar.id
                   ), '[]'::jsonb)::text AS finding_severities,
                   row_to_json(ar)::text AS review_json
            FROM rubric_ai_reviews ar
            JOIN rubrics r ON r.id = ar.rubric_id
            WHERE ar.task_id IN ({ids})
            ORDER BY ar.task_id, ar.rubric_id, ar.task_version DESC, ar.created_at DESC
            """
        )

    reviews = [row for rows in map_chunks(db, fetch_reviews, chunks, jobs) for row in rows]
    grouped: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    for row in reviews:
        record = json.loads(row["review_json"])
        record["rubric_name"] = row["rubric_name"]
        record["status"] = row["status"]
        record["result"] = row["result"]
        record["finding_severities"] = json.loads(row["finding_severities"])
        grouped[row["task_id"]].append(record)

    for task_id, item in found.items():
        all_reviews = grouped[task_id]
        ai_reviews = [
            review
            for review in all_reviews
            if not is_global_review(review["rubric_name"])
        ]
        ai_names = [review["rubric_name"] for review in ai_reviews]
        grader_coverage_reviews = [
            review
            for review in all_reviews
            if re.sub(r"^\[[^]]+\]\s*", "", review["rubric_name"])
            .strip()
            .casefold()
            == "grader coverage"
        ]
        pass_reviews = ai_reviews + grader_coverage_reviews
        # Retain the reviews themselves: the variant collapse needs to inherit a
        # pass per rubric across variants, which a count cannot express.
        item["ai_reviews"] = pass_reviews
        item["ai_count"] = len(ai_reviews)
        # "None" is not "Fail": a task with no applicable reviews has not been
        # judged, and collapsing the two makes an unreviewed task look rejected.
        if not pass_reviews:
            item["ai_rubrics"] = "None"
            item["ai_rubrics_strict"] = "None"
        else:
            item["ai_rubrics"] = (
                "Pass" if all(ai_review_passes(r) for r in pass_reviews) else "Fail")
            item["ai_rubrics_strict"] = (
                "Pass" if all(ai_review_passes(r, ignore_grader_coverage=False)
                              for r in pass_reviews) else "Fail")
        item["grader_coverage"] = next(
            (r.get("result") for r in grader_coverage_reviews), None)
        item["shape"] = (shape_from_batch(item["batch_name"])
                         if item.get("batch_name")
                         else shape_from_reviews(len(ai_reviews), ai_names))
        argus = next(
            (
                review
                for review in all_reviews
                if review["rubric_name"] == "Environment & Grading Lint"
            ),
            None,
        )
        item["argus_reviews"] = [
            r for r in all_reviews if r["rubric_name"] == "Environment & Grading Lint"]
        item["argus_main"] = classify_argus(argus)
    return found


def numeric_score_is_one(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return float(value) == 1.0
    except ValueError:
        return False


UUID_TEXT_GUARD = (
    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    "-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Chunk sizes are a statement-timeout budget, not a tuning knob. The read-only
# role runs with statement_timeout=120s, and a query that trips it fails the
# whole run -- so each query is sized to stay far below it whatever the sheet
# grows to.
MESSAGE_COUNT_CHUNK = 300
MESSAGE_EXPORT_CHUNK = 24
TASK_METADATA_CHUNK = 200


def chunked(values: list[Any], size: int) -> list[list[Any]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


def balanced_chunks(values: list[Any], jobs: int, cap: int) -> list[list[Any]]:
    """Chunks wide enough to keep every worker busy, never wider than `cap`.

    Batching and parallelism pull against each other: a fixed chunk size that
    amortises the round trip also collapses 24 rollouts into two chunks, so ten
    of twelve workers idle and the batched version loses to the unbatched one.
    Measured: 24 exports took 5.9 s at a fixed size of 12 and 5.0 s here.
    `cap` stays as the statement-timeout ceiling, not as the target.
    """
    if not values:
        return []
    size = max(1, -(-len(values) // max(1, jobs)))
    return chunked(values, min(cap, size))


def map_chunks(db: "HorizonDatabase", work: Any, chunks: list[Any],
               jobs: int) -> list[Any]:
    """Run chunks on the run's shared pool, or inline for a single chunk.

    Each worker thread opens its own database connection, and a connection
    costs ~560 ms. Handing one chunk to another thread throws away the caller's
    already-warm connection to pay for a new one, and a pool created per stage
    would pay for a whole new set at every stage.
    """
    if not chunks:
        return []
    if len(chunks) == 1 or jobs <= 1:
        return [work(chunk) for chunk in chunks]
    return list(db.pool(min(jobs, len(chunks))).map(work, chunks))


def rollout_message_counts(
    db: HorizonDatabase, rollout_ids: list[str], jobs: int = 12
) -> dict[str, tuple[int, int]]:
    """Assistant turns and tool calls per rollout, keyed by rollout id.

    This used to be a LEFT JOIN inside the rollout query. The planner cannot
    push a 156-million-row, 176 GB `messages` table through that join by index,
    so it hash-joined the whole table and the statement hit the 120 s timeout
    once the sheet passed roughly a hundred tasks -- the run did not get slower,
    it FAILED. Asking for the rollout ids first and then probing `messages` by
    those ids uses idx_messages_rollout_id_role and returns in seconds.

    `json_typeof`/`json_array_length` replace `jsonb_typeof((...)::jsonb)`:
    `content_json` is already `json`, so the cast was re-parsing and
    normalising every assistant message only to read one array's length.
    Verified to return identical counts.
    """
    if not rollout_ids:
        return {}
    chunks = balanced_chunks(sorted(set(rollout_ids)), jobs, MESSAGE_COUNT_CHUNK)

    def one(chunk: list[str]) -> list[dict[str, str]]:
        ids = ",".join(f"'{value}'::uuid" for value in chunk)
        return db.csv(
            f"""
            SELECT m.rollout_id::text AS rollout_id,
                   count(*) FILTER (WHERE m.role='assistant') AS assistant_turns,
                   coalesce(sum(
                     CASE WHEN m.role='assistant'
                           AND json_typeof(m.content_json->'tool_calls')='array'
                          THEN json_array_length(m.content_json->'tool_calls')
                          ELSE 0 END
                   ), 0) AS tool_calls
            FROM messages m
            WHERE m.rollout_id IN ({ids})
            GROUP BY m.rollout_id
            """
        )

    results = map_chunks(db, one, chunks, jobs)
    counts: dict[str, tuple[int, int]] = {}
    for rows in results:
        for row in rows:
            counts[row["rollout_id"]] = (
                int(row["assistant_turns"] or 0), int(row["tool_calls"] or 0))
    return counts


def load_rollouts(db: HorizonDatabase, task_ids: list[str],
                  jobs: int = 12) -> dict[str, dict[str, Any]]:
    ids = sql_ids(task_ids)
    rows = db.csv(
        f"""
        SELECT r.local_task_id::text AS task_id,
               r.id::text AS rollout_id,
               e.model,
               e.status::text AS evaluation_status,
               r.extracted_score::text AS extracted_score,
               r.created_at::text AS created_at
        FROM rollouts r
        JOIN evaluations e ON e.id = r.evaluation_id
        WHERE r.local_task_id ~ '{UUID_TEXT_GUARD}'
          AND r.local_task_id::uuid IN ({ids}) AND {MODEL_FILTER_SQL}
        """
    )
    counts = rollout_message_counts(db, [row["rollout_id"] for row in rows], jobs)
    grouped: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    for row in rows:
        # A rollout with no messages kept its row under the old LEFT JOIN, with
        # both counts at zero. Missing from `counts` means exactly that.
        turns, calls = counts.get(row["rollout_id"], (0, 0))
        row["assistant_turns"] = turns
        row["tool_calls"] = calls
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
        if median_candidates:
            representative = median_candidates[len(median_candidates) // 2]
            rollout_source = "Completed"
        elif long_enough:
            representative = max(
                long_enough,
                key=lambda row: (row["created_at"], row["rollout_id"]),
            )
            rollout_source = f"Fallback: {representative['evaluation_status'].lower()}"
        else:
            representative = None
            rollout_source = "Unavailable"
        per_model: dict[str, Any] = {}
        for label, prefix in MODEL_COLUMNS:
            subset = [row for row in completed if (row["model"] or "").startswith(prefix)]
            subset.sort(key=lambda row: (row["created_at"], row["rollout_id"]), reverse=True)
            six = subset[:6]
            per_model[label] = {
                "pass6": sum(numeric_score_is_one(row["extracted_score"]) for row in six),
                "denominator": len(six),
                "median_turns": (
                    sorted(row["assistant_turns"] for row in six)[len(six) // 2]
                    if six else None
                ),
            }
        # Turn depth over the same six rollouts pass@6 is computed from, so the
        # numbers in a row always describe one set of runs.
        six_turns = sorted(r["assistant_turns"] for r in latest)
        result[task_id] = {
            "turns_median": six_turns[len(six_turns) // 2] if six_turns else None,
            "turns_max": six_turns[-1] if six_turns else None,
            "rollouts_n": len(completed),
            "per_model": per_model,
            "pass6": sum(numeric_score_is_one(row["extracted_score"]) for row in latest),
            "pass6_denominator": len(latest),
            "eligible_rollouts": len(completed),
            "incomplete_rollouts": len(long_enough) - len(completed),
            "representative": representative,
            "rollout_source": rollout_source,
            "representative_created_at": (
                representative["created_at"] if representative else None
            ),
        }
    return result


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:80] or "task"


def write_trajectory(rollout_id: str, rows: list[dict[str, str]], task_dir: Path) -> None:
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


def export_messages(
    db: HorizonDatabase,
    rollout_id: str,
    task_dir: Path,
) -> None:
    export_message_batches(db, [(rollout_id, task_dir)], jobs=1)


def export_message_batches(
    db: HorizonDatabase,
    pairs: list[tuple[str, Path]],
    jobs: int = 12,
) -> None:
    """Export several rollouts per query instead of one query per rollout.

    A trajectory is ~0.5 MB and 60-200 messages, so a dozen fit in one round
    trip comfortably. Measured at 0.80 s per rollout one at a time versus
    0.25 s inside a batch of twenty -- the difference is the connection and
    round trip, not the rows.
    """
    # A list per rollout, not a single path: two rows can legitimately share a
    # representative rollout, and keying a plain dict on the rollout id would
    # silently export only the last of them.
    targets: dict[str, list[Path]] = {}
    for rollout_id, task_dir in pairs:
        targets.setdefault(str(uuid.UUID(rollout_id)), []).append(task_dir)
    chunks = balanced_chunks(sorted(targets), jobs, MESSAGE_EXPORT_CHUNK)

    def one(chunk: list[str]) -> None:
        ids = ",".join(f"'{value}'::uuid" for value in chunk)
        rows = db.csv(
            f"""
            SELECT rollout_id::text AS rollout_id,
                   sequence_number::text AS sequence_number,
                   role,
                   coalesce(content, '') AS content,
                   coalesce(content_json::text, '') AS content_json
            FROM messages
            WHERE rollout_id IN ({ids})
            ORDER BY rollout_id, sequence_number
            """
        )
        by_rollout: dict[str, list[dict[str, str]]] = {value: [] for value in chunk}
        for row in rows:
            by_rollout[row["rollout_id"]].append(row)
        for rollout_id, rollout_rows in by_rollout.items():
            for task_dir in targets[rollout_id]:
                write_trajectory(rollout_id, rollout_rows, task_dir)

    map_chunks(db, one, chunks, jobs)


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
    jobs: int = 12,
    label_concurrency: int = 16,
) -> dict[str, Path]:
    annotation_tool = tool_root / ".venv/bin/annotation-tool"
    labeler = pipeline_root / "label_rp.py"
    if not annotation_tool.is_file():
        raise SystemExit(f"Annotation tool not found: {annotation_tool}")
    if not labeler.is_file():
        raise SystemExit(f"R/P labeler not found: {labeler}")
    outputs = run_dir / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    def prepare_one(name: str) -> None:
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

    print(f"preparing {len(task_names)} task packets with {jobs} workers ...",
          file=sys.stderr, flush=True)
    with cf.ThreadPoolExecutor(jobs) as pool:
        list(pool.map(prepare_one, task_names))

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
                str(label_concurrency),
            ],
            capture=False,
        )
    finally:
        if remove_key and key_path.exists():
            key_path.unlink()

    print(f"merging {len(task_names)} annotations with {jobs} workers ...",
          file=sys.stderr, flush=True)
    with cf.ThreadPoolExecutor(jobs) as pool:
        list(pool.map(
            lambda name: merge_and_finalize(outputs / name, annotation_tool, tool_root),
            task_names))
    return {name: outputs / name / "annotated_trajectories.json" for name in task_names}


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


RP_FILE_PATTERN = (
    r"(?:/app/|/workspace/repo/)?"
    r"RESEARCH_AND_(?:PLANNING|IMPLEMENTATION)\.md"
)
RP_WRITE_PATTERNS = [
    re.compile(
        rf"(?:pathlib\.)?Path\s*\(\s*(['\"]){RP_FILE_PATTERN}\1\s*\)"
        r"\s*\.write_(?:text|bytes)\s*\(",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b([A-Za-z_]\w*)\s*=\s*(?:pathlib\.)?Path\s*\(\s*"
        rf"(['\"]){RP_FILE_PATTERN}\2\s*\)\s*;?\s*\1"
        r"\.write_(?:text|bytes)\s*\(",
        re.IGNORECASE,
    ),
    re.compile(
        rf"open\s*\(\s*(['\"]){RP_FILE_PATTERN}\1\s*,\s*"
        r"(?:mode\s*=\s*)?(['\"])[^'\"]*[wax][^'\"]*\2\s*\)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:^|[\s;|&])>>?\s*(['\"]?){RP_FILE_PATTERN}\1",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\btee(?:\s+-a)?\s+(['\"]?){RP_FILE_PATTERN}\1",
        re.IGNORECASE,
    ),
]


def is_rp_write_action(action: dict[str, Any]) -> bool:
    command = str((action.get("arguments") or {}).get("keystrokes", ""))
    if not any(pattern.search(command) for pattern in RP_WRITE_PATTERNS):
        return False
    observation = str(action.get("observation", ""))
    return "Previous response had parsing errors:" not in observation


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
        if is_rp_write_action(action):
            completion_steps.append(int(action.get("ordinal", 0)))
    return {
        "leading_rp": leading,
        "total_rp": sum(flags),
        "tool_calls": len(actions),
        "rp_complete_step": max(completion_steps) if completion_steps else None,
    }


def fit_for_pilot(item: dict[str, Any]) -> str:
    complete_step = item.get("rp_complete_step")
    rp_gate = (
        int(complete_step)
        if complete_step is not None
        else int(item.get("leading_rp", 0))
    )
    return (
        "YES"
        if item.get("ai_rubrics") == "Pass"
        and int(item.get("pass6", 99)) < 2
        and int(item.get("pass6_denominator", 0)) > 0
        and item.get("argus_main") == "Pass"
        and rp_gate > 20
        else "NO"
    )


# The R/P labels are the run's dominant cost -- one LLM call per 40 actions,
# 69 chunks for 11 tasks -- and they are a deterministic function of ONE
# rollout's trajectory. Keyed on rollout id, a re-run after adding five tasks
# labels five tasks. The cache stores the finalized annotation, not the derived
# metrics, so rp_metrics() and the R/P write patterns still re-evaluate from
# source on every run: a code change takes effect, a labelling call does not
# get repeated.
DEFAULT_RP_CACHE = Path.home() / ".cache" / "pilot-analysis" / "rp"


def rp_cache_path(cache_dir: Path, rollout_id: str) -> Path:
    return cache_dir / f"{str(uuid.UUID(rollout_id))}.json"


def seed_rp_cache_from_runs(runs_root: Path, cache_dir: Path) -> int:
    """Adopt annotations left behind by earlier --keep-run-files runs.

    A run directory names its packets by task, not by rollout, so the rollout id
    is read back out of the exported trajectory itself rather than inferred from
    the directory name -- two tasks can share a name, and a task can change
    which rollout is representative between runs.
    """
    if not runs_root.is_dir():
        return 0
    cache_dir.mkdir(parents=True, exist_ok=True)
    adopted = 0
    for run_dir in sorted(runs_root.iterdir()):
        tasks = run_dir / "tasks"
        outputs = run_dir / "outputs"
        if not tasks.is_dir() or not outputs.is_dir():
            continue
        for task_dir in sorted(tasks.iterdir()):
            trajectory = task_dir / "trajectory-001.jsonl"
            annotated = outputs / task_dir.name / "annotated_trajectories.json"
            if not trajectory.is_file() or not annotated.is_file():
                continue
            try:
                first = json.loads(trajectory.read_text(encoding="utf-8").split("\n", 1)[0])
                rollout_id = str(uuid.UUID(first["rollout_id"]))
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
            target = rp_cache_path(cache_dir, rollout_id)
            if target.exists():
                continue
            shutil.copyfile(annotated, target)
            adopted += 1
    return adopted


def analyse_tasks(
    db: HorizonDatabase,
    task_ids: list[str],
    run_dir: Path,
    pipeline_root: Path,
    tool_root: Path,
    jobs: int = 12,
    label_concurrency: int = 16,
    rp_cache: Path | None = None,
) -> list[dict[str, Any]]:
    metadata = load_task_metadata(db, task_ids, jobs=jobs)
    rollout_data = load_rollouts(db, task_ids, jobs=jobs)
    # Directory names are assigned serially -- collision handling depends on what
    # has been claimed so far -- but the exports themselves are independent.
    names: dict[str, str] = {}
    rollout_ids: dict[str, str] = {}
    for task_id in task_ids:
        representative = rollout_data[task_id]["representative"]
        if not representative:
            continue
        dirname = safe_name(metadata[task_id]["name"])
        if dirname in names.values():
            dirname = f"{dirname}__{task_id[:8]}"
        names[task_id] = dirname
        rollout_ids[dirname] = representative["rollout_id"]

    if rp_cache is not None:
        rp_cache.mkdir(parents=True, exist_ok=True)
    cached: dict[str, Path] = {}
    if rp_cache is not None:
        for dirname, rollout_id in rollout_ids.items():
            path = rp_cache_path(rp_cache, rollout_id)
            if path.is_file():
                cached[dirname] = path
    pending = [name for name in names.values() if name not in cached]
    if cached:
        print(f"reusing {len(cached)} cached R/P annotations; {len(pending)} to label",
              file=sys.stderr, flush=True)

    if pending:
        print(f"exporting {len(pending)} trajectories with {jobs} workers ...",
              file=sys.stderr, flush=True)
        export_message_batches(
            db,
            [(rollout_ids[name], run_dir / "tasks" / name) for name in pending],
            jobs=jobs,
        )

    paths = dict(cached)
    if pending:
        paths.update(prepare_and_label(
            pending, run_dir, pipeline_root, tool_root,
            jobs=jobs, label_concurrency=label_concurrency,
        ))
        if rp_cache is not None:
            for name in pending:
                try:
                    shutil.copyfile(paths[name], rp_cache_path(rp_cache, rollout_ids[name]))
                except OSError as exc:  # a cache miss is never a run failure
                    print(f"could not cache R/P annotation for {name}: {exc}",
                          file=sys.stderr, flush=True)
    result = []
    for task_id in task_ids:
        rp_data = (
            rp_metrics(paths[names[task_id]])
            if task_id in names
            else {
                "leading_rp": 0,
                "total_rp": 0,
                "tool_calls": 0,
                "rp_complete_step": None,
            }
        )
        item = {
            "task_id": task_id,
            **metadata[task_id],
            **{
                key: value
                for key, value in rollout_data[task_id].items()
                if key != "representative"
            },
            **rp_data,
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
.tablewrap {{ overflow-x:auto; border:1px solid var(--border); border-radius:10px; }}
.tablewrap table {{ margin:0; }}
.tablewrap th, .tablewrap td {{ white-space:nowrap; }}
.tablewrap th:first-child, .tablewrap td:first-child {{ position:sticky; left:0;
  background:var(--bg); z-index:2; min-width:230px; white-space:normal; }}
.tablewrap th:nth-child(2), .tablewrap td:nth-child(2) {{ position:sticky; left:230px;
  background:var(--bg); z-index:2; }}
.tablewrap td:nth-child(9) {{ max-width:150px; overflow:hidden; text-overflow:ellipsis; }}
.toolbar {{ display:flex; flex-wrap:wrap; align-items:flex-end; gap:10px 14px;
  padding:12px 14px; border:1px solid var(--border); border-radius:10px;
  background:var(--card); margin:14px 0 4px; }}
.filter {{ display:flex; flex-direction:column; gap:4px; min-width:0; }}
.filter > span {{ font-size:.74rem; letter-spacing:.02em; text-transform:uppercase;
  color:var(--muted); white-space:nowrap; }}
.filter select {{ min-width:132px; max-width:230px; }}
.toolbar .spacer {{ flex:1 1 auto; }}
.toolbar .actions {{ display:flex; align-items:center; gap:10px; }}
#shown {{ white-space:nowrap; font-variant-numeric:tabular-nums; }}
#reset {{ font:inherit; font-size:.85rem; color:inherit; background:var(--bg);
  border:1px solid var(--border); border-radius:7px; padding:7px 12px; cursor:pointer; }}
#reset:hover {{ border-color:var(--muted); }}
#reset[disabled] {{ opacity:.45; cursor:default; }}
select {{ font:inherit; color:inherit; background:var(--bg); border:1px solid var(--border); border-radius:7px; padding:6px 9px; }}
.table-wrap {{ overflow-x:clip; }}
table {{ width:100%; min-width:0; table-layout:fixed; border-collapse:collapse; font-size:clamp(12px,.95vw,15px); }}
th,td {{ padding:7px clamp(2px,.45vw,8px); border-bottom:1px solid var(--border); overflow-wrap:anywhere; vertical-align:middle; }}
th {{ color:var(--muted); font-weight:600; text-align:left; vertical-align:bottom; }}
th:nth-child(1),td:nth-child(1) {{ width:21%; }}
th:nth-child(2),td:nth-child(2) {{ width:8%; }}
th:nth-child(3),td:nth-child(3) {{ width:7%; }}
th:nth-child(4),td:nth-child(4) {{ width:9%; }}
th:nth-child(5),td:nth-child(5) {{ width:8%; }}
th:nth-child(6),td:nth-child(6) {{ width:10%; }}
th:nth-child(7),td:nth-child(7) {{ width:6%; }}
th:nth-child(8),td:nth-child(8) {{ width:7%; }}
th:nth-child(9),td:nth-child(9) {{ width:6%; }}
th:nth-child(10),td:nth-child(10) {{ width:6%; }}
th:nth-child(11),td:nth-child(11) {{ width:12%; }}
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
  <div class="toolbar">
    <label class="filter" for="fit-filter"><span>Fit for pilot</span><select id="fit-filter"><option value="all">Any</option><option value="YES">YES</option><option value="NO">NO</option></select></label>
    <label class="filter" for="shape-filter"><span>Shape</span><select id="shape-filter"><option value="all">All shapes</option><option>Diagnosis</option><option>Migration</option><option>Optimization</option></select></label>
    <label class="filter" for="rubric-filter"><span>AI rubrics</span><select id="rubric-filter"><option value="all">Any</option><option value="Pass">Pass</option><option value="Fail">Fail</option><option value="None">No reviews</option></select></label>
    <label class="filter" for="argus-filter"><span>Argus Main</span><select id="argus-filter"><option value="all">Any</option><option value="Pass">Pass</option><option value="Fail">Fail</option><option value="In Progress">In progress</option><option value="Missing">Missing</option></select></label>
    <label class="filter" for="glm-filter"><span>GLM</span><select id="glm-filter"><option value="all">Any</option><option value="fails">Fails (&lt;2)</option><option value="solves">Solves (&ge;2)</option><option value="none">No rollouts</option></select></label>
    <label class="filter" for="router-filter"><span>Router</span><select id="router-filter"><option value="all">Any</option><option value="fails">Fails (&lt;2)</option><option value="solves">Solves (&ge;2)</option><option value="none">No rollouts</option></select></label>
    <label class="filter" for="starfall-filter"><span>Starfall</span><select id="starfall-filter"><option value="all">Any</option><option value="fails">Fails (&lt;2)</option><option value="solves">Solves (&ge;2)</option><option value="none">No rollouts</option></select></label>
    <label class="filter" for="repo-filter"><span>Repository</span><select id="repo-filter"><option value="all">All repositories</option></select></label>
    <label class="filter" for="lang-filter"><span>Language</span><select id="lang-filter"><option value="all">All languages</option></select></label>
    <span class="spacer"></span>
    <span class="actions"><span id="shown" class="muted small"></span><button id="reset" type="button">Reset filters</button></span>
  </div>
  <div class="table-wrap"><div class="tablewrap"><table>
    <thead><tr><th>Task</th><th class="center">Fit</th><th>Shape</th><th class="center">AI rubrics</th><th class="center" title="cipher-omni">GLM Pass@6</th><th class="center" title="router-16a8dce2a6e7">Router Pass@6</th><th class="center" title="starfall">Starfall Pass@6</th><th>Language</th><th>Repository</th><th class="center">Argus Main</th><th class="center">R/P rollout</th><th class="right">Leading R/P</th><th class="center" title="Final successful RESEARCH_AND_PLANNING.md write step">R/P complete step</th><th class="right">Total R/P</th><th class="right">Tool calls</th></tr></thead>
    <tbody id="rows"></tbody>
  </table></div></div>
  <footer>
    <p>Fit requires AI rubrics Pass, fewer than 2 passes among available completed eligible Gemini 3.7 rollouts, Argus Main Pass, and an R/P complete step greater than 20. If no completion step is available, more than 20 leading R/P calls satisfies the R/P gate. A denominator from 1 to 6 is valid.</p>
    <p>Pass@6 uses up to the latest six completed Starfall or router-16a8dce2a6e7 rollouts with more than 10 reconstructed assistant turns. Only a score of exactly 1 counts as Pass.</p>
    <p>R/P analysis uses the median completed eligible rollout. If none exists, it uses the most recent failed or cancelled rollout with more than 10 assistant turns and marks it as a fallback.</p>
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
const fitFilter=document.querySelector('#fit-filter');
const rubricFilter=document.querySelector('#rubric-filter');
const argusFilter=document.querySelector('#argus-filter');
const glmFilter=document.querySelector('#glm-filter');
const routerFilter=document.querySelector('#router-filter');
const starfallFilter=document.querySelector('#starfall-filter');
const repoFilter=document.querySelector('#repo-filter');
const langFilter=document.querySelector('#lang-filter');
const shown=document.querySelector('#shown');
// Repository and language options are built from the data, so the dropdown can
// never offer a value no row has.
for(const [sel,key] of [[repoFilter,'repo_key'],[langFilter,'lang_key']]){{
  [...new Set(DATA.map(r=>r[key]).filter(Boolean))].sort().forEach(v=>{{
    const o=document.createElement('option'); o.value=v; o.textContent=v; sel.appendChild(o);
  }});
}}
function render(){{
  const modelOk=(row,key,want)=>{{
    if(want==='all') return true;
    const m=(row.per_model||{{}})[key];
    const n=m&&m.denominator?m.denominator:0;
    if(want==='none') return n===0;
    if(n===0) return false;                       // no rollouts is not a pass or a fail
    return want==='fails' ? m.pass6<2 : m.pass6>=2;
  }};
  // Every active control is ANDed: "router fails AND rubrics pass AND repo=x".
  const visible=ordered.filter(row=>
       (fitFilter.value==='all'||row.fit===fitFilter.value)
    && (filter.value==='all'||row.shape===filter.value)
    && (rubricFilter.value==='all'||row.ai_rubrics===rubricFilter.value)
    && (argusFilter.value==='all'||row.argus_main===argusFilter.value)
    && modelOk(row,'glm',glmFilter.value)
    && modelOk(row,'router',routerFilter.value)
    && modelOk(row,'starfall',starfallFilter.value)
    && (repoFilter.value==='all'||(row.repo_key||'')===repoFilter.value)
    && (langFilter.value==='all'||(row.lang_key||'')===langFilter.value));
  const active=allFilters.filter(sel=>sel.value!=='all').length;
  shown.textContent=active
    ? `${{visible.length}} of ${{ordered.length}} shown · ${{active}} filter${{active>1?'s':''}}`
    : `${{ordered.length}} tasks`;
  resetBtn.disabled=active===0;
  tbody.innerHTML=visible.map(row=>`<tr>
    <td><span class="task-name">${{esc(row.name)}}</span><a class="task-id" href="https://horizon.bespokelabs.ai/tasks/${{encodeURIComponent(row.task_id)}}" target="_blank" rel="noopener noreferrer">${{esc(row.task_id)}}</a>${{row.rubrics_source||row.rollouts_source?`<span class="muted small"> rubrics:${{esc(row.rubrics_source||'none')}} · rollouts:${{esc(row.rollouts_source||'none')}}</span>`:''}}</td>
    <td class="center"><span class="${{row.fit==='YES'?'fit-yes':'fit-no'}}">${{row.fit}}</span></td>
    <td>${{esc(row.shape)}}</td>
    <td class="center"><span class="${{statusClass(row.ai_rubrics)}}" title="${{row.ai_count}} applicable reviews">${{esc(row.ai_rubrics)}}</span></td>
    ${{['glm','router','starfall'].map(k=>{{const m=(row.per_model||{{}})[k];
      if(!m||!m.denominator) return '<td class="center"><span class="muted">—</span></td>';
      return `<td class="center"><span class="${{m.pass6<2?'pass6-good':'pass6-bad'}}" title="latest ${{m.denominator}} eligible rollouts, median ${{m.median_turns??'?'}} assistant turns">${{m.pass6}}/${{m.denominator}}</span>${{m.median_turns!=null?` <span class="muted small">${{m.median_turns}}t</span>`:''}}</td>`;}}).join('')}}
    <td>${{esc(row.lang_key||'—')}}</td>
    <td class="small" title="${{esc(row.repo_key||'')}}">${{esc((row.repo_key||'—').replace(/^github\.com\//,''))}}</td>
    <td class="center"><span class="${{statusClass(row.argus_main)}}">${{esc(row.argus_main)}}</span></td>
    <td class="center"><span class="${{row.rollout_source?.startsWith('Fallback:')?'progress':''}}" title="${{esc(row.representative_created_at??'No representative rollout')}}">${{esc(row.rollout_source??'Completed')}}</span></td>
    <td class="right">${{row.leading_rp}}</td>
    <td class="center">${{row.rp_complete_step??'—'}}</td>
    <td class="right">${{row.total_rp}}</td>
    <td class="right">${{row.tool_calls}}</td>
  </tr>`).join('');
  parent.postMessage({{type:'pilot-report-height',height:document.documentElement.scrollHeight}},'*');
}}
const allFilters=[fitFilter,filter,rubricFilter,argusFilter,glmFilter,routerFilter,starfallFilter,repoFilter,langFilter];
const resetBtn=document.querySelector('#reset');
for(const sel of allFilters) sel.addEventListener('change',render);
resetBtn.addEventListener('click',()=>{{ for(const sel of allFilters) sel.value='all'; render(); }});
render();
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
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; connect-src 'none'; frame-src 'self'; child-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>Pilot task analysis</title>
<style>html,body{{margin:0;background:#fff}}iframe{{display:block;width:100%;height:100vh;border:0}}</style>
</head>
<body>
<iframe id="report" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" scrolling="no" referrerpolicy="no-referrer" title="Pilot task analysis" data-srcdoc="{encoded}"></iframe>
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


SIGNAL_KEYS = {
    "rubrics": ("ai_rubrics", "ai_rubrics_strict", "grader_coverage",
                "ai_reviews", "argus_reviews", "ai_count", "argus_main", "shape"),
    "rollouts": ("pass6", "pass6_denominator", "eligible_rollouts", "per_model",
                 "rollout_source", "representative_created_at", "turns_median", "turns_max", "rollouts_n",
                 "leading_rp", "total_rp", "tool_calls", "rp_complete_step"),
}


def _has_rubrics(row: dict[str, Any]) -> bool:
    return bool(row.get("ai_reviews")) or row.get("argus_main") not in (None, "", "Missing")


def _has_rollouts(row: dict[str, Any]) -> bool:
    return int(row.get("pass6_denominator") or 0) > 0


def collapse_variants(rows: list[dict[str, Any]],
                      groups: list[dict[str, Any]],
                      enrich: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per logical task, with each SIGNAL taken from the first variant
    that actually carries it (final -> binary -> partial).

    Rubrics and rollouts are resolved INDEPENDENTLY: a task routinely has its
    rubrics on one variant and its rollouts on another, because the partial
    variant is the one quality gating was run against. Every borrowed signal
    records the variant it came from, so a verdict can be audited back to the
    exact task id that supplied its evidence -- a merged row whose provenance is
    invisible is not reviewable.
    """
    by_id = {row["task_id"]: row for row in rows}
    collapsed: list[dict[str, Any]] = []
    for group in groups:
        present = [(name, group[name]) for name in VARIANT_ORDER
                   if group.get(name) and group[name] in by_id]
        if not present:
            continue
        base_name, base_id = present[0]
        merged: dict[str, Any] = dict(by_id[base_id])
        merged["variants"] = {name: tid for name, tid in present}
        merged["responsible"] = group.get("responsible", "")

        # TWO DIFFERENT THINGS ARE CALLED "VERSION"; only one of them inherits.
        #   Horizon task_version  -> ALWAYS take the LATEST. An older Horizon
        #                            version passing does not excuse the current one.
        #   CSV column            -> final / binary / partial. THESE inherit: the
        #                            same rubric is re-fired against each shipped
        #                            variant, so a pass on any of the three settles
        #                            it and you stop looking.
        # Taking rubrics wholesale from the first variant that has ANY rows reads
        # a fail on the copy while the source has passed the same rubric ten times.
        # TWO RULES, IN ORDER.
        #  1. WITHIN a variant only the LATEST run of a rubric counts -- a re-fired
        #     rubric has a current answer and older runs are history. Retired
        #     rubric sets leave stale rows behind (one task carries 31 rubrics,
        #     19 of them historical).
        #  2. ACROSS variants, in CSV order final -> binary -> partial, the first
        #     variant whose LATEST run passes settles that rubric and you stop.
        #     If no variant's latest run passes, the most recent failing run is
        #     what gets reported.
        def latest_per_name(reviews):
            out: dict[str, dict[str, Any]] = {}
            for review in reviews or []:
                name = review.get("rubric_name")
                if not name:
                    continue
                held = out.get(name)
                if held is None or str(review.get("created_at") or "") > str(
                        held.get("created_at") or ""):
                    out[name] = review
            return out

        per_variant = [(vname, latest_per_name(by_id[tid].get("ai_reviews")))
                       for vname, tid in present]
        settled: dict[str, dict[str, Any]] = {}
        settled_by: dict[str, str] = {}
        for rname in {n for _, latest in per_variant for n in latest}:
            fallback = None
            for vname, latest in per_variant:
                review = latest.get(rname)
                if review is None:
                    continue
                if ai_review_passes(review, ignore_grader_coverage=False):
                    settled[rname] = review
                    settled_by[rname] = vname
                    break
                if fallback is None or str(review.get("created_at") or "") > str(
                        fallback.get("created_at") or ""):
                    fallback = review
            if rname not in settled and fallback is not None:
                settled[rname] = fallback
                settled_by[rname] = "none-passed"
        merged["ai_reviews_merged"] = list(settled.values())
        merged["rubric_settled_by"] = settled_by

        for signal, keys in SIGNAL_KEYS.items():
            probe = _has_rubrics if signal == "rubrics" else _has_rollouts
            source = next((n for n, t in present if probe(by_id[t])), None)
            merged[f"{signal}_source"] = source
            if source:
                donor = by_id[group[source]]
                for key in keys:
                    if key in donor:
                        merged[key] = donor[key]

        argus_pick = argus_from = None
        for vname, tid in present:
            runs = by_id[tid].get("argus_reviews") or []
            if not runs:
                continue
            newest = max(runs, key=lambda r: str(r.get("created_at") or ""))
            if argus_pick is None:
                argus_pick, argus_from = newest, vname
            if classify_argus(newest) == "Pass":
                argus_pick, argus_from = newest, vname
                break
        if argus_pick is not None:
            merged["argus_main"] = classify_argus(argus_pick)
            merged["argus_settled_by"] = argus_from

        # Recompute the verdict from the merged, pass-inherited set.
        merged_reviews = merged.get("ai_reviews_merged") or []
        strict = merged.get("rubrics_source") == "partial"
        if not merged_reviews:
            merged["ai_rubrics"] = "None"
        else:
            merged["ai_rubrics"] = (
                "Pass"
                if all(ai_review_passes(r, ignore_grader_coverage=not strict)
                       for r in merged_reviews)
                else "Fail")
        merged["ai_count"] = len(merged_reviews)
        merged["failing_rubrics"] = sorted(
            r.get("rubric_name", "?") for r in merged_reviews
            if not ai_review_passes(r, ignore_grader_coverage=not strict))
        merged["rubric_variants_merged"] = [name for name, _ in present]
        facts = next((enrich[t] for _, t in present
                      if t in enrich and enrich[t].get("repo_key")), None)
        merged["repo_key"] = (facts or {}).get("repo_key", "")
        merged["lang_key"] = (facts or {}).get("lang_key", "")
        merged["base_commit"] = (facts or {}).get("base_commit", "")
        merged["fit"] = fit_for_pilot(merged)
        collapsed.append(merged)
    return collapsed


def diversity_summary(rows: list[dict[str, Any]], max_per_repo: int,
                      max_lang_share: float) -> dict[str, Any]:
    """Pool-level gates over the FIT rows only.

    An unknown repository is not a repository: bucketing every unreadable task
    under one key makes them collide and breaches a cap that was never actually
    evaluated. Unknowns are counted separately and reported as unchecked.
    """
    fit = [row for row in rows if row.get("fit") == "YES"]

    # SAME REPO + SAME COMMIT is a stronger duplication signal than repo alone:
    # two tasks cut from one artifact share their whole source tree. Commits are
    # compared by PREFIX because the pool stores the same hash at different
    # lengths (40b0ab56da3682c2... on one task, 40b0ab56 on another), so string
    # equality silently calls one artifact two.
    shared_artifact: list[dict[str, Any]] = []
    by_repo: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in fit:
        if row.get("repo_key") and row.get("base_commit"):
            by_repo[row["repo_key"]].append(row)
    for repo, members in by_repo.items():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                left = members[i].get("base_commit") or ""
                right = members[j].get("base_commit") or ""
                shortest = min(left, right, key=len)
                if shortest and left.startswith(shortest) and right.startswith(shortest):
                    shared_artifact.append({
                        "repo": repo,
                        "commit": shortest,
                        "tasks": [members[i].get("name"), members[j].get("name")],
                        "match": "exact" if left == right else "prefix",
                    })

    repos = collections.Counter(row.get("repo_key") or "(unknown)" for row in fit)
    langs = collections.Counter(row.get("lang_key") or "(unknown)" for row in fit)
    total = len(fit)
    return {
        "fit_count": total,
        "repos": dict(repos),
        "languages": dict(langs),
        "repo_breaches": {r: n for r, n in repos.items()
                          if r != "(unknown)" and n > max_per_repo},
        "language_breaches": {l: n for l, n in langs.items()
                              if l != "(unknown)" and total and n / total > max_lang_share},
        "shared_artifact_pairs": shared_artifact,
        "unchecked_repo": repos.get("(unknown)", 0),
        "max_per_repo": max_per_repo,
        "max_lang_share": max_lang_share,
    }


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

    rp_cache = None if args.no_rp_cache else args.rp_cache.expanduser()
    if args.seed_rp_cache and rp_cache is not None:
        adopted = seed_rp_cache_from_runs(
            Path(__file__).resolve().parent / "runs", rp_cache)
        print(f"seeded {adopted} R/P annotations into {rp_cache}",
              file=sys.stderr, flush=True)

    stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    run_dir = args.run_dir or Path(__file__).resolve().parent / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    try:
        with HorizonDatabase(args.port) as db:
            new_rows = analyse_tasks(
                db,
                task_ids,
                run_dir,
                args.pipeline_root.expanduser(),
                args.tool_root.expanduser(),
                jobs=args.jobs,
                label_concurrency=args.label_concurrency,
                rp_cache=rp_cache,
            )
        enrich: dict[str, dict[str, Any]] = {}
        if getattr(args, "enrich", None) and args.enrich.is_file():
            enrich = {e["task_id"]: e for e in
                      json.loads(args.enrich.read_text(encoding="utf-8")).get("tasks", [])}
        groups = getattr(args, "variant_groups", None)
        if groups:
            new_rows = collapse_variants(new_rows, groups, enrich)
            args.diversity = diversity_summary(
                new_rows, args.max_per_repo, args.max_lang_share)
        by_id = {row["task_id"]: row for row in existing}
        for row in new_rows:
            by_id[row["task_id"]] = row
        write_outputs(list(by_id.values()), args.output)
    except Exception:
        print(f"Run files kept for debugging: {run_dir}", file=sys.stderr)
        raise
    else:
        print(f"analysed {len(task_ids)} tasks in {time.perf_counter() - started:.1f} s",
              file=sys.stderr, flush=True)
        if not args.keep_run_files:
            shutil.rmtree(run_dir)


if __name__ == "__main__":
    main()
