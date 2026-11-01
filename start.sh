#!/usr/bin/env bash
# Local dev: a throwaway Postgres in docker + the service with Kubernetes evidence off and email off.
# UI on http://127.0.0.1:${UI_PORT:-8090}, webhook on :${WEBHOOK_PORT:-8091}. Triage needs a reachable gpu-scheduler
# (GPU_SCHEDULER_URL, GPU_SCHEDULER_TOKEN); without one, incidents stay `new` and are retried every few seconds.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p .pids
if ! docker ps --format '{{.Names}}' | grep -qx io-pg; then
  docker rm -f io-pg >/dev/null 2>&1 || true
  docker run -d --name io-pg -e POSTGRES_PASSWORD=dev -p 127.0.0.1:55444:5432 postgres:17 >/dev/null
  until docker exec io-pg pg_isready -U postgres -q; do sleep 1; done
fi
export PGPASSWORD=dev                     # libpq reads it, so the URL carries no password
export DATABASE_URL="postgresql://postgres@127.0.0.1:55444/postgres"
F="$PWD/helm/incident-operator/files"
export EVIDENCE=off PRIMER_PATH="$F/primer.md" RUNBOOKS_DIR="$F/runbooks" OPERATIONS_DIR="$F/operations"
export UI_PORT=${UI_PORT:-8090} WEBHOOK_PORT=${WEBHOOK_PORT:-8091}
nohup .venv/bin/python -m incident_operator > .pids/io.log 2>&1 &
echo $! > .pids/io.pid
echo "UI on http://127.0.0.1:$UI_PORT, webhook on :$WEBHOOK_PORT (log: .pids/io.log)"
