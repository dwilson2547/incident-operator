"""Incidents, the evidence collected for them, and every model turn (docs/design.md §8).

Phase 1 lifecycle: `new` (alert received) -> `triaging` (evidence collected, job submitted) -> `analysed` or
`triage_failed`; independently, the alert can resolve (`alert_resolved_at`). One open incident per Alertmanager
fingerprint: re-sends update `last_seen`, a resolve closes it, and the next firing opens a new one.

Two brakes keep a noisy alert from turning into a pile of identical reports (triage.Engine.start_new):
- `repeat`: an incident whose problem key (problem_key()) was triaged within the repeat window is not triaged or
  mailed again; it points at that incident (`repeat_of`). Catches flapping and per-run names (CronJob runs, pods).
- `skipped`: over the hourly triage cap, incidents wait; one that resolved long before its turn is dropped.

A person can re-run triage on an answered incident with a model they pick (`rerun`, begin_rerun): same row, stored
evidence, attempt + 1, the answer it replaces kept in `earlier_attempts`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

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
                       CHECK (status IN ('new', 'triaging', 'investigating', 'advising', 'analysed', 'triage_failed', 'repeat', 'skipped', 'rerun')),
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
    CHECK (status IN ('new', 'triaging', 'investigating', 'advising', 'analysed', 'triage_failed', 'repeat', 'skipped', 'rerun'));
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

-- Phase 3: fixes (design §7, fixes.py). One row per proposed mutating operation; its pre/post check results are
-- evidence rows with trigger pre|post. `notified` is the status last mailed, so each outcome is mailed once.
CREATE TABLE IF NOT EXISTS op_runs (
    id             bigserial PRIMARY KEY,
    incident_id    bigint NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    operation      text NOT NULL,
    op_rev         text NOT NULL,
    params         jsonb NOT NULL,
    autonomy       text NOT NULL,
    status         text NOT NULL CHECK (status IN ('suggested', 'proposed', 'approved', 'rejected', 'refused',
                                                   'ready', 'running', 'settling', 'failed', 'succeeded',
                                                   'post_failed')),
    command        text,                           -- the kubectl equivalent
    proposed_at    timestamptz NOT NULL DEFAULT now(),
    decided_by     text,
    decided_at     timestamptz,
    started_at     timestamptz,
    post_due_at    timestamptz,
    finished_at    timestamptz,
    checks         text,                           -- one line per pre/post check: pass/FAIL, expectation, value seen
    result         text,                           -- what the executor did
    error          text,
    notified       text
);
CREATE INDEX IF NOT EXISTS op_runs_incident ON op_runs (incident_id);

-- The last version of each runbook and operation file that loaded (library.Library), so a rejected file falls back to
-- it even after a restart.
CREATE TABLE IF NOT EXISTS library_last_good (
    file      text PRIMARY KEY,                    -- runbooks/<name>.md | operations/<name>.yaml
    content   bytea NOT NULL,
    rev       text NOT NULL,
    saved_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS op_runs_active ON op_runs (status)
    WHERE status IN ('proposed', 'approved', 'ready', 'running', 'settling');

-- Re-runs (begin_rerun). A re-run keeps the row, because only one incident per fingerprint can be open, so its turns
-- are told apart by attempt and the answers it replaced are kept here. A fix still waiting when a re-run starts is
-- `superseded`: nobody decided on it, so it isn't a rejection in the fix's record.
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS attempt int NOT NULL DEFAULT 1;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS rerun_model text;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS rerun_by text;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS earlier_attempts jsonb NOT NULL DEFAULT '[]';
ALTER TABLE model_turns ADD COLUMN IF NOT EXISTS attempt int NOT NULL DEFAULT 1;
ALTER TABLE op_runs DROP CONSTRAINT IF EXISTS op_runs_status_check;
ALTER TABLE op_runs ADD CONSTRAINT op_runs_status_check
    CHECK (status IN ('suggested', 'proposed', 'approved', 'rejected', 'refused', 'ready', 'running', 'settling',
                      'failed', 'succeeded', 'post_failed', 'superseded'));
"""


