#!/usr/bin/env python3
"""Attach repo + language facts to tasks, for the pilot diversity gates.

WHY THIS IS NOT A DB QUERY: the Horizon Postgres `task` table carries no
repository or language column. Both live in the task's own `task.toml`, which
is only reachable through the API's download URLs. So this stage uses
HORIZON_API_KEY and needs no gcloud, no IAP tunnel, no DB secret.

THREE SCHEMAS, and reading one as another silently corrupts the language mix:

  diagnosis     metadata.language = "python"   metadata.repository_url present
  migration     metadata.language = "go"       <- the PORT TARGET, not the source.
                The source is a Rust crate under environment/rust_reference/ and
                there is no repository_url at all.
  optimization  no language field whatsoever   <- it is inside metadata.tags
                and again no repository_url.

So language is resolved from three sources, the winning source is recorded, and
an independent `ext_lang` is derived from the shipped source-file extensions as
a cross-check that owes nothing to the metadata.

    python3 diversity_enrich.py --task-file ids.txt --out enrich.json
"""
from __future__ import annotations
import argparse, collections, hashlib, json, os, re, sys, tomllib, urllib.request
import concurrent.futures as cf

EXT_LANG = {".py":"python",".go":"go",".rs":"rust",".ts":"typescript",".js":"javascript",
            ".java":"java",".rb":"ruby",".c":"c",".cpp":"cpp",".cc":"cpp",".cs":"csharp"}
ALIAS = {"rs":"rust","py":"python","python3":"python","js":"javascript","ts":"typescript",
         "golang":"go","c++":"cpp","cxx":"cpp","c#":"csharp"}
KNOWN = set(EXT_LANG.values())
BIG = (".tar.gz", ".tgz", ".zip", ".tar")
# task scaffolding is never the subject language of the task itself
NOISE = re.compile(r"(^|/)(tests?|solution|environment/port_skeleton|\.horizon)/")


def norm_lang(v: str) -> str:
    s = (v or "").strip().strip("\"'").lower()
    return ALIAS.get(s, s)


def norm_repo(u: str) -> str:
    if not u:
        return ""
    s = re.sub(r"^git\+", "", u.strip().strip("\"'"))
    s = re.sub(r"^[a-z]+://", "", s, flags=re.I)
    s = re.sub(r"^git@([^:]+):", r"\1/", s)
    s = s.split("#")[0].split("?")[0]
    return re.sub(r"\.git$", "", s.rstrip("/")).lower()


def derive_repo(meta_task_id: str, name: str) -> tuple[str, bool]:
    """Migration/optimization tasks ship no repository_url. Recover identity from
    the id, and ALWAYS mark it derived so an inference is never read as data."""
    for cand in (meta_task_id or "", name or ""):
        m = re.match(r"^tests-\w+-(.+)$", cand)          # tests-go-JelteF__derive_more
        if m:
            return m.group(1).replace("__", "/").lower(), True
        m = re.match(r"^(.+?)-task(-v\d+)?$", cand)       # protocompile-task-v2
        if m:
            return m.group(1).lower(), True
    return "", False


def enrich_one(client, task_id: str) -> dict:
    row = {"task_id": task_id, "problems": []}
    try:
        task = client.tasks.get(task_id)
        row["name"] = task.name
        row["version"] = task.current_version
    except Exception as exc:
        row["problems"].append(f"tasks.get: {type(exc).__name__}")
        return row
    try:
        files = {f.path: f.url for f in client.tasks.download_urls(task_id).files}
    except Exception as exc:
        row["problems"].append(f"download_urls: {type(exc).__name__}")
        return row

    meta: dict = {}
    path = next((p for p in ("task.toml", "task.yaml", "task.yml") if p in files), None)
    if path is None:
        row["problems"].append("no task.toml")
    elif path.endswith(".toml"):
        try:
            blob = urllib.request.urlopen(files[path], timeout=90).read()
            doc = tomllib.loads(blob.decode("utf-8", "replace"))
            meta = {**(doc.get("metadata") or {}), **(doc.get("task") or {})}
        except Exception as exc:
            row["problems"].append(f"toml parse: {type(exc).__name__}")
    else:
        row["problems"].append("yaml metadata not parsed")

    lang, source = norm_lang(meta.get("language")), "metadata.language"
    if not lang:
        tags = [norm_lang(t) for t in (meta.get("tags") or [])]
        hit = next((t for t in tags if t in KNOWN), "")
        if hit:
            lang, source = hit, "metadata.tags"
    if not lang:
        source = "none"
        row["problems"].append("no declared language")

    ext = collections.Counter()
    for p in files:
        if NOISE.search(p):
            continue
        e = os.path.splitext(p)[1]
        if e in EXT_LANG:
            ext[EXT_LANG[e]] += 1
    row["ext_lang"] = ext.most_common(1)[0][0] if ext else ""
    row["declared_lang"] = lang
    row["lang_source"] = source
    row["category"] = meta.get("category", "")

    is_port = "migration" in str(row["category"]).lower() or str(row.get("name","")).startswith("tests-")
    row["is_port"] = is_port
    # A port task exercises its SOURCE language: that is the code the model must
    # read and reason about. Flip with --migration-lang target if the client
    # cares about the language being written instead.
    row["lang_key"] = (row["ext_lang"] or lang) if is_port else (lang or row["ext_lang"])

    repo = norm_repo(meta.get("repository_url"))
    row["repo_derived"] = False
    if not repo:
        repo, row["repo_derived"] = derive_repo(meta.get("task_id"), row.get("name", ""))
        row["problems"].append(
            "repository_url absent; repo identity DERIVED from task id" if repo
            else "no repository_url and none derivable")
    row["repo_key"] = repo
    row["base_commit"] = meta.get("base_commit_hash", "")

    # content fingerprint -> exact-duplicate detection across differently named rows
    parts, unread = [], 0
    archive_sha = meta.get("faulted_archive_sha256", "")
    for p in sorted(files):
        if p.endswith(BIG) and archive_sha:
            parts.append(f"{p}\0{archive_sha}")
            continue
        try:
            data = urllib.request.urlopen(files[p], timeout=180).read()
            parts.append(f"{p}\0{hashlib.sha256(data).hexdigest()}")
        except Exception:
            unread += 1
    if unread:
        row["problems"].append(f"{unread} file(s) unreadable; fingerprint PARTIAL")
    row["fingerprint_partial"] = bool(unread)
    row["fingerprint"] = hashlib.sha256("\n".join(parts).encode()).hexdigest() if parts else ""
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-file")
    ap.add_argument("--task-ids", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    ids: list[str] = []
    if args.task_file:
        for line in open(args.task_file):
            s = line.split("#", 1)[0].strip()
            if s:
                ids.append(re.sub(r"^.*/", "", s))
    ids += [x.strip() for x in args.task_ids.split(",") if x.strip()]
    ids = list(dict.fromkeys(ids))
    if not ids:
        print("no task ids given", file=sys.stderr)
        return 2

    from horizon.client import HorizonClient
    client = HorizonClient(api_key=os.environ["HORIZON_API_KEY"])
    print(f"enriching {len(ids)} tasks ...", file=sys.stderr)
    with cf.ThreadPoolExecutor(args.workers) as ex:
        rows = list(ex.map(lambda t: enrich_one(client, t), ids))
    json.dump({"tasks": rows}, open(args.out, "w"), indent=2)
    bad = sum(1 for r in rows if r["problems"])
    print(f"wrote {len(rows)} rows -> {args.out}   ({bad} with problems)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
