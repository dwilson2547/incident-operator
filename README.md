---
tier: project
domains: [ai]
---

# incident-operator

Turns Alertmanager alerts into tracked incidents and has a small local LLM (through gpu-scheduler) triage each one.
The model picks and explains; code decides what can run. Design: [`docs/design.md`](docs/design.md).

**Status: phase 1 (suggest-only), built 2026-11-01.** Nothing it does changes the cluster.

## What phase 1 does

1. **Intake:** Alertmanager posts the alerts it also mails (critical, plus the listed warnings) to the webhook. One
   incident per alert fingerprint: re-sends update it, a resolve closes it, the next firing opens a new one.
2. **Evidence**, collected by code, read-only, before any model call: for whatever the alert's labels name, a pod's
   status, events and log tail (and the previous container's after a restart), a Job's status and its newest pod, a
   workload's status and events, a node's conditions (node-exporter's `instance` IP is mapped to the node). Capped per
   item and per incident (14k characters), so a small model's prompt stays around 1.5–5k tokens.
3. **Triage:** one call to gpu-scheduler with the system prompt, the environment primer
   (`helm/incident-operator/files/primer.md`, mounted, read on every call) and the evidence. `qwen35-9b` on the
   3080, with `gemma4-26b-a4b` on the Mac as fallback after 60 s. Every prompt is stored as sent.
4. **Email**, one per incident, with the answer, which model and machine answered, the evidence list and a link.
5. **Page** at http://incident-operator.cluster.local (LAN): incidents, each one's answer, evidence and exact prompt,
   and "needs a runbook": alerts that fired with no runbook (all of them, in phase 1).

Checked end to end on 2026-11-01 against the real cluster and gpu-scheduler: two real failed Jobs (an OOMKilled
`rclone` backup, a parts-direct scraper that timed out) were triaged in 25 s by `qwen35-9b` on the 3080; both
answers found the right container, exit code and log line.

## Layout

- `src/incident_operator/db.py`: incidents, evidence, model turns (schema created on start).
- `src/incident_operator/evidence.py`: the read-only Kubernetes reads and the budget.
- `src/incident_operator/triage.py`: prompt, gpu-scheduler client, and the engine (three idempotent passes: new →
  submit, poll, notify).
- `src/incident_operator/notify.py`: SMTP email (same settings as scrape-job-manager).
- `src/incident_operator/app.py` + `static/index.html`: the webhook (port 8081, in-cluster Service only, no Ingress,
  so the LAN can't inject alerts) and the page (8080).
- `helm/incident-operator/`: Deployment, the read-only ClusterRole, Services, LAN Ingress, the primer ConfigMap.
- `scripts/create-secrets.sh`, `scripts/setup-argo-access.sh`: one-time setup (they generate secrets and print none).

## Configuration

| Env | |
|---|---|
| `DATABASE_URL` | Postgres (Secret `incident-operator-db`) |
| `GPU_SCHEDULER_URL`, `GPU_SCHEDULER_TOKEN` | gpu-scheduler API and this client's token |
| `TRIAGE_MODEL`, `TRIAGE_FALLBACK`, `TRIAGE_FALLBACK_AFTER_SECONDS` | `qwen35-9b`, `gemma4-26b-a4b`, 60 |
| `TRIAGE_DEADLINE_SECONDS`, `TRIAGE_MAX_NEW_TOKENS` | 900, 1024 |
| `PRIMER_PATH` | the primer file (the chart mounts it at `/etc/incident-operator/primer.md`) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO`, `PUBLIC_URL` | email; off unless host, from and to are set |
| `UI_PORT`, `WEBHOOK_PORT` | 8080, 8081 |
| `KUBE_API_URL` | local runs: e.g. `kubectl proxy` at `http://127.0.0.1:8001`; in the cluster the service account is used |
| `EVIDENCE=off` | skip the Kubernetes reads |

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
docker run -d --name io-pg -e POSTGRES_PASSWORD=dev -p 127.0.0.1:55444:5432 postgres:17
PGPASSWORD=dev INCIDENT_OPERATOR_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55444/postgres .venv/bin/pytest -q
./start.sh          # the service on :8090 (UI) / :8091 (webhook), evidence and email off
./kill.sh
```

The test fixture refuses a database with tables other than its own (another project's local Postgres answered on the
same port once).

## Release

Pushing to `main` runs the tests and publishes `dwilson2547/incident-operator:sha-<12>` (`.github/workflows/ci.yml`;
chart, primer and doc changes don't build). To deploy: set `image.tag` in `values-prod.yaml` and push; Argo app
`incident-operator` rolls it out. A primer change is a chart change: it reaches the running pod within a minute.
