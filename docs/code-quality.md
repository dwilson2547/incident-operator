# Code quality checklist

From a full read of `src/` on 2026-11-20. Tick an item off in the commit that fixes it. Behaviour doesn't change in
any of these: the test suite (with a test Postgres, see README: Develop) must pass before and after each one.

## 1. Kubernetes client

- [x] `K8s` moves out of `evidence.py` into `k8s.py`, so the evidence module is the read-only thing its docstring says
      it is (the client also posts, patches and deletes for probes and the executor).
- [x] One `_request()` instead of four copies of the URL, timeout, `verify` and auth header.
- [x] One `httpx.Client` for the process, not a new connection per call.
- [x] The service-account token is re-read when the file changes (the kubelet rotates it), and the file is closed.

## 2. Lint and types in CI

- [ ] `ruff check` in CI, with a config in `pyproject.toml`.
- [ ] `pyright` in CI (basic mode to start).

## 3. Private names used across modules

- [ ] `ops` calls `evidence._pod`, `_job`, `_node`, `_workload`: make them public.
- [ ] `fixes` and `executor` import `ops._fill`: make it public.
- [ ] `fixes` imports `triage._jsonable` inside a function: move it somewhere both can import at the top.

## 4. Duplication

- [ ] `_cap` is in both `evidence.py` and `ops.py`.
- [ ] The alert header is built in both `triage.build_messages` and `investigate.alert_block`.
- [ ] Node-by-`instance`-IP lookup is in both `evidence._node_by_instance` and `library.resolve_target`.
- [ ] Workload kind / plural / API group is spelled three ways (`evidence.collect`, `WORKLOAD_KINDS`, `ops._summary`).
- [ ] A stored evidence row is turned back into a `Result` in three places (triage twice, replay).
- [ ] Results are stored as evidence by both `Engine._store` and `fixes.store`.
- [ ] Durations (`s/m/h/d`) are parsed by both `ops._seconds` and `library.LIMIT`.
- [ ] `f"{type(e).__name__}: {e}"[:N]` is written out about six times, with different N.
- [ ] Prompt tokens are estimated as chars/3 in triage and chars/4 in library: one function, or say why they differ.

## 5. Strings for state

- [ ] Incident and fix statuses, turn sessions and evidence triggers become `StrEnum`s; the status lists repeated in
      `db.py` queries become named constants.
- [ ] Incident, turn and fix rows get `TypedDict`s, so a key typo is a type error.

## 6. The triage engine

- [ ] `Engine._runbook_turn_done` (~85 lines, four outcomes) split into one method per outcome.
- [ ] The no-runbook path of `start_new` becomes its own method, like `_start_runbook`.
- [ ] The no-runbook path submits before storing evidence, like the runbook path, instead of storing and then
      deleting it with raw SQL when the submit fails.
- [ ] No SQL outside `db.py` (`triage.py` has two queries).
- [ ] One helper for "submit the next turn; on failure log and leave it for the next pass" (written out five times).

## 7. Dispatch

- [ ] A fix verb is defined in one place (validation, kubectl command, executor action), not across `library`,
      `fixes` and `executor`.
- [ ] `Runner.run` looks the kind up in a table and fails on an unknown one, instead of falling through to logs.

## 8. Readability

- [ ] Nested ternaries and packed one-liners split into named steps: `Engine._add_turn`, `Engine.step`,
      `fixes.evaluate`, `investigate._span`, `Library.describe`.
- [ ] No function-level imports without a reason (`httpx` in six places, `json` re-imported, `uuid`).
- [ ] Stale "phase 1" comments and docstrings updated (`evidence.py`, `db.py`, schema column comments).
- [ ] `Config.from_env` built from the dataclass fields instead of eleven hand-written lines.

## 9. Schema

- [ ] `SCHEMA` becomes numbered migrations with a version row, so the `CREATE TABLE`s show the real tables instead of
      being followed by a growing list of `ALTER ... IF NOT EXISTS`.
