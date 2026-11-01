"""Incidents, the evidence collected for them, and every model turn (docs/design.md §8).

Phase 1 lifecycle: `new` (alert received) -> `triaging` (evidence collected, job submitted) -> `analysed` or
`triage_failed`; independently, the alert can resolve (`alert_resolved_at`). One open incident per Alertmanager
fingerprint: re-sends update `last_seen`, a resolve closes it, and the next firing opens a new one.

Two brakes keep a noisy alert from turning into a pile of identical reports (triage.Engine.start_new):
- `repeat`: an incident whose problem key (problem_key()) was triaged within the repeat window is not triaged or
  mailed again; it points at that incident (`repeat_of`). Catches flapping and per-run names (CronJob runs, pods).
- `skipped`: over the hourly triage cap, incidents wait; one that resolved long before its turn is dropped.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                 bigserial PRIMARY KEY,
    fingerprint        text NOT NULL,
    alertname          text NOT NULL,
    severity           text,
    labels             jsonb NOT NULL,
    annotations        jsonb NOT NULL,
    generator_url      text,
    status             text NOT NULL DEFAULT 'new'
                       CHECK (status IN ('new', 'triaging', 'investigating', 'advising', 'analysed', 'triage_failed', 'repeat', 'skipped')),
    runbook            text,                       -- none in phase 1: every incident needs one
    starts_at          timestamptz,
    first_seen         timestamptz NOT NULL DEFAULT now(),
    last_seen          timestamptz NOT NULL DEFAULT now(),
    alert_resolved_at  timestamptz,
    analysis           text,                       -- the model's answer
    analysis_model     text,                       -- which model answered (gpu-scheduler ran_model)
    analysis_worker    text,
    notified_at        timestamptz,
    error              text
);
-- One open incident per fingerprint.
CREATE UNIQUE INDEX IF NOT EXISTS incidents_open_fingerprint ON incidents (fingerprint) WHERE alert_resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS incidents_status ON incidents (status) WHERE status IN ('new', 'triaging');
CREATE INDEX IF NOT EXISTS incidents_first_seen ON incidents (first_seen DESC);
-- Added after the first release: repeats and skips.
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS problem_key text;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS repeat_of bigint REFERENCES incidents (id);
ALTER TABLE incidents DROP CONSTRAINT IF EXISTS incidents_status_check;
ALTER TABLE incidents ADD CONSTRAINT incidents_status_check
    CHECK (status IN ('new', 'triaging', 'investigating', 'advising', 'analysed', 'triage_failed', 'repeat', 'skipped'));
CREATE INDEX IF NOT EXISTS incidents_problem_key ON incidents (problem_key, first_seen DESC);
CREATE INDEX IF NOT EXISTS incidents_repeat_of ON incidents (repeat_of) WHERE repeat_of IS NOT NULL;

CREATE TABLE IF NOT EXISTS evidence (
    id            bigserial PRIMARY KEY,
    incident_id   bigint NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    kind          text NOT NULL,                   -- pod | events | logs | job | node | ...
    source        text NOT NULL,                   -- what was read, e.g. "pod gyopart/ryans-…"
    content       text,
    truncated     boolean NOT NULL DEFAULT false,
    error         text,
    collected_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidence_incident ON evidence (incident_id);

-- Exactly what each model call saw and returned, for replay (design §9.5).
CREATE TABLE IF NOT EXISTS model_turns (
    id              bigserial PRIMARY KEY,
    incident_id     bigint NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    session         text NOT NULL,                 -- 'triage' in phase 1
    prompt_version  text NOT NULL,
    model           text NOT NULL,                 -- requested
    fallback        text[],
    gpu_job_id      uuid,
    status          text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'done', 'failed')),
    messages        jsonb NOT NULL,
    result          jsonb,
    ran_model       text,
    worker          text,
    error           text,
    submitted_at    timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX IF NOT EXISTS model_turns_incident ON model_turns (incident_id);
CREATE INDEX IF NOT EXISTS model_turns_pending ON model_turns (status) WHERE status = 'submitted';

-- Phase 2: runbooks (design §6). An incident that matched a runbook records its revision and the derived target;
-- the investigation ends in findings, the remediation step in advice. Evidence rows are operation results too.
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS runbook_rev text;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS target jsonb;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS findings jsonb;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS advice jsonb;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS trigger text NOT NULL DEFAULT 'generic';   -- generic|precursor|requested
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS operation text;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS round int;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS data jsonb;
ALTER TABLE model_turns ADD COLUMN IF NOT EXISTS turn int NOT NULL DEFAULT 1;
ALTER TABLE model_turns ADD COLUMN IF NOT EXISTS json_schema jsonb;
ALTER TABLE model_turns ADD COLUMN IF NOT EXISTS output jsonb;               -- the parsed JSON reply
"""


