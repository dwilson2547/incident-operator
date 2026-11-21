"""The tables, as SQLAlchemy models: the schema's one definition (migrations/ is generated from it, and
tests/test_schema.py holds the two together).

Columns are in the order the tables were built in, so a dump of a database made from these matches one of the
production database exactly. State columns read back as the enums in states.py; their CHECK lists are made from those
enums, so adding a value is a schema change that needs a migration (tests/test_schema.py fails until there is one:
`alembic check` doesn't compare CHECKs).
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Dialect,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    TypeDecorator,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .states import UNDER_WAY, FixStatus, IncidentStatus, Session, Trigger, TurnStatus


class EnumText[E: StrEnum](TypeDecorator[E]):
    """A StrEnum stored as plain text (no Postgres enum type): written as its value, read back as the enum."""

    impl = Text
    cache_ok = True

    def __init__(self, enum: type[E]):
        super().__init__()
        self.enum = enum

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        return None if value is None else self.enum(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        return None if value is None else self.enum(value)


def _one_of(column: str, values: Iterable[StrEnum]) -> str:
    """`status IN ('new', 'triaging')`: a CHECK or partial-index condition."""
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


NOW = text("now()")
TS = DateTime(timezone=True)
JSON = JSONB(none_as_null=True)     # None is SQL NULL, as it always was; JSONB's default would store JSON `null`


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON, datetime: TS, str: Text}

    def to_dict(self) -> dict[str, Any]:
        """Every column, in table order: what `SELECT *` gave the API before the models."""
        return {c.key: getattr(self, c.key) for c in self.__mapper__.column_attrs}


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fingerprint: Mapped[str]
    alertname: Mapped[str]
    severity: Mapped[str | None]
    labels: Mapped[dict[str, Any]]
    annotations: Mapped[dict[str, Any]]
    generator_url: Mapped[str | None]
    status: Mapped[IncidentStatus] = mapped_column(EnumText(IncidentStatus), server_default=IncidentStatus.NEW.value)
    runbook: Mapped[str | None]
    starts_at: Mapped[datetime | None]
    first_seen: Mapped[datetime] = mapped_column(server_default=NOW)
    last_seen: Mapped[datetime] = mapped_column(server_default=NOW)
    alert_resolved_at: Mapped[datetime | None]
    analysis: Mapped[str | None]                       # the model's answer
    analysis_model: Mapped[str | None]                 # which model answered (gpu-scheduler ran_model)
    analysis_worker: Mapped[str | None]
    notified_at: Mapped[datetime | None]
    error: Mapped[str | None]
    problem_key: Mapped[str | None]                    # db.problem_key(): what makes two alerts the same problem
    repeat_of: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("incidents.id"))
    runbook_rev: Mapped[str | None]
    target: Mapped[dict[str, Any] | None]
    findings: Mapped[dict[str, Any] | None]
    advice: Mapped[dict[str, Any] | None]
    attempt: Mapped[int] = mapped_column(server_default="1")
    rerun_model: Mapped[str | None]
    rerun_by: Mapped[str | None]
    earlier_attempts: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"))  # answers a re-run replaced

    # Loaded only when asked for (db.get_incident): evidence and turns are large, and most reads don't need them.
    evidence: Mapped[list[Evidence]] = relationship(order_by="Evidence.id", lazy="raise", passive_deletes=True)
    turns: Mapped[list[ModelTurn]] = relationship(order_by="ModelTurn.id", lazy="raise", passive_deletes=True)
    fixes: Mapped[list[OpRun]] = relationship(order_by="OpRun.id", lazy="raise", passive_deletes=True)

    __table_args__ = (
        CheckConstraint(_one_of("status", IncidentStatus), name="incidents_status_check"),
        # One open incident per fingerprint.
        Index("incidents_open_fingerprint", "fingerprint", unique=True,
              postgresql_where=text("alert_resolved_at IS NULL")),
        Index("incidents_status", "status",
              postgresql_where=text(_one_of("status", (IncidentStatus.NEW, IncidentStatus.TRIAGING)))),
        Index("incidents_first_seen", text("first_seen DESC")),
        Index("incidents_problem_key", "problem_key", text("first_seen DESC")),
        Index("incidents_repeat_of", "repeat_of", postgresql_where=text("repeat_of IS NOT NULL")),
    )


class Evidence(Base):
    """What was read for an incident: generic collection, a runbook's precursors, what the model asked for, and a
    fix's pre/post checks (trigger)."""
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("incidents.id", ondelete="CASCADE"))
    kind: Mapped[str]                                  # pod | events | logs | job | node | an operation's name
    source: Mapped[str]                                # what was read, e.g. "pod gyopart/ryans-…"
    content: Mapped[str | None]
    truncated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    error: Mapped[str | None]
    collected_at: Mapped[datetime] = mapped_column(server_default=NOW)
    trigger: Mapped[Trigger] = mapped_column(EnumText(Trigger), server_default=Trigger.GENERIC.value)
    operation: Mapped[str | None]
    round: Mapped[int | None]
    data: Mapped[Any | None] = mapped_column(JSON)    # the result's structured form, when it is JSON

    __table_args__ = (Index("evidence_incident", "incident_id"),)


