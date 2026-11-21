#!/usr/bin/env bash
# Local dev: a throwaway Postgres in docker + the service, configured by local.env (Kubernetes evidence, email and
# sign-in off). Triage needs a reachable gpu-scheduler (local.env's GPU_SCHEDULER_URL, and GPU_SCHEDULER_TOKEN exported).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p .pids
if ! docker ps --format '{{.Names}}' | grep -qx io-pg; then
  docker rm -f io-pg >/dev/null 2>&1 || true
  docker run -d --name io-pg -e POSTGRES_PASSWORD=dev -p 127.0.0.1:55444:5432 postgres:17 >/dev/null
  until docker exec io-pg pg_isready -U postgres -q; do sleep 1; done
fi
export PGPASSWORD=dev                     # libpq reads it, so the URL carries no password
export GPU_SCHEDULER_TOKEN="${GPU_SCHEDULER_TOKEN-}"     # yours if exported, else none
set -a; . ./local.env; set +a             # every setting the service reads (it has no defaults)
nohup .venv/bin/python -m incident_operator > .pids/io.log 2>&1 &
echo $! > .pids/io.pid
echo "UI on http://127.0.0.1:$UI_PORT, webhook on :$WEBHOOK_PORT (log: .pids/io.log)"