class Reconnecting:
    """A connection for the long-running loops that reopens itself after the server dropped it.

    psycopg marks a connection closed once the server is gone (a postgres restart) and never reopens it, so a loop
    holding one fails every pass from then on. This opens a new one on the next use instead. The pass that hit the
    drop still fails; the loops already log that and retry.
    """

    def __init__(self, url: str, autocommit: bool = True):
        self._url, self._autocommit = url, autocommit
        self._conn = self._open()

    def _open(self) -> psycopg.Connection:
        return psycopg.connect(self._url, row_factory=dict_row, autocommit=self._autocommit)

    def __getattr__(self, name):
        if self._conn.closed:
            self._conn = self._open()
        return getattr(self._conn, name)


def connect(url: str, autocommit: bool = True) -> Reconnecting:
    return Reconnecting(url, autocommit)


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
        WHERE problem_key = %s AND id <> %s
          AND status IN ('triaging', 'investigating', 'advising', 'analysed', 'triage_failed', 'rerun')
          AND first_seen > now() - make_interval(secs => %s)
        ORDER BY first_seen DESC LIMIT 1
        """,
        (key, exclude_id, within_seconds),
    ).fetchone()
    return row["id"] if row else None


def triages_since(conn: psycopg.Connection, seconds: int) -> int:
    """Incidents whose triage started in the window (an investigation's later turns don't count again). The cap is
    against alert storms: a re-run someone asked for doesn't count, so comparing models can't hold back a real alert."""
    return conn.execute(
        "SELECT count(*) AS n FROM model_turns WHERE turn = 1 AND attempt = 1 AND session IN ('triage', 'investigation')"
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
             json_schema: dict | None = None, attempt: int = 1) -> int:
    return conn.execute(
        """
        INSERT INTO model_turns (incident_id, session, prompt_version, model, fallback, messages, gpu_job_id, turn,
                                 json_schema, attempt)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (incident_id, session, prompt_version, model, fallback, Jsonb(messages), gpu_job_id, turn,
         Jsonb(json_schema) if json_schema is not None else None, attempt),
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


RERUNNABLE = ("analysed", "triage_failed")
FIX_UNDER_WAY = ("approved", "ready", "running", "settling")


def begin_rerun(conn: psycopg.Connection, incident_id: int, model: str, who: str) -> str | None:
    """Queue a re-run with this model: keep the current answer in `earlier_attempts`, supersede a fix still waiting for
    a decision, and mark the incident mailed so the new answer isn't. Returns why it can't start, or None if it did."""
    with conn.transaction():
        inc = conn.execute("SELECT * FROM incidents WHERE id = %s FOR UPDATE", (incident_id,)).fetchone()
        if inc is None:
            return "no such incident"
        if inc["status"] not in RERUNNABLE:
            return f"it is {inc['status']}: only an answered or failed incident can be re-run"
        if conn.execute("SELECT 1 FROM op_runs WHERE incident_id = %s AND status = ANY(%s) LIMIT 1",
                        (incident_id, list(FIX_UNDER_WAY))).fetchone():
            return "a fix is under way on this incident"
        earlier = {k: inc[k] for k in ("attempt", "status", "analysis", "analysis_model", "analysis_worker", "findings",
                                       "advice", "error", "runbook", "runbook_rev", "rerun_by")}
        earlier["replaced_at"] = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE op_runs SET status = 'superseded', finished_at = now()"
            " WHERE incident_id = %s AND status = 'proposed'", (incident_id,))
        conn.execute(
            """
            UPDATE incidents SET status = 'rerun', attempt = attempt + 1, rerun_model = %s, rerun_by = %s,
                earlier_attempts = earlier_attempts || %s, analysis = NULL, analysis_model = NULL,
                analysis_worker = NULL, findings = NULL, advice = NULL, error = NULL,
                notified_at = coalesce(notified_at, now())
            WHERE id = %s
            """, (model, who, Jsonb([earlier]), incident_id))
    return None


