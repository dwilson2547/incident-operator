"""Fixes: mutating operations from proposal to verified result (docs/design.md §7), on the controller's side.

    remediation proposes  ->  suggested (autonomy suggest: shown with its kubectl command, nothing runs)
                          ->  proposed  -> [Approve] -> approved -> pre-checks + limits -> ready | refused
                                        -> [Reject]  -> rejected
    executor (executor.py) takes ready -> running -> settling (or failed)
    after `settle`: post-checks -> succeeded | post_failed

The controller does everything except the change itself: it holds the read and probe rights the checks need, and the
executor holds only the generated rights for the verbs. Pre-checks run at approval time against fresh results, not
the investigation's. An operation whose file changed since it was proposed is refused: what was approved is what runs.
"""
from __future__ import annotations

import json
import logging
import re
import shlex

from . import db
from .library import EXPECT, Check, Operation
from .ops import ParamError, Result, Runner, SAFE_VALUE, _fill, resolve_params

log = logging.getLogger("incident_operator.fixes")

READY_TIMEOUT_SECONDS = 600       # a ready run the executor hasn't taken by then fails (is the executor running?)


def evaluate(expect: str, data) -> tuple[bool, str]:
    """Check one expectation against a result's structured data. Returns (ok, what was seen)."""
    from .ops import dig
    m = EXPECT.match(expect)
    path, op, raw = m.group(1), m.group(2), m.group(3)
    want = json.loads(raw)
    have = dig(data, path) if data is not None else None
    seen = f"{path} = {json.dumps(have)}"
    if op in ("==", "!="):                    # strict: 1 is not true, "0" is not 0
        same = have == want and isinstance(have, bool) == isinstance(want, bool) and type(have) in (type(want), int, float)
        return same == (op == "=="), seen
    if not isinstance(have, (int, float)) or isinstance(have, bool) or not isinstance(want, (int, float)):
        return False, seen + " (not a number)"
    return {">": have > want, ">=": have >= want, "<": have < want, "<=": have <= want}[op], seen


def fix_params(op: Operation, target: dict | None, labels: dict) -> dict[str, str]:
    """Parameters for a fix: from the target and the alert only (never results, never the model), then the
    namespace checked against the operation's list."""
    params = resolve_params(op, target, labels, {})
    ns = _fill(op.run["namespace"], params)
    if ns not in op.run["namespaces"]:
        raise ParamError(f"namespace {ns!r} isn't one this operation may act in ({', '.join(op.run['namespaces'])})")
    return params


def command(op: Operation, params: dict) -> str:
    """The kubectl equivalent, for the page (and for a human to run at `suggest`)."""
    run = _fill(op.run, params)
    q = shlex.quote
    if run["verb"] == "DeletePods":
        sel = ",".join(f"{k}={v}" for k, v in run["selector"].items())
        node = f" --field-selector spec.nodeName={q(run['node'])}" if run.get("node") else ""
        return f"kubectl -n {q(run['namespace'])} delete pod -l {q(sel)}{node}"
    if run["verb"] == "CreateJobFrom":
        return (f"kubectl -n {q(run['namespace'])} create job --from=cronjob/{q(run['cronjob'])} "
                f"{q(run['cronjob'][:40])}-manual-$(date +%s)")
    return json.dumps(run)


def propose(conn, incident: dict, runbook, op: Operation) -> int:
    """Record a proposed fix. A fix whose parameters can't be resolved is recorded as refused, with why."""
    autonomy = runbook.fixes.get(op.name, op.autonomy)
    try:
        params = fix_params(op, incident.get("target"), incident["labels"])
    except ParamError as e:
        return db.add_op_run(conn, incident["id"], op.name, op.rev, {}, autonomy, "refused", error=str(e))
    return db.add_op_run(conn, incident["id"], op.name, op.rev, params, autonomy,
                         "suggested" if autonomy == "suggest" else "proposed", command=command(op, params))


def over_limit(conn, op: Operation, params: dict) -> str | None:
    n, secs = op.limits["perTarget"]
    if db.op_runs_since(conn, op.name, secs, params) >= n:
        return f"limit: {op.name} already ran {n} time(s) on this target in the last {secs // 60} minutes"
    n, secs = op.limits["global"]
    if db.op_runs_since(conn, op.name, secs) >= n:
        return f"limit: {op.name} already ran {n} time(s) in the last {secs // 60} minutes"
    return None


