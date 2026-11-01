"""Incidents, the evidence collected for them, and every model turn (docs/design.md §8).

Phase 1 lifecycle: `new` (alert received) -> `triaging` (evidence collected, job submitted) -> `analysed` or
`triage_failed`; independently, the alert can resolve (`alert_resolved_at`). One open incident per Alertmanager
fingerprint: re-sends update `last_seen`, a resolve closes it, and the next firing opens a new one.
"""
from __future__ import annotations

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
                       CHECK (status IN ('new', 'triaging', 'analysed', 'triage_failed')),
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
"""


def connect(url: str, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(url, row_factory=dict_row, autocommit=autocommit)


def migrate(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('incident_operator.migrate'))")
        conn.execute(SCHEMA)


# -- intake ------------------------------------------------------------------------------------------------------------

def record_firing(conn: psycopg.Connection, alert: dict) -> tuple[int, bool]:
    """Open an incident for a firing alert, or touch the open one with its fingerprint. Returns (id, created)."""
    labels = alert.get("labels") or {}
    row = conn.execute(
        """
        INSERT INTO incidents (fingerprint, alertname, severity, labels, annotations, generator_url, starts_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fingerprint) WHERE alert_resolved_at IS NULL
        DO UPDATE SET last_seen = now(), annotations = EXCLUDED.annotations
        RETURNING id, (xmax = 0) AS created
        """,
        (alert["fingerprint"], labels.get("alertname", "unknown"), labels.get("severity"), Jsonb(labels),
         Jsonb(alert.get("annotations") or {}), alert.get("generatorURL"), _ts(alert.get("startsAt"))),
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

def incidents_with_status(conn: psycopg.Connection, status: str, limit: int = 20) -> list[dict]:
    return conn.execute("SELECT * FROM incidents WHERE status = %s ORDER BY first_seen LIMIT %s",
                        (status, limit)).fetchall()


def add_evidence(conn: psycopg.Connection, incident_id: int, kind: str, source: str, content: str | None,
                 truncated: bool = False, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO evidence (incident_id, kind, source, content, truncated, error) VALUES (%s, %s, %s, %s, %s, %s)",
        (incident_id, kind, source, content, truncated, error),
    )


def add_turn(conn: psycopg.Connection, incident_id: int, *, session: str, prompt_version: str, model: str,
             fallback: list[str] | None, messages: list[dict], gpu_job_id) -> int:
    return conn.execute(
        """
        INSERT INTO model_turns (incident_id, session, prompt_version, model, fallback, messages, gpu_job_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (incident_id, session, prompt_version, model, fallback, Jsonb(messages), gpu_job_id),
    ).fetchone()["id"]


def pending_turns(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM model_turns WHERE status = 'submitted' ORDER BY submitted_at").fetchall()


def set_status(conn: psycopg.Connection, incident_id: int, status: str, **fields) -> None:
    cols = ", ".join(f"{k} = %s" for k in fields)
    conn.execute(f"UPDATE incidents SET status = %s{', ' + cols if cols else ''} WHERE id = %s",
                 (status, *fields.values(), incident_id))


def finish_turn(conn: psycopg.Connection, turn_id: int, *, status: str, result: dict | None = None,
                ran_model: str | None = None, worker: str | None = None, error: str | None = None) -> None:
    conn.execute(
        "UPDATE model_turns SET status = %s, result = %s, ran_model = %s, worker = %s, error = %s, finished_at = now()"
        " WHERE id = %s",
        (status, Jsonb(result) if result is not None else None, ran_model, worker, error, turn_id),
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
        SELECT id, fingerprint, alertname, severity, labels, status, runbook, first_seen, last_seen, alert_resolved_at,
               analysis_model, analysis_worker, notified_at, left(analysis, 300) AS analysis_preview
        FROM incidents ORDER BY first_seen DESC LIMIT %s
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
    return inc


def needs_runbook(conn: psycopg.Connection) -> list[dict]:
    """Alertnames that have fired with no runbook: the to-do list for writing runbooks (design §8.1)."""
    return conn.execute(
        """
        SELECT alertname, count(*) AS incidents, max(first_seen) AS last_seen,
               count(*) FILTER (WHERE alert_resolved_at IS NULL) AS open
        FROM incidents WHERE runbook IS NULL GROUP BY alertname ORDER BY incidents DESC, last_seen DESC
        """
    ).fetchall()