def to_notify(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        "SELECT * FROM incidents WHERE status IN ('analysed', 'triage_failed') AND notified_at IS NULL ORDER BY id"
    ).fetchall()


def mark_notified(conn: psycopg.Connection, incident_id: int) -> None:
    conn.execute("UPDATE incidents SET notified_at = now() WHERE id = %s", (incident_id,))


# -- fixes (fixes.py, executor.py) ---------------------------------------------------------------------------------------

def add_op_run(conn: psycopg.Connection, incident_id: int, operation: str, op_rev: str, params: dict, autonomy: str,
               status: str, command: str | None = None, error: str | None = None) -> int:
    return conn.execute(
        "INSERT INTO op_runs (incident_id, operation, op_rev, params, autonomy, status, command, error, finished_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s = 'refused' THEN now() END) RETURNING id",
        (incident_id, operation, op_rev, Jsonb(params), autonomy, status, command, error, status)).fetchone()["id"]


def get_op_run(conn: psycopg.Connection, run_id: int) -> dict | None:
    return conn.execute("SELECT * FROM op_runs WHERE id = %s", (run_id,)).fetchone()


def op_runs_with_status(conn: psycopg.Connection, status: str) -> list[dict]:
    return conn.execute("SELECT * FROM op_runs WHERE status = %s ORDER BY id", (status,)).fetchall()


def decide_op_run(conn: psycopg.Connection, run_id: int, approve: bool, who: str) -> bool:
    """Approve or reject a proposed fix. False if it isn't awaiting a decision (already decided, or suggest-only)."""
    row = conn.execute(
        "UPDATE op_runs SET status = %s, decided_by = %s, decided_at = now(),"
        " finished_at = CASE WHEN %s THEN NULL ELSE now() END WHERE id = %s AND status = 'proposed' RETURNING id",
        ("approved" if approve else "rejected", who, approve, run_id)).fetchone()
    return row is not None


_FINAL = ("refused", "rejected", "failed", "succeeded", "post_failed", "superseded")


def update_op_run(conn: psycopg.Connection, run_id: int, status: str, *, checks: str | None = None,
                  result: str | None = None, error: str | None = None, settle_seconds: int | None = None) -> None:
    conn.execute(
        """
        UPDATE op_runs SET status = %(s)s,
            checks = coalesce(%(checks)s, checks), result = coalesce(%(result)s, result), error = %(error)s,
            post_due_at = CASE WHEN %(settle)s::int IS NOT NULL THEN now() + make_interval(secs => %(settle)s) ELSE post_due_at END,
            finished_at = CASE WHEN %(s)s = ANY(%(final)s) THEN now() ELSE finished_at END
        WHERE id = %(id)s
        """,
        {"s": status, "checks": checks, "result": result, "error": error, "settle": settle_seconds,
         "final": list(_FINAL), "id": run_id})


def claim_ready(conn: psycopg.Connection) -> dict | None:
    """The executor takes the oldest ready run (one executor, but safe with more)."""
    return conn.execute(
        "UPDATE op_runs SET status = 'running', started_at = now() WHERE id = ("
        " SELECT id FROM op_runs WHERE status = 'ready' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *"
    ).fetchone()


def op_runs_due_for_post(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM op_runs WHERE status = 'settling' AND post_due_at <= now() ORDER BY id"
                        ).fetchall()


def expire_ready(conn: psycopg.Connection, seconds: int) -> int:
    return len(conn.execute(
        "UPDATE op_runs SET status = 'failed', finished_at = now(),"
        " error = 'the executor did not take it in time: is the executor running?'"
        " WHERE status = 'ready' AND decided_at < now() - make_interval(secs => %s) RETURNING id", (seconds,)
    ).fetchall())


def op_runs_since(conn: psycopg.Connection, operation: str, seconds: int, params: dict | None = None) -> int:
    """Runs of an operation that reached the cluster (or are about to) in the window, for its limits."""
    return conn.execute(
        "SELECT count(*) AS n FROM op_runs WHERE operation = %s"
        " AND status IN ('ready', 'running', 'settling', 'failed', 'succeeded', 'post_failed')"
        " AND coalesce(started_at, decided_at) > now() - make_interval(secs => %s)"
        " AND (%s::jsonb IS NULL OR params = %s::jsonb)",
        (operation, seconds, Jsonb(params) if params is not None else None,
         Jsonb(params) if params is not None else None)).fetchone()["n"]


