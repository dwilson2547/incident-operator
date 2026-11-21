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

The data access is one class, Store: each method is one transaction and returns models (models.py) loaded in full,
so a caller can read them after the session is gone. A relationship not loaded raises rather than querying behind the
caller's back (lazy="raise"): get_incident loads evidence, turns and fixes; everything else reads the row alone.
Reports that are aggregates rather than rows (related, health, the UI's lists) come back as dicts.
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict, Unpack

from sqlalchemy import Boolean, ColumnElement, create_engine, func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload, sessionmaker

from . import schema
from .models import Evidence, Incident, LibraryLastGood, ModelTurn, OpRun
from .schema import engine_url
from .states import (
    ANSWERED,
    FINAL,
    RAN,
    REACHED_CLUSTER,
    SENT_TO_MODEL,
    UNDER_WAY,
    WAITING,
    FixStatus,
    IncidentStatus,
    Trigger,
    TurnStatus,
)
from .states import Session as TurnSession

if TYPE_CHECKING:
    from .ops import Result


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


def _ts(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-"):        # Alertmanager's zero time
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _jsonable(v) -> bool:
    if v is None:
        return False
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False


def _ago(seconds: int) -> ColumnElement[datetime]:
    """now() less that many seconds, on the database's clock like every other timestamp here."""
    return func.now() - timedelta(seconds=seconds)


class IncidentUpdate(TypedDict, total=False):
    """What a status change may set alongside the status (Store.set_status)."""
    repeat_of: int
    runbook: str | None
    runbook_rev: str | None
    target: dict[str, Any] | None
    findings: dict[str, Any] | None
    advice: dict[str, Any] | None
    analysis: str
    analysis_model: str | None
    analysis_worker: str | None
    error: str | None


class Nearby(TypedDict):
    id: int
    alertname: str
    labels: dict[str, Any]
    status: str
    repeat_of: int | None
    offset_seconds: float               # from this incident; negative is earlier
    fired_seconds: float | None         # None: still firing


class Earlier(TypedDict):
    id: int
    status: str
    age_seconds: float


class Related(TypedDict):
    """Store.related: what else was going on around an incident."""
    nearby: list[Nearby]
    nearby_total: int
    nearby_close: int
    earlier: list[Earlier]
    earlier_total: int


RELATED_WINDOW_SECONDS = 3600     # other alerts this close count as "around" this one
RELATED_CLOSE_SECONDS = 600       # counted apart: alerts fired by one cause tend to land within minutes
_AT = "coalesce(starts_at, first_seen)"


class Store:
    """The database. Safe to share between threads: each call takes a pooled connection for its transaction, and the
    pool tests a connection before handing it out, so one dropped by a postgres restart is replaced, not used."""

    def __init__(self, url: str, pool_size: int = 4):
        self.engine = create_engine(engine_url(url), pool_pre_ping=True, pool_size=pool_size, max_overflow=4)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """One transaction: committed when the block ends, rolled back if it raises."""
        with self._sessions.begin() as s:
            yield s

    def migrate(self) -> None:
        """Bring the database up to the models (migrations/); schema.upgrade says when it refuses."""
        schema.upgrade(self.engine)

    # -- intake --------------------------------------------------------------------------------------------------------

    def record_firing(self, alert: dict) -> tuple[int, bool]:
        """Open an incident for a firing alert, or touch the open one with its fingerprint. Returns (id, created)."""
        labels = alert.get("labels") or {}
        stmt = insert(Incident).values(
            fingerprint=alert["fingerprint"], alertname=labels.get("alertname", "unknown"),
            severity=labels.get("severity"), labels=labels, annotations=alert.get("annotations") or {},
            generator_url=alert.get("generatorURL"), starts_at=_ts(alert.get("startsAt")),
            problem_key=problem_key(labels))
        stmt = stmt.on_conflict_do_update(
            index_elements=[Incident.fingerprint], index_where=Incident.alert_resolved_at.is_(None),
            set_={"last_seen": func.now(), "annotations": stmt.excluded.annotations},
        ).returning(Incident.id, literal_column("xmax = 0", Boolean))      # xmax is 0 on a row this statement inserted
        with self.session() as s:
            inc_id, created = s.execute(stmt).one()
        return inc_id, created

    def record_resolved(self, alert: dict) -> int | None:
        """Mark the open incident for this fingerprint as resolved. Returns its id, or None if none was open."""
        with self.session() as s:
            return s.scalar(update(Incident)
                            .where(Incident.fingerprint == alert["fingerprint"], Incident.alert_resolved_at.is_(None))
                            .values(alert_resolved_at=func.now(), last_seen=func.now()).returning(Incident.id))

    # -- triage --------------------------------------------------------------------------------------------------------

    def last_triaged(self, key: str | None, within_seconds: int, exclude_id: int) -> Incident | None:
        """The newest incident with this problem key that was sent to the model within the window, if any."""
        if key is None:
            return None
        with self.session() as s:
            return s.scalars(select(Incident).where(
                Incident.problem_key == key, Incident.id != exclude_id, Incident.status.in_(SENT_TO_MODEL),
                Incident.first_seen > _ago(within_seconds)).order_by(Incident.first_seen.desc()).limit(1)).first()

    def triages_since(self, seconds: int) -> int:
        """Incidents whose triage started in the window (an investigation's later turns don't count again). The cap is
        against alert storms: a re-run someone asked for doesn't count, so comparing models can't hold back a real
        alert."""
        with self.session() as s:
            return s.scalar(select(func.count()).select_from(ModelTurn).where(
                ModelTurn.turn == 1, ModelTurn.attempt == 1,
                ModelTurn.session.in_([TurnSession.TRIAGE, TurnSession.INVESTIGATION]),
                ModelTurn.submitted_at > _ago(seconds))) or 0

    def incidents_with_status(self, status: IncidentStatus, limit: int = 20) -> list[Incident]:
        with self.session() as s:
            return list(s.scalars(select(Incident).where(Incident.status == status)
                                  .order_by(Incident.first_seen).limit(limit)))

    def add_evidence(self, incident_id: int, kind: str, source: str, content: str | None, truncated: bool = False,
                     error: str | None = None) -> None:
        """Generic evidence (evidence.collect)."""
        with self.session() as s:
            s.add(Evidence(incident_id=incident_id, kind=kind, source=source, content=content, truncated=truncated,
                           error=error, trigger=Trigger.GENERIC))

    def drop_evidence(self, incident_id: int) -> None:
        """Forget what was collected for an attempt that never reached the model."""
        with self.session() as s:
            s.query(Evidence).filter(Evidence.incident_id == incident_id).delete()

    def add_results(self, incident_id: int, results: list[Result], trigger: Trigger, round: int | None = None) -> None:
        """Operation results as evidence rows: what the page shows and what a re-run reads back (Result.from_evidence).
        A result's structured form is kept when it is JSON; the text always is."""
        with self.session() as s:
            s.add_all(Evidence(incident_id=incident_id, kind=r.operation, source=r.source, content=r.text,
                               truncated=r.truncated, error=r.error, trigger=trigger, operation=r.operation,
                               round=round, data=r.data if _jsonable(r.data) else None) for r in results)

    def add_turn(self, incident_id: int, *, session: TurnSession, prompt_version: str, model: str,
                 fallback: list[str] | None, messages: list[dict], gpu_job_id: str, turn: int = 1,
                 json_schema: dict | None = None, attempt: int = 1) -> int:
        row = ModelTurn(incident_id=incident_id, session=session, prompt_version=prompt_version, model=model,
                        fallback=fallback, messages=messages, gpu_job_id=uuid.UUID(gpu_job_id), turn=turn,
                        json_schema=json_schema, attempt=attempt)
        with self.session() as s:
            s.add(row)
            s.flush()
            return row.id

    def pending_turns(self) -> list[ModelTurn]:
        with self.session() as s:
            return list(s.scalars(select(ModelTurn).where(ModelTurn.status == TurnStatus.SUBMITTED)
                                  .order_by(ModelTurn.submitted_at)))

    def set_status(self, incident_id: int, status: IncidentStatus, **fields: Unpack[IncidentUpdate]) -> None:
        with self.session() as s:
            s.execute(update(Incident).where(Incident.id == incident_id).values(status=status, **fields))

    def finish_turn(self, turn_id: int, *, status: TurnStatus, result: dict | None = None,
                    ran_model: str | None = None, worker: str | None = None, error: str | None = None,
                    output: dict | None = None) -> None:
        with self.session() as s:
            s.execute(update(ModelTurn).where(ModelTurn.id == turn_id).values(
                status=status, result=result, ran_model=ran_model, worker=worker, error=error, output=output,
                finished_at=func.now()))

    def begin_rerun(self, incident_id: int, model: str, who: str) -> str | None:
        """Queue a re-run with this model: keep the current answer in `earlier_attempts`, supersede a fix still waiting
        for a decision, and mark the incident mailed so the new answer isn't. Returns why it can't start, or None if it
        did."""
        with self.session() as s:
            inc = s.get(Incident, incident_id, with_for_update=True)
            if inc is None:
                return "no such incident"
            if inc.status not in ANSWERED:
                return f"it is {inc.status}: only an answered or failed incident can be re-run"
            if s.scalar(select(OpRun.id).where(OpRun.incident_id == incident_id, OpRun.status.in_(UNDER_WAY))
                        .limit(1)):
                return "a fix is under way on this incident"
            earlier = {k: getattr(inc, k) for k in ("attempt", "status", "analysis", "analysis_model",
                                                    "analysis_worker", "findings", "advice", "error", "runbook",
                                                    "runbook_rev", "rerun_by")}
            earlier["replaced_at"] = datetime.now(UTC).isoformat()
            s.execute(update(OpRun).where(OpRun.incident_id == incident_id, OpRun.status == FixStatus.PROPOSED)
                      .values(status=FixStatus.SUPERSEDED, finished_at=func.now()))
            s.execute(update(Incident).where(Incident.id == incident_id).values(
                status=IncidentStatus.RERUN, attempt=Incident.attempt + 1, rerun_model=model, rerun_by=who,
                earlier_attempts=[*inc.earlier_attempts, earlier], analysis=None, analysis_model=None,
                analysis_worker=None, findings=None, advice=None, error=None,
                notified_at=func.coalesce(Incident.notified_at, func.now())))
        return None

    def to_notify(self) -> list[Incident]:
        with self.session() as s:
            return list(s.scalars(select(Incident).where(Incident.status.in_(ANSWERED), Incident.notified_at.is_(None))
                                  .order_by(Incident.id)))

    def mark_notified(self, incident_id: int) -> None:
        with self.session() as s:
            s.execute(update(Incident).where(Incident.id == incident_id).values(notified_at=func.now()))

    # -- fixes (fixes.py, executor.py) ---------------------------------------------------------------------------------

    def add_op_run(self, incident_id: int, operation: str, op_rev: str, params: dict, autonomy: str,
                   status: FixStatus, command: str | None = None, error: str | None = None) -> int:
        run = OpRun(incident_id=incident_id, operation=operation, op_rev=op_rev, params=params, autonomy=autonomy,
                    status=status, command=command, error=error)
        if status in FINAL:
            run.finished_at = func.now()          # type: ignore[assignment]  # set by the database, like the rest
        with self.session() as s:
            s.add(run)
            s.flush()
            return run.id

    def get_op_run(self, run_id: int) -> OpRun | None:
        with self.session() as s:
            return s.get(OpRun, run_id)

    def op_runs_with_status(self, status: FixStatus) -> list[OpRun]:
        with self.session() as s:
            return list(s.scalars(select(OpRun).where(OpRun.status == status).order_by(OpRun.id)))

    def decide_op_run(self, run_id: int, approve: bool, who: str) -> bool:
        """Approve or reject a proposed fix. False if it isn't awaiting a decision (already decided, or
        suggest-only)."""
        with self.session() as s:
            return s.scalar(update(OpRun).where(OpRun.id == run_id, OpRun.status == FixStatus.PROPOSED).values(
                status=FixStatus.APPROVED if approve else FixStatus.REJECTED, decided_by=who, decided_at=func.now(),
                finished_at=None if approve else func.now()).returning(OpRun.id)) is not None

    def update_op_run(self, run_id: int, status: FixStatus, *, checks: str | None = None, result: str | None = None,
                      error: str | None = None, settle_seconds: int | None = None) -> None:
        """Move a run on. `checks` and `result` are kept when not given; `error` is always replaced."""
        values: dict[str, Any] = {"status": status, "error": error}
        if checks is not None:
            values["checks"] = checks
        if result is not None:
            values["result"] = result
        if settle_seconds is not None:
            values["post_due_at"] = func.now() + timedelta(seconds=settle_seconds)
        if status in FINAL:
            values["finished_at"] = func.now()
        with self.session() as s:
            s.execute(update(OpRun).where(OpRun.id == run_id).values(**values))

    def claim_ready(self) -> OpRun | None:
        """The executor takes the oldest ready run, in one statement (one executor, but safe with more)."""
        oldest = (select(OpRun.id).where(OpRun.status == FixStatus.READY).order_by(OpRun.id)
                  .with_for_update(skip_locked=True).limit(1).scalar_subquery())
        with self.session() as s:
            return s.scalars(update(OpRun).where(OpRun.id == oldest)
                             .values(status=FixStatus.RUNNING, started_at=func.now()).returning(OpRun)
                             .execution_options(synchronize_session=False)).one_or_none()

    def op_runs_due_for_post(self) -> list[OpRun]:
        with self.session() as s:
            return list(s.scalars(select(OpRun).where(OpRun.status == FixStatus.SETTLING,
                                                      OpRun.post_due_at <= func.now()).order_by(OpRun.id)))

    def expire_ready(self, seconds: int) -> int:
        with self.session() as s:
            return len(s.scalars(update(OpRun).where(OpRun.status == FixStatus.READY,
                                                     OpRun.decided_at < _ago(seconds)).values(
                status=FixStatus.FAILED, finished_at=func.now(),
                error="the executor did not take it in time: is the executor running?").returning(OpRun.id)).all())

    def op_runs_since(self, operation: str, seconds: int, params: dict | None = None) -> int:
        """Runs of an operation that reached the cluster (or are about to) in the window, for its limits."""
        q = select(func.count()).select_from(OpRun).where(
            OpRun.operation == operation, OpRun.status.in_(REACHED_CLUSTER),
            func.coalesce(OpRun.started_at, OpRun.decided_at) > _ago(seconds))
        if params is not None:
            q = q.where(OpRun.params == params)
        with self.session() as s:
            return s.scalar(q) or 0

    def op_runs_to_notify(self) -> list[OpRun]:
        """Outcomes of approved fixes not yet mailed (the proposal itself goes out with the incident's email)."""
        with self.session() as s:
            return list(s.scalars(select(OpRun).where(OpRun.decided_by.is_not(None), OpRun.status.in_(FINAL),
                                                      OpRun.notified.is_distinct_from(OpRun.status))
                                  .order_by(OpRun.id)))

    def mark_op_run_notified(self, run_id: int, status: FixStatus) -> None:
        with self.session() as s:
            s.execute(update(OpRun).where(OpRun.id == run_id).values(notified=status))

    def operation_records(self) -> list[dict[str, Any]]:
        """Each fix's record (design §7.1), what moving it up an autonomy level is decided on."""
        def n(*statuses: FixStatus):
            return func.count().filter(OpRun.status.in_(statuses))
        q = select(OpRun.operation, func.count().label("proposed"), n(*RAN).label("ran"),
                   n(FixStatus.SUCCEEDED).label("succeeded"), n(FixStatus.POST_FAILED).label("post_failed"),
                   n(FixStatus.FAILED).label("failed"), n(FixStatus.REJECTED).label("rejected"),
                   n(FixStatus.REFUSED).label("refused"),
                   func.max(OpRun.finished_at).filter(OpRun.status.in_([FixStatus.POST_FAILED, FixStatus.FAILED]))
                   .label("last_failure")).group_by(OpRun.operation)
        with self.session() as s:
            return [dict(r) for r in s.execute(q).mappings()]

    # -- health (/metrics) ---------------------------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """What the operator's own alerts watch: incidents by status, the oldest one still waiting for triage, the
        oldest answer not yet mailed, and fixes by status."""
        def age(*where) -> ColumnElement[Any]:
            return func.coalesce(func.extract("epoch", func.now() - func.min(Incident.first_seen).filter(*where)), 0)
        with self.session() as s:
            out: dict[str, Any] = {
                "incidents": dict(s.execute(select(Incident.status, func.count()).group_by(Incident.status)).all()),
                "fixes": dict(s.execute(select(OpRun.status, func.count()).group_by(OpRun.status)).all())}
            waiting, unmailed = s.execute(select(
                # a re-run of an old incident isn't an alert waiting for triage
                age(Incident.status.in_(WAITING), Incident.attempt == 1),
                age(Incident.status.in_(ANSWERED), Incident.notified_at.is_(None)))).one()
        return out | {"oldest_waiting": waiting, "oldest_unmailed": unmailed}

    # -- reading (UI) --------------------------------------------------------------------------------------------------

    def list_incidents(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.session() as s:
            return [dict(r) for r in s.execute(text(
                """
                SELECT i.id, i.fingerprint, i.alertname, i.severity, i.labels, i.status, i.runbook, i.first_seen,
                       i.last_seen, i.alert_resolved_at, i.analysis_model, i.analysis_worker, i.notified_at,
                       left(i.analysis, 300) AS analysis_preview,
                       (SELECT status FROM op_runs o WHERE o.incident_id = i.id ORDER BY o.id DESC LIMIT 1)
                           AS fix_status,
                       r.n AS repeats, r.open AS repeats_open, r.last AS last_repeat
                FROM incidents i
                LEFT JOIN LATERAL (
                    SELECT count(*) AS n, count(*) FILTER (WHERE alert_resolved_at IS NULL) AS open,
                           max(first_seen) AS last
                    FROM incidents WHERE repeat_of = i.id
                ) r ON true
                WHERE i.status <> :repeat                      -- shown folded under the incident they repeat
                ORDER BY greatest(i.first_seen, r.last) DESC LIMIT :limit
                """), {"repeat": IncidentStatus.REPEAT.value, "limit": limit}).mappings()]

    def incident(self, incident_id: int) -> Incident:
        """get_incident for an id the caller just read from the database: a missing one is a bug, not a 404."""
        inc = self.get_incident(incident_id)
        if inc is None:
            raise LookupError(f"incident {incident_id} does not exist")
        return inc

    def get_incident(self, incident_id: int) -> Incident | None:
        """The incident with its evidence, turns and fixes."""
        with self.session() as s:
            return s.get(Incident, incident_id, options=[selectinload(Incident.evidence),
                                                         selectinload(Incident.turns), selectinload(Incident.fixes)])

    def get_incident_row(self, incident_id: int) -> Incident | None:
        """The incident alone."""
        with self.session() as s:
            return s.get(Incident, incident_id)

    def repeats(self, incident_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """The incidents that repeat this one (status `repeat`), newest first."""
        with self.session() as s:
            return [dict(r) for r in s.execute(
                select(Incident.id, Incident.labels, Incident.first_seen, Incident.last_seen,
                       Incident.alert_resolved_at).where(Incident.repeat_of == incident_id)
                .order_by(Incident.first_seen.desc()).limit(limit)).mappings()]

    def related(self, inc: Incident, limit: int = 50) -> Related:
        """What else was going on, from the incident list alone: any other alert around this one's time, nearest first,
        and how often this same problem fired before it. Measured from this incident, not from now, so a re-run of an
        old one sees its own moment. Alerts that came in after it are included if they are in by now."""
        args = {"at": inc.starts_at or inc.first_seen, "id": inc.id, "key": inc.problem_key,
                "window": RELATED_WINDOW_SECONDS, "close": RELATED_CLOSE_SECONDS, "limit": limit}
        with self.session() as s:
            nearby = s.execute(text(
                f"""
                SELECT id, alertname, labels, status, repeat_of,
                       extract(epoch FROM {_AT} - :at)::float AS offset_seconds,
                       extract(epoch FROM alert_resolved_at - {_AT})::float AS fired_seconds,
                       count(*) OVER () AS total,
                       count(*) FILTER (WHERE abs(extract(epoch FROM {_AT} - :at)) <= :close) OVER () AS close
                FROM incidents
                WHERE id <> :id AND problem_key IS DISTINCT FROM :key
                  AND abs(extract(epoch FROM {_AT} - :at)) <= :window
                ORDER BY abs(extract(epoch FROM {_AT} - :at)), id LIMIT :limit
                """), args).mappings().all()
            earlier = s.execute(text(
                f"""
                SELECT id, status, extract(epoch FROM :at - {_AT})::float AS age_seconds, count(*) OVER () AS total
                FROM incidents WHERE problem_key = :key AND id <> :id AND {_AT} < :at
                ORDER BY {_AT} DESC LIMIT 5
                """), args).mappings().all()
        return {"nearby": [Nearby(id=r["id"], alertname=r["alertname"], labels=r["labels"], status=r["status"],
                                  repeat_of=r["repeat_of"], offset_seconds=r["offset_seconds"],
                                  fired_seconds=r["fired_seconds"])
                           for r in nearby],
                "nearby_total": nearby[0]["total"] if nearby else 0,
                "nearby_close": nearby[0]["close"] if nearby else 0,
                "earlier": [Earlier(id=r["id"], status=r["status"], age_seconds=r["age_seconds"]) for r in earlier],
                "earlier_total": earlier[0]["total"] if earlier else 0}

    def runbook_usage(self) -> list[dict[str, Any]]:
        with self.session() as s:
            return [dict(r) for r in s.execute(
                select(Incident.runbook, func.count().label("incidents"),
                       func.max(Incident.first_seen).label("last_matched"))
                .where(Incident.runbook.is_not(None)).group_by(Incident.runbook)).mappings()]

    def needs_runbook(self, covered: Callable[[dict], bool] | None = None) -> list[dict[str, Any]]:
        """Alertnames that have fired with no runbook: the to-do list for writing runbooks (design §8.1).

        `covered(labels)` says whether a runbook matches an incident now. Those are left out: a runbook written after
        an incident fired covers the next one, so the alert is no longer on the to-do list. Checked per incident,
        because a runbook can match on labels other than the alertname."""
        out: dict[str, dict[str, Any]] = {}
        with self.session() as s:
            rows = s.execute(select(Incident.alertname, Incident.labels, Incident.first_seen,
                                    Incident.alert_resolved_at.is_(None).label("open"))
                             .where(Incident.runbook.is_(None))).all()
        for alertname, labels, first_seen, is_open in rows:
            if covered is not None and covered(labels or {}):
                continue
            a = out.setdefault(alertname, {"alertname": alertname, "incidents": 0, "last_seen": first_seen, "open": 0})
            a["incidents"] += 1
            a["open"] += is_open
            a["last_seen"] = max(a["last_seen"], first_seen)
        rows_out = sorted(out.values(), key=lambda a: a["last_seen"], reverse=True)
        return sorted(rows_out, key=lambda a: a["incidents"], reverse=True)


# -- library ---------------------------------------------------------------------------------------------------------

class LastGood:
    """library.Library's store for the last good version of each file."""

    def __init__(self, store: Store):
        self.store = store

    def save(self, file: str, content: bytes, rev: str) -> None:
        stmt = insert(LibraryLastGood).values(file=file, content=content, rev=rev)
        stmt = stmt.on_conflict_do_update(index_elements=[LibraryLastGood.file],
                                          set_={"content": stmt.excluded.content, "rev": stmt.excluded.rev,
                                                "saved_at": func.now()},
                                          where=LibraryLastGood.rev != stmt.excluded.rev)
        with self.store.session() as s:
            s.execute(stmt)

    def load(self, file: str) -> tuple[bytes, datetime] | None:
        with self.store.session() as s:
            row = s.get(LibraryLastGood, file)
            return (bytes(row.content), row.saved_at) if row else None
