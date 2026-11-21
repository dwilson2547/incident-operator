"""The values the operator's state columns take, and the groups of them that queries ask for.

They are StrEnums, so a row read back from Postgres (plain text) compares equal to them and psycopg stores their
value. The schema's CHECK constraints (db.SCHEMA) list the same values; tests/test_states.py keeps the two in step.
"""
from __future__ import annotations

from enum import StrEnum


class IncidentStatus(StrEnum):
    NEW = "new"                          # received, not yet looked at
    TRIAGING = "triaging"                # one-shot triage sent to the model (no runbook)
    INVESTIGATING = "investigating"      # a runbook's investigation, possibly over several turns
    ADVISING = "advising"                # the remediation turn
    ANALYSED = "analysed"                # answered
    TRIAGE_FAILED = "triage_failed"      # the model or the submit failed, or the prompt was too large
    REPEAT = "repeat"                    # same problem as a recent incident: its answer stands
    SKIPPED = "skipped"                  # over the hourly cap, or resolved long before its turn
    RERUN = "rerun"                      # someone asked for a new answer, with a model they picked


class FixStatus(StrEnum):
    """An op_runs row: a proposed fix, from proposal to its post-check (design §7)."""
    SUGGESTED = "suggested"              # autonomy suggest: shown, never run
    PROPOSED = "proposed"                # waiting for a decision
    APPROVED = "approved"                # decided, pre-checks not yet run
    REJECTED = "rejected"
    REFUSED = "refused"                  # the operator wouldn't: bad params, a limit, a failed pre-check
    READY = "ready"                      # pre-checks passed, waiting for the executor
    RUNNING = "running"
    SETTLING = "settling"                # done, waiting out post.settle before the post-checks
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    POST_FAILED = "post_failed"          # ran, but the post-checks didn't hold
    SUPERSEDED = "superseded"            # still waiting when the incident was re-run


class TurnStatus(StrEnum):
    SUBMITTED = "submitted"
    DONE = "done"
    FAILED = "failed"


class Session(StrEnum):
    """Which conversation a model turn belongs to."""
    TRIAGE = "triage"
    INVESTIGATION = "investigation"
    REMEDIATION = "remediation"


class Trigger(StrEnum):
    """Why an evidence row was collected."""
    GENERIC = "generic"                  # the alert's own objects, no runbook
    PRECURSOR = "precursor"              # a runbook's precursors, before the first turn
    REQUESTED = "requested"              # asked for by the model
    PRE = "pre"                          # a fix's pre-check
    POST = "post"                        # a fix's post-check


# Incidents
WAITING = (IncidentStatus.NEW, IncidentStatus.TRIAGING, IncidentStatus.INVESTIGATING, IncidentStatus.ADVISING)
ANSWERED = (IncidentStatus.ANALYSED, IncidentStatus.TRIAGE_FAILED)          # mailed once; what a re-run may replace
SENT_TO_MODEL = (IncidentStatus.TRIAGING, IncidentStatus.INVESTIGATING, IncidentStatus.ADVISING, *ANSWERED,
                 IncidentStatus.RERUN)

# Fixes
UNDER_WAY = (FixStatus.APPROVED, FixStatus.READY, FixStatus.RUNNING, FixStatus.SETTLING)
RAN = (FixStatus.SUCCEEDED, FixStatus.POST_FAILED, FixStatus.FAILED)
REACHED_CLUSTER = (FixStatus.READY, FixStatus.RUNNING, FixStatus.SETTLING, *RAN)     # what a fix's limits count
FINAL = (FixStatus.REFUSED, FixStatus.REJECTED, *RAN, FixStatus.SUPERSEDED)

CHECK_TRIGGERS = (Trigger.PRE, Trigger.POST)          # a fix's own checks, not evidence about the incident
