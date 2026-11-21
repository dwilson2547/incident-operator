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

- [x] `ruff check` in CI, with a config in `pyproject.toml` (pyflakes, pycodestyle at 120 columns, import order,
  bugbear, pyupgrade, simplify, logging).
- [x] `pyright` in CI (basic mode to start), over `src` and `tests`, with no errors. The fakes now subclass what they
  stand in for. Tests leave two optional-value rules to the test run (see `pyproject.toml`).
- [ ] Later: pyright `standard` mode, and `ruff format` once the remaining manual wrapping settles.

## 3. Private names used across modules

- [x] `ops` calls `evidence._pod`, `_job`, `_node`, `_workload`: now `evidence.for_pod` etc., with docstrings.
- [x] `fixes` and `executor` import `ops._fill`: now `ops.fill`.
- [x] `fixes` imports `triage._jsonable` inside a function: `db.add_evidence` now decides what it can store as JSON,
  so neither caller needs it.

## 4. Duplication

- [x] `_cap` is in both `evidence.py` and `ops.py`: one `text.cap`.
- [x] The alert header is built in both `triage.build_messages` and `investigate.alert_block`: triage calls
  `alert_block`.
- [x] Node-by-`instance`-IP lookup is in both `evidence._node_by_instance` and `library.resolve_target`: one
  `k8s.node_by_instance`.
- [x] Workload kind / plural / API group is spelled three ways (`evidence.collect`, `WORKLOAD_KINDS`, `ops._summary`):
  one `evidence.WORKLOADS`, and `for_workload` takes the object kind.
- [x] A stored evidence row is turned back into a `Result` in three places (triage twice, replay):
  `Result.from_evidence`.
- [x] Results are stored as evidence by both `Engine._store` and `fixes.store`: one `db.add_results`.
- [x] Durations (`s/m/h/d`) are parsed by both `ops._seconds` and `library.LIMIT`: one `library.seconds`.
- [x] `f"{type(e).__name__}: {e}"[:N]` is written out about six times, with different N: `text.error_text(e, N)`;
  the limits stay per caller (an evidence item, an operation, a fix run).
- [x] Prompt tokens are estimated as chars/3 in triage and chars/4 in library: they differ on purpose (a prompt
  over the context fails, a long runbook text only warns), now said in `library.tokens`.

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
- [x] No defaults in code: every setting comes from the chart (or `local.env`), read through `settings.Env`, all
  missing ones reported together at start-up; `tests/test_settings.py` checks the rendered chart covers them.

## 9. Schema

- [ ] `SCHEMA` becomes numbered migrations with a version row, so the `CREATE TABLE`s show the real tables instead of
      being followed by a growing list of `ALTER ... IF NOT EXISTS`.