class ModelTurn(Base):
    """Exactly what each model call saw and returned, for replay (design §9.5)."""
    __tablename__ = "model_turns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("incidents.id", ondelete="CASCADE"))
    session: Mapped[Session] = mapped_column(EnumText(Session))
    prompt_version: Mapped[str]
    model: Mapped[str]                                 # requested
    fallback: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    gpu_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[TurnStatus] = mapped_column(EnumText(TurnStatus), server_default=TurnStatus.SUBMITTED.value)
    messages: Mapped[list[Any]]
    result: Mapped[dict[str, Any] | None]
    ran_model: Mapped[str | None]
    worker: Mapped[str | None]
    error: Mapped[str | None]
    submitted_at: Mapped[datetime] = mapped_column(server_default=NOW)
    finished_at: Mapped[datetime | None]
    turn: Mapped[int] = mapped_column(server_default="1")
    json_schema: Mapped[dict[str, Any] | None]
    output: Mapped[dict[str, Any] | None]              # the parsed JSON reply
    attempt: Mapped[int] = mapped_column(server_default="1")

    __table_args__ = (
        CheckConstraint(_one_of("status", TurnStatus), name="model_turns_status_check"),
        Index("model_turns_incident", "incident_id"),
        Index("model_turns_pending", "status", postgresql_where=text(f"status = '{TurnStatus.SUBMITTED}'")),
    )


class OpRun(Base):
    """One proposed mutating operation (a fix, design §7) and what became of it. `notified` is the status last mailed,
    so each outcome is mailed once."""
    __tablename__ = "op_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("incidents.id", ondelete="CASCADE"))
    operation: Mapped[str]
    op_rev: Mapped[str]
    params: Mapped[dict[str, Any]]
    autonomy: Mapped[str]
    status: Mapped[FixStatus] = mapped_column(EnumText(FixStatus))
    command: Mapped[str | None]                        # the kubectl equivalent
    proposed_at: Mapped[datetime] = mapped_column(server_default=NOW)
    decided_by: Mapped[str | None]
    decided_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    post_due_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    checks: Mapped[str | None]                         # one line per pre/post check: pass/FAIL, expectation, value seen
    result: Mapped[str | None]                         # what the executor did
    error: Mapped[str | None]
    notified: Mapped[FixStatus | None] = mapped_column(EnumText(FixStatus))

    __table_args__ = (
        CheckConstraint(_one_of("status", FixStatus), name="op_runs_status_check"),
        Index("op_runs_incident", "incident_id"),
        Index("op_runs_active", "status", postgresql_where=text(_one_of("status", (FixStatus.PROPOSED, *UNDER_WAY)))),
    )


class LibraryLastGood(Base):
    """The last version of each runbook and operation file that loaded (library.Library), so a rejected file falls
    back to it even after a restart."""
    __tablename__ = "library_last_good"

    file: Mapped[str] = mapped_column(primary_key=True)    # runbooks/<name>.md | operations/<name>.yaml
    content: Mapped[bytes] = mapped_column(LargeBinary)
    rev: Mapped[str]
    saved_at: Mapped[datetime] = mapped_column(server_default=NOW)