def run_checks(runner: Runner, library, checks: list[Check], incident: dict) -> tuple[bool, list[Result], list[str]]:
    """Run each check's operation fresh and test its expectation. Returns (all passed, results, one line per check)."""
    results, lines, ok, cache = [], [], True, {}
    for c in checks:
        if c.op not in cache:
            cache[c.op] = runner.run(library.operations[c.op], incident.get("target"), incident["labels"], {})
            results.append(cache[c.op])
        r = cache[c.op]
        if r.error:
            passed, seen = False, f"could not run: {r.error}"
        else:
            passed, seen = evaluate(c.expect, r.data)
        ok &= passed
        lines.append(f"{'pass' if passed else 'FAIL'}: {c.op} expects {c.expect}; {seen}")
    return ok, results, lines


def store(conn, incident_id: int, results: list[Result], trigger: str) -> None:
    from .triage import _jsonable
    for r in results:
        db.add_evidence(conn, incident_id, r.operation, r.source, r.text, r.truncated, r.error,
                        trigger=trigger, operation=r.operation, data=_jsonable(r.data))


def step(conn, library, runner: Runner) -> int:
    """One pass: pre-check approved runs, post-check settled ones, time out runs the executor never took."""
    n = 0
    for run in db.op_runs_with_status(conn, "approved"):
        n += 1
        op = library.operations.get(run["operation"])
        inc = db.get_incident(conn, run["incident_id"])
        if op is None or not op.mutating:
            db.update_op_run(conn, run["id"], "refused", error="the operation is no longer loaded")
            continue
        if op.rev != run["op_rev"]:
            db.update_op_run(conn, run["id"], "refused",
                             error=f"{op.name} changed since it was proposed ({run['op_rev']} -> {op.rev})")
            continue
        if why := over_limit(conn, op, run["params"]):
            db.update_op_run(conn, run["id"], "refused", error=why)
            continue
        ok, results, lines = run_checks(runner, library, op.pre, inc) if op.pre else (True, [], [])
        store(conn, inc["id"], results, "pre")
        if not ok:
            db.update_op_run(conn, run["id"], "refused", checks="\n".join(lines),
                             error="a pre-check failed: the situation isn't what the fix is for (any more)")
            continue
        db.update_op_run(conn, run["id"], "ready", checks="\n".join(lines))
        log.info("fix %s (%s) for incident %s: pre-checks passed, ready", run["id"], op.name, inc["id"])
    for run in db.op_runs_due_for_post(conn):
        n += 1
        op = library.operations.get(run["operation"])
        inc = db.get_incident(conn, run["incident_id"])
        if op is None:
            db.update_op_run(conn, run["id"], "post_failed", error="the operation is no longer loaded: not verified")
            continue
        ok, results, lines = run_checks(runner, library, op.post, inc)
        store(conn, inc["id"], results, "post")
        prior = run.get("checks") or ""
        db.update_op_run(conn, run["id"], "succeeded" if ok else "post_failed",
                         checks=(prior + "\n" if prior else "") + "\n".join(lines),
                         error=None if ok else "a post-check failed: the fix didn't take; see the runbook's escalation")
        log.info("fix %s (%s) for incident %s: %s", run["id"], op.name, inc["id"], "succeeded" if ok else "post_failed")
    n += db.expire_ready(conn, READY_TIMEOUT_SECONDS)
    return n


def valid_params(op: Operation, params: dict) -> dict[str, str]:
    """For the executor: the stored parameters are exactly the operation's, each a plain name, and the namespace is
    one of the operation's. The executor trusts nothing it reads from the database beyond that."""
    if set(params) != set(op.params):
        raise ParamError(f"parameters {sorted(params)} don't match the operation's {sorted(op.params)}")
    for k, v in params.items():
        if not isinstance(v, str) or not SAFE_VALUE.match(v):
            raise ParamError(f"parameter {k} isn't a plain name")
    if _fill(op.run["namespace"], params) not in op.run["namespaces"]:
        raise ParamError("namespace isn't one this operation may act in")
    if re.search(r"\{\w+\}", json.dumps(_fill(op.run, params))):
        raise ParamError("unfilled parameter")
    return params
