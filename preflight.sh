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
# TWO SHELL DIFFERENCES BITE HERE, and this file is SOURCED into whichever shell
# the user has -- zsh is the macOS default, bash is common, so it must work in both.
#   1. zsh ABORTS an entire `for` list on an unmatched glob ("no matches found"),
#      so the loop never runs and $PY is silently left empty. `find` does its own
#      matching and never hands an unmatched pattern to the shell.
#   2. zsh does NOT word-split an unquoted variable, so `for c in $candidates`
#      iterates ONCE over the whole string. Reading newline-delimited lines
#      behaves identically in sh, bash and zsh.
# A venv's `python` is also a symlink, so `-type f` would skip it.
_cands=$(
  for _p in python3.13 python3.12 python3.11; do command -v "$_p" 2>/dev/null; done
  _root=$(git rev-parse --show-toplevel 2>/dev/null) && printf '%s\n' "$_root/.venv/bin/python"
  find "$HOME" -maxdepth 6 -path '*/.venv/bin/python' 2>/dev/null | head -40
)
PY=""
while IFS= read -r c; do
  [ -n "$c" ] || continue
  # test the CAPABILITY, not just the import: version_materiality.py needs
  # download_urls(version=...), which older horizon clients do not have.
  if [ -x "$c" ] && "$c" -c "import tomllib, inspect
from horizon.client import HorizonClient
assert 'version' in inspect.signature(HorizonClient(api_key='x').tasks.download_urls).parameters" 2>/dev/null
  then PY="$c"; break; fi
done <<EOF
$_cands
EOF
unset _cands
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
  # same two shell constraints as above
  _envs=$(printf '%s\n' .env ../.env; find "$HOME" -maxdepth 4 -name .env 2>/dev/null | head -10)
  while IFS= read -r env; do
    [ -n "$env" ] || continue
    if [ -f "$env" ] && grep -q '^HORIZON_API_KEY=.' "$env" 2>/dev/null; then
      set -a; . "$env"; set +a; say "ok" "api key from $env"; break
    fi
  done <<EOF
$_envs
EOF
  unset _envs
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