def op_runs_to_notify(conn: psycopg.Connection) -> list[dict]:
    """Outcomes of approved fixes not yet mailed (the proposal itself goes out with the incident's email)."""
    return conn.execute(
        "SELECT * FROM op_runs WHERE decided_by IS NOT NULL AND status = ANY(%s)"
        " AND notified IS DISTINCT FROM status ORDER BY id", (list(_FINAL),)).fetchall()


def mark_op_run_notified(conn: psycopg.Connection, run_id: int, status: str) -> None:
    conn.execute("UPDATE op_runs SET notified = %s WHERE id = %s", (status, run_id))


def operation_records(conn: psycopg.Connection) -> list[dict]:
    """Each fix's record (design §7.1), what moving it up an autonomy level is decided on."""
    return conn.execute(
        """
        SELECT operation, count(*) AS proposed,
               count(*) FILTER (WHERE status IN ('succeeded', 'post_failed', 'failed')) AS ran,
               count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
               count(*) FILTER (WHERE status = 'post_failed') AS post_failed,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'rejected') AS rejected,
               count(*) FILTER (WHERE status = 'refused') AS refused,
               max(finished_at) FILTER (WHERE status IN ('post_failed', 'failed')) AS last_failure
        FROM op_runs GROUP BY operation
        """).fetchall()


# -- health (/metrics) -----------------------------------------------------------------------------------------------