def connect(url: str, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(url, row_factory=dict_row, autocommit=autocommit)


def migrate(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('incident_operator.migrate'))")
        conn.execute(SCHEMA)


# -- intake ------------------------------------------------------------------------------------------------------------

# A trailing name segment Kubernetes or a controller generated: a number (CronJob schedule time, Job attempt,
# StatefulSet ordinal) or a 5-10 character hash with a digit in it (ReplicaSet hash, pod suffix).
_GENERATED = re.compile(r"-(?:\d+|(?=[a-z0-9]*\d)[a-z0-9]{5,10})$")
# Labels that change from one occurrence of the same problem to the next.
_VOLATILE = {"uid", "pod_ip", "container_id", "prometheus"}


def stem(name: str) -> str:
    """`offsite-objects-29886270-4rg6q` -> `offsite-objects`, `parts-direct-mercedes-20261031212238-2` ->
    `parts-direct-mercedes`, `gpu-scheduler-api-7b5d7788dc-x6fps` -> `gpu-scheduler-api`."""
    prev = None
    while prev != name:
        prev, name = name, _GENERATED.sub("", name)
    return name


def problem_key(labels: dict) -> str:
    """What makes two alerts "the same problem": the labels, with pod and Job names reduced to their stems and the
    per-occurrence labels dropped. (`instance` is dropped when something else names the target: for pod and Job
    alerts it is kube-state-metrics' own address.)"""
    key = {k: v for k, v in labels.items() if k not in _VOLATILE}
    for k in ("pod", "job_name"):
        if k in key:
            key[k] = stem(key[k])
    if any(k in key for k in ("pod", "job_name", "deployment", "statefulset", "daemonset", "node")):
        key.pop("instance", None)
    return json.dumps(key, sort_keys=True, separators=(",", ":"))


def record_firing(conn: psycopg.Connection, alert: dict) -> tuple[int, bool]:
    """Open an incident for a firing alert, or touch the open one with its fingerprint. Returns (id, created)."""
    labels = alert.get("labels") or {}
    row = conn.execute(
        """
        INSERT INTO incidents (fingerprint, alertname, severity, labels, annotations, generator_url, starts_at,
                               problem_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fingerprint) WHERE alert_resolved_at IS NULL
        DO UPDATE SET last_seen = now(), annotations = EXCLUDED.annotations
        RETURNING id, (xmax = 0) AS created
        """,
        (alert["fingerprint"], labels.get("alertname", "unknown"), labels.get("severity"), Jsonb(labels),
         Jsonb(alert.get("annotations") or {}), alert.get("generatorURL"), _ts(alert.get("startsAt")),
         problem_key(labels)),
    ).fetchone()
    return row["id"], row["created"]


def record_resolved(conn: psycopg.Connection, alert: dict) -> int | None:
    """Mark the open incident for this fingerprint as resolved. Returns its id, or None if none was open."""
    row = conn.execute(
        "UPDATE incidents SET alert_resolved_at = now(), last_seen = now()"
        " WHERE fingerprint = %s AND alert_resolved_at IS NULL RETURNING id",
        (alert["fingerprint"],),
    ).fetchone()
    return row["id"] if row else None


def _ts(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-"):        # Alertmanager's zero time
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# -- triage ------------------------------------------------------------------------------------------------------------

def last_triaged(conn: psycopg.Connection, key: str | None, within_seconds: int, exclude_id: int) -> int | None:
    """The newest incident with this problem key that was sent to the model within the window, if any."""
    if key is None:
        return None
    row = conn.execute(
        """
        SELECT id FROM incidents
        WHERE problem_key = %s AND id <> %s AND status IN ('triaging', 'analysed', 'triage_failed')
          AND first_seen > now() - make_interval(secs => %s)
        ORDER BY first_seen DESC LIMIT 1
        """,
        (key, exclude_id, within_seconds),
    ).fetchone()
    return row["id"] if row else None


def triages_since(conn: psycopg.Connection, seconds: int) -> int:
    """Incidents whose triage started in the window (an investigation's later turns don't count again)."""
    return conn.execute(
        "SELECT count(*) AS n FROM model_turns WHERE turn = 1 AND session IN ('triage', 'investigation')"
        " AND submitted_at > now() - make_interval(secs => %s)", (seconds,)).fetchone()["n"]


def incidents_with_status(conn: psycopg.Connection, status: str, limit: int = 20) -> list[dict]:
    return conn.execute("SELECT * FROM incidents WHERE status = %s ORDER BY first_seen LIMIT %s",
                        (status, limit)).fetchall()


def add_evidence(conn: psycopg.Connection, incident_id: int, kind: str, source: str, content: str | None,
                 truncated: bool = False, error: str | None = None, *, trigger: str = "generic",
                 operation: str | None = None, round: int | None = None, data=None) -> None:
    conn.execute(
        "INSERT INTO evidence (incident_id, kind, source, content, truncated, error, trigger, operation, round, data)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (incident_id, kind, source, content, truncated, error, trigger, operation, round,
         Jsonb(data) if data is not None else None),
    )


def add_turn(conn: psycopg.Connection, incident_id: int, *, session: str, prompt_version: str, model: str,
             fallback: list[str] | None, messages: list[dict], gpu_job_id, turn: int = 1,
             json_schema: dict | None = None) -> int:
    return conn.execute(
        """
        INSERT INTO model_turns (incident_id, session, prompt_version, model, fallback, messages, gpu_job_id, turn,
                                 json_schema)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (incident_id, session, prompt_version, model, fallback, Jsonb(messages), gpu_job_id, turn,
         Jsonb(json_schema) if json_schema is not None else None),
    ).fetchone()["id"]


def pending_turns(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM model_turns WHERE status = 'submitted' ORDER BY submitted_at").fetchall()


def set_status(conn: psycopg.Connection, incident_id: int, status: str, **fields) -> None:
    cols = ", ".join(f"{k} = %s" for k in fields)
    vals = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
    conn.execute(f"UPDATE incidents SET status = %s{', ' + cols if cols else ''} WHERE id = %s",
                 (status, *vals, incident_id))


def finish_turn(conn: psycopg.Connection, turn_id: int, *, status: str, result: dict | None = None,
                ran_model: str | None = None, worker: str | None = None, error: str | None = None,
                output: dict | None = None) -> None:
    conn.execute(
        "UPDATE model_turns SET status = %s, result = %s, ran_model = %s, worker = %s, error = %s, output = %s,"
        " finished_at = now() WHERE id = %s",
        (status, Jsonb(result) if result is not None else None, ran_model, worker, error,
         Jsonb(output) if output is not None else None, turn_id),
    )


def to_notify(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        "SELECT * FROM incidents WHERE status IN ('analysed', 'triage_failed') AND notified_at IS NULL ORDER BY id"
    ).fetchall()


def mark_notified(conn: psycopg.Connection, incident_id: int) -> None:
    conn.execute("UPDATE incidents SET notified_at = now() WHERE id = %s", (incident_id,))


# -- reading (UI) ------------------------------------------------------------------------------------------------------

def list_incidents(conn: psycopg.Connection, limit: int = 200) -> list[dict]:
    return conn.execute(
        """
        SELECT i.id, i.fingerprint, i.alertname, i.severity, i.labels, i.status, i.runbook, i.first_seen, i.last_seen,
               i.alert_resolved_at, i.analysis_model, i.analysis_worker, i.notified_at,
               left(i.analysis, 300) AS analysis_preview,
               r.n AS repeats, r.open AS repeats_open, r.last AS last_repeat
        FROM incidents i
        LEFT JOIN LATERAL (
            SELECT count(*) AS n, count(*) FILTER (WHERE alert_resolved_at IS NULL) AS open, max(first_seen) AS last
            FROM incidents WHERE repeat_of = i.id
        ) r ON true
        WHERE i.status <> 'repeat'                     -- shown folded under the incident they repeat
        ORDER BY greatest(i.first_seen, r.last) DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()


def get_incident(conn: psycopg.Connection, incident_id: int) -> dict | None:
    inc = conn.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,)).fetchone()
    if inc is None:
        return None
    inc["evidence"] = conn.execute("SELECT * FROM evidence WHERE incident_id = %s ORDER BY id",
                                   (incident_id,)).fetchall()
    inc["turns"] = conn.execute("SELECT * FROM model_turns WHERE incident_id = %s ORDER BY id",
                                (incident_id,)).fetchall()
    inc["repeats"] = conn.execute(
        "SELECT id, labels, first_seen, last_seen, alert_resolved_at FROM incidents WHERE repeat_of = %s"
        " ORDER BY first_seen DESC LIMIT 100", (incident_id,)).fetchall()
    return inc


def runbook_usage(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT runbook, count(*) AS incidents, max(first_seen) AS last_matched FROM incidents"
                        " WHERE runbook IS NOT NULL GROUP BY runbook").fetchall()


def needs_runbook(conn: psycopg.Connection) -> list[dict]:
    """Alertnames that have fired with no runbook: the to-do list for writing runbooks (design §8.1)."""
    return conn.execute(
        """
        SELECT alertname, count(*) AS incidents, max(first_seen) AS last_seen,
               count(*) FILTER (WHERE alert_resolved_at IS NULL) AS open
        FROM incidents WHERE runbook IS NULL GROUP BY alertname ORDER BY incidents DESC, last_seen DESC
        """
    ).fetchall()
