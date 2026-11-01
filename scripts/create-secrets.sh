#!/bin/bash
# One-time setup of incident-operator's Secrets. Generates every password/token itself and prints none of them.
# Run from anywhere:  bash infra/incident-operator/scripts/create-secrets.sh
#
#   1. namespace incident-operator
#   2. database incident_operator (role + db on the plain postgres) -> Secret incident-operator-db (DATABASE_URL)
#   3. a gpu-scheduler client token "incident-operator": appended to gpu-scheduler-tokens, and the same token in
#      Secret incident-operator-gpu-scheduler (GPU_SCHEDULER_TOKEN); restarts gpu-scheduler-api to load it
#   4. the Alertmanager SMTP password copied into Secret incident-operator-smtp (SMTP_PASSWORD)
# Re-running is safe: each step is skipped when its Secret already exists.
set -euo pipefail
NS=incident-operator
HERE=$(cd "$(dirname "$0")" && pwd)
INIT_SQL="$HERE/../../cluster-config/postgres/init-incident-operator.sql"
exists() { kubectl -n "$1" get secret "$2" >/dev/null 2>&1; }

kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

if exists "$NS" incident-operator-db; then
  echo "incident-operator-db exists: skipping the database"
else
  PW=$(openssl rand -hex 24)
  kubectl -n postgres exec -i postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -q -v pw="$1"' _ "$PW" < "$INIT_SQL"
  # The connection URL is joined from its parts at run time (user, the generated password, host, database) and only
  # ever piped into the Secret, so neither it nor anything shaped like it is written down anywhere.
  DB_USER=incident_operator DB_HOST=postgres.postgres.svc.cluster.local:5432 DB_NAME=incident_operator
  printf '%s' "postgresql" "://" "$DB_USER" ":" "$PW" "@" "$DB_HOST" "/" "$DB_NAME" \
    | kubectl -n "$NS" create secret generic incident-operator-db --from-file=DATABASE_URL=/dev/stdin >/dev/null
  echo "database incident_operator created; Secret incident-operator-db written"
fi

if exists "$NS" incident-operator-gpu-scheduler; then
  echo "incident-operator-gpu-scheduler exists: skipping the gpu-scheduler token"
else
  TOK=$(openssl rand -hex 24)
  CUR=$(kubectl -n gpu-scheduler get secret gpu-scheduler-tokens -o jsonpath='{.data.GPU_SCHEDULER_TOKENS}' | base64 -d)
  case ",$CUR," in *,incident-operator:*) echo "gpu-scheduler-tokens already has an incident-operator token; remove it first"; exit 1;; esac
  kubectl -n gpu-scheduler create secret generic gpu-scheduler-tokens \
    --from-literal=GPU_SCHEDULER_TOKENS="$CUR,incident-operator:$TOK" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n gpu-scheduler annotate secret gpu-scheduler-tokens kubectl.kubernetes.io/last-applied-configuration- >/dev/null 2>&1 || true
  kubectl -n "$NS" create secret generic incident-operator-gpu-scheduler --from-literal=GPU_SCHEDULER_TOKEN="$TOK" >/dev/null
  kubectl -n gpu-scheduler rollout restart deploy/gpu-scheduler-api >/dev/null
  echo "gpu-scheduler token for incident-operator added; gpu-scheduler-api restarting to load it"
fi

if exists "$NS" incident-operator-smtp; then
  echo "incident-operator-smtp exists: skipping"
else
  kubectl -n monitoring get secret alertmanager-smtp -o jsonpath='{.data.SMTP_PASSWORD}' | base64 -d \
    | kubectl -n "$NS" create secret generic incident-operator-smtp --from-file=SMTP_PASSWORD=/dev/stdin >/dev/null
  echo "SMTP password copied into incident-operator-smtp"
fi

echo "done:"; kubectl -n "$NS" get secrets