def health(conn: psycopg.Connection) -> dict:
    """What the operator's own alerts watch: incidents by status, the oldest one still waiting for triage, the oldest
    answer not yet mailed, and fixes by status."""
    out = {"incidents": {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, count(*) AS n FROM incidents GROUP BY status").fetchall()},
           "fixes": {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, count(*) AS n FROM op_runs GROUP BY status").fetchall()}}
    out |= conn.execute(
        """
        SELECT coalesce(extract(epoch FROM now() - min(first_seen) FILTER (
                   WHERE status IN ('new', 'triaging', 'investigating', 'advising') AND attempt = 1)), 0)
                   AS oldest_waiting,                  -- a re-run of an old incident isn't an alert waiting for triage
               coalesce(extract(epoch FROM now() - min(first_seen) FILTER (
                   WHERE status IN ('analysed', 'triage_failed') AND notified_at IS NULL)), 0) AS oldest_unmailed
        FROM incidents
        """).fetchone()
    return out


# -- library ---------------------------------------------------------------------------------------------------------

class LastGood:
    """library.Library's store for the last good version of each file."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def save(self, file: str, content: bytes, rev: str) -> None:
        self.conn.execute(
            "INSERT INTO library_last_good (file, content, rev) VALUES (%s, %s, %s) ON CONFLICT (file) DO UPDATE"
            " SET content = EXCLUDED.content, rev = EXCLUDED.rev, saved_at = now()"
            " WHERE library_last_good.rev <> EXCLUDED.rev", (file, content, rev))

    def load(self, file: str) -> tuple[bytes, datetime] | None:
        row = self.conn.execute("SELECT content, saved_at FROM library_last_good WHERE file = %s", (file,)).fetchone()
        return (bytes(row["content"]), row["saved_at"]) if row else None


# -- reading (UI) ------------------------------------------------------------------------------------------------------

def list_incidents(conn: psycopg.Connection, limit: int = 200) -> list[dict]:
    return conn.execute(
        """
        SELECT i.id, i.fingerprint, i.alertname, i.severity, i.labels, i.status, i.runbook, i.first_seen, i.last_seen,
               i.alert_resolved_at, i.analysis_model, i.analysis_worker, i.notified_at,
               left(i.analysis, 300) AS analysis_preview,
               (SELECT status FROM op_runs o WHERE o.incident_id = i.id ORDER BY o.id DESC LIMIT 1) AS fix_status,
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
    inc["fixes"] = conn.execute("SELECT * FROM op_runs WHERE incident_id = %s ORDER BY id", (incident_id,)).fetchall()
    inc["repeats"] = conn.execute(
        "SELECT id, labels, first_seen, last_seen, alert_resolved_at FROM incidents WHERE repeat_of = %s"
        " ORDER BY first_seen DESC LIMIT 100", (incident_id,)).fetchall()
    return inc


RELATED_WINDOW_SECONDS = 3600     # other alerts this close count as "around" this one
RELATED_CLOSE_SECONDS = 600       # counted apart: alerts fired by one cause tend to land within minutes
_AT = "coalesce(starts_at, first_seen)"


def related(conn: psycopg.Connection, inc: dict, limit: int = 50) -> dict:
    """What else was going on, from the incident list alone: any other alert around this one's time, nearest first, and
    how often this same problem fired before it. Measured from this incident, not from now, so a re-run of an old one
    sees its own moment. Alerts that came in after it are included if they are in by now."""
    at = inc.get("starts_at") or inc["first_seen"]
    args = {"at": at, "id": inc["id"], "key": inc.get("problem_key"), "window": RELATED_WINDOW_SECONDS,
            "close": RELATED_CLOSE_SECONDS, "limit": limit}
    nearby = conn.execute(
        f"""
        SELECT id, alertname, labels, status, repeat_of,
               extract(epoch FROM {_AT} - %(at)s)::float AS offset_seconds,
               extract(epoch FROM alert_resolved_at - {_AT})::float AS fired_seconds,
               count(*) OVER () AS total,
               count(*) FILTER (WHERE abs(extract(epoch FROM {_AT} - %(at)s)) <= %(close)s) OVER () AS close
        FROM incidents
        WHERE id <> %(id)s AND problem_key IS DISTINCT FROM %(key)s
          AND abs(extract(epoch FROM {_AT} - %(at)s)) <= %(window)s
        ORDER BY abs(extract(epoch FROM {_AT} - %(at)s)), id LIMIT %(limit)s
        """, args).fetchall()
    earlier = conn.execute(
        f"""
        SELECT id, status, extract(epoch FROM %(at)s - {_AT})::float AS age_seconds, count(*) OVER () AS total
        FROM incidents WHERE problem_key = %(key)s AND id <> %(id)s AND {_AT} < %(at)s
        ORDER BY {_AT} DESC LIMIT 5
        """, args).fetchall()
    return {"nearby": nearby, "nearby_total": nearby[0]["total"] if nearby else 0,
            "nearby_close": nearby[0]["close"] if nearby else 0, "earlier": earlier,
            "earlier_total": earlier[0]["total"] if earlier else 0}


def runbook_usage(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT runbook, count(*) AS incidents, max(first_seen) AS last_matched FROM incidents"
                        " WHERE runbook IS NOT NULL GROUP BY runbook").fetchall()


def needs_runbook(conn: psycopg.Connection, covered=None) -> list[dict]:
    """Alertnames that have fired with no runbook: the to-do list for writing runbooks (design §8.1).

    `covered(labels)` says whether a runbook matches an incident now. Those are left out: a runbook written after an
    incident fired covers the next one, so the alert is no longer on the to-do list. Checked per incident, because a
    runbook can match on labels other than the alertname."""
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT alertname, labels, first_seen, alert_resolved_at IS NULL AS open FROM incidents"
                          " WHERE runbook IS NULL").fetchall():
        if covered is not None and covered(r["labels"] or {}):
            continue
        a = out.setdefault(r["alertname"], {"alertname": r["alertname"], "incidents": 0, "last_seen": r["first_seen"],
                                            "open": 0})
        a["incidents"] += 1
        a["open"] += r["open"]
        a["last_seen"] = max(a["last_seen"], r["first_seen"])
    rows = sorted(out.values(), key=lambda a: a["last_seen"], reverse=True)
    return sorted(rows, key=lambda a: a["incidents"], reverse=True)
