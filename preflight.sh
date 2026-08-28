#!/usr/bin/env bash
# Check every precondition and say exactly what to do about each failure.
# Sourced by run_pilot.sh; also runnable on its own: ./preflight.sh
# Exports PY, and the DB role/secret. Returns non-zero if anything is missing.
set -uo pipefail
ok=1
say() { printf '  %-6s %s\n' "$1" "$2"; }
fail() { say "MISSING" "$1"; printf '         %s\n' "$2"; ok=0; }

echo "Preflight"

# --- interpreter: python >= 3.11 WITH the horizon package -------------------
# The macOS system python3 is 3.9 and has no tomllib, so diversity_enrich.py
# dies on its import line. Discover rather than assume a path.
PY=""
for c in $(command -v python3.13 python3.12 python3.11 2>/dev/null) \
         "$(git rev-parse --show-toplevel 2>/dev/null)/.venv/bin/python" \
         "$HOME"/*/horizon/.venv/bin/python "$HOME"/*/*/horizon/.venv/bin/python; do
  [ -x "$c" ] && "$c" -c "import horizon, tomllib" 2>/dev/null && { PY="$c"; break; }
done
if [ -n "$PY" ]; then say "ok" "python  $PY ($("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'))"
else fail "python >= 3.11 with the 'horizon' package" \
          "install the horizon client into a venv, e.g. 'uv pip install horizon' in your horizon checkout"; fi
export PY

# --- psql: optional psycopg means EVERY query falls back to psql ------------
if ! command -v psql >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1 && [ -x "$(brew --prefix libpq 2>/dev/null)/bin/psql" ]; then
    export PATH="$(brew --prefix libpq)/bin:$PATH"
    say "ok" "psql    $(command -v psql)  (added keg-only libpq to PATH)"
  else
    fail "psql on PATH" "brew install libpq && export PATH=\"\$(brew --prefix libpq)/bin:\$PATH\""
  fi
else say "ok" "psql    $(command -v psql)"; fi

# --- the proxy: port 15433, NOT 5432 ---------------------------------------
if nc -z 127.0.0.1 15433 2>/dev/null; then
  say "ok" "proxy   listening on 15433"
else
  fail "cloud-sql-proxy on port 15433" \
       "run in ANOTHER terminal (it is long-lived and belongs to you, not this script):
           cloud-sql-proxy --private-ip --port 15433 apex-485220:us-central1:horizon-db
         Use that ADC form. A --token proxy keeps listening after its token expires and
         every later query then fails as 'server closed the connection unexpectedly',
         which reads exactly like a database outage."
fi

# --- the API key, needed twice under two names ------------------------------
if [ -z "${HORIZON_API_KEY:-}" ]; then
  for env in .env ../.env "$HOME"/*/voyager-diagnosis-pipeline/.env; do
    [ -f "$env" ] && grep -q '^HORIZON_API_KEY=.' "$env" 2>/dev/null && {
      set -a; . "$env"; set +a; say "ok" "api key from $env"; break; }
  done
fi
if [ -n "${HORIZON_API_KEY:-}" ]; then
  say "ok" "api key set (${#HORIZON_API_KEY} chars)"
  # the measurement pass needs the SAME key again as the R/P worker key; it does
  # not mint one and exits if it is absent.
  export HORIZON_WORKER_KEY="${HORIZON_WORKER_KEY:-$HORIZON_API_KEY}"
else
  fail "HORIZON_API_KEY" "export HORIZON_API_KEY=...  (or put it in a .env beside this repo)"
fi

# --- gcloud, for the read-only database password ----------------------------
if command -v gcloud >/dev/null 2>&1 && gcloud auth print-access-token >/dev/null 2>&1; then
  say "ok" "gcloud  authenticated"
else
  fail "gcloud login" "gcloud auth login && gcloud auth application-default login"
fi

export HORIZON_DB_ROLE="${HORIZON_DB_ROLE:-horizon_claude_ro}"
export HORIZON_DB_SECRET="${HORIZON_DB_SECRET:-horizon-claude-ro-password}"

if [ "$ok" = 1 ]; then echo "  all preconditions met"; else echo "  -> fix the above, then re-run"; fi
return $((1-ok)) 2>/dev/null || exit $((1-ok))
