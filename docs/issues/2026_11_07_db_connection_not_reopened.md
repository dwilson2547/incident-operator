# A postgres restart closed the triage loop's database connection, and every pass failed until the pod was restarted

**Date:** 2026-11-07
**Component:** `src/incident_operator/db.py` — `connect()`; its callers in `__main__.py` (triage loop, runbook store) and `executor.py`
**Severity:** High — while it lasted, no alert was triaged and no approved fix ran. `IncidentOperatorDown` caught it.

## Symptom

From about 19:15Z, every 5 s the operator logged `triage pass failed` with `psycopg.OperationalError: the connection
is closed`, and the executor logged the same. The pods stayed Running and Ready, so the only sign was
`IncidentOperatorDown` firing once no pass had completed in a while.

## Root cause

### One long-lived connection, never reopened

The triage loop, the last-good runbook store and the executor each opened a single `psycopg.connect()` at startup
and held it for the life of the process. The postgres StatefulSet was rolled out for a config change at about 19:15Z,
which closed every client connection. psycopg marks such a connection closed and does not reopen it, so every pass
afterwards failed on its first query. The loops caught the exception and retried, but always on the same dead
connection.

The UI and webhook use a `psycopg_pool.ConnectionPool`, which drops broken connections, so they were not affected.

## Troubleshooting steps

1. Checked the operator logs: the same `the connection is closed` error on every pass, starting at the postgres
   rollout.
2. Restarted both Deployments: passes resumed and the alert cleared. That confirmed the cause was connection state
   held in the process.
3. Went through the uses of the shared connection: only `execute`, `transaction()` and `info`, so a wrapper that
   forwards attribute access is enough.

## Fix

### `db.connect` returns a reconnecting wrapper

`Reconnecting` holds the connection and forwards attribute access to it. If the connection is `closed`, it opens a
new one first. The pass that hits the drop still fails and is logged; the next pass gets a fresh connection. If
postgres is still down, the reopen raises inside the pass, the loop logs that, and the next pass tries again.

### Test

`tests/test_db_reconnect.py` terminates the backend with `pg_terminate_backend` from another session, then checks
that the next query raises and the one after runs on a new backend pid. It fails on the old code.

### Verified in prod

On release `sha-7d3fa161cf8e`, all of the operator's postgres sessions were terminated at 20:12:12Z. Each loop logged
one `AdminShutdown` failure, and both pods had new sessions open by 20:12:17Z. No errors after that and no restarts.

## Files changed

- `src/incident_operator/db.py` — `Reconnecting`, `connect()`
- `tests/test_db_reconnect.py` — new
- `helm/incident-operator/values-prod.yaml` — image `sha-7d3fa161cf8e`
