"""The triage engine: alert -> evidence -> model -> suggestion, as a loop of idempotent passes over the database, so a
restart picks up where it stopped:
1. `new` incidents (after the repeat and rate brakes): if a runbook matches (design §6), derive the target, run its
   precursors and submit the first investigation turn -> `investigating`; otherwise the no-runbook triage (§6.5):
   generic evidence and one call -> `triaging`
2. submitted turns: poll gpu-scheduler. A triage answer -> `analysed`. An investigation reply either asks for checks
   from its menu (they run, the next turn is submitted) or finishes with findings (the remediation turn is submitted
   -> `advising`). Remediation advice -> `analysed`. A failed job -> `triage_failed`
3. fixes (fixes.py): approved ones are pre-checked and handed to the executor; executed ones are post-checked
4. analysed or failed incidents not yet mailed, and outcomes of approved fixes: send the email

A follow-up turn is submitted before anything about it is recorded: if gpu-scheduler is unreachable the finished
turn stays unprocessed and the next pass tries again, re-running only read-only checks.

The model is a gpu-scheduler model with a fallback (design §9.2): qwen35-9b on the 3080, gemma4-26b-a4b on the Mac
after `fallback_after_seconds`, so a busy GPU doesn't hold up triage. Every prompt is stored exactly as sent.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db, evidence, fixes, investigate
from .library import Library, resolve_target
from .ops import Result, Runner

log = logging.getLogger("incident_operator.triage")

PROMPT_VERSION = "triage-v1"
SYSTEM_PROMPT = ("You are a triage agent, users will provide you with stack traces, error logs, or grafana metrics "
                 "and your job is to identify where the failure happened and advise the user")
ANSWER_FORMAT = """Answer in plain text with these four headings, briefly:
Where it failed: the component, and the exact line, object or condition from the evidence.
Likely cause: what the evidence shows; say so when it doesn't show enough.
What to do: concrete next steps, in order. Read-only checks first.
Will it fix itself: whether waiting or a retry is likely to help."""


@dataclass
class Config:
    gpu_url: str = "http://gpu-scheduler-api.gpu-scheduler.svc"
    gpu_token: str = ""
    model: str = "qwen35-9b"
    fallback: list[str] = field(default_factory=lambda: ["gemma4-26b-a4b"])
    fallback_after_seconds: int = 60
    # The fallback (gemma4-26b-a4b on the Mac) has a 16k context in gpu-scheduler's values: a longer prompt would fail
    # there, so it is sent to the main model only. Estimated at 3 characters a token (logs tokenize worse than prose).
    fallback_max_prompt_tokens: int = 12000
    deadline_seconds: int = 900
    max_new_tokens: int = 1024
    investigate_max_new_tokens: int = 1536     # one JSON reply per turn (768 cut long 'why's mid-JSON)
    primer_path: str = "/etc/incident-operator/primer.md"
    repeat_window_seconds: int = 6 * 3600      # same problem key triaged this recently -> `repeat`, no new report
    max_per_hour: int = 12                     # triage submissions; the rest wait
    skip_resolved_after_seconds: int = 3600    # a waiting incident resolved this long ago -> `skipped`

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        fb = e.get("TRIAGE_FALLBACK", "gemma4-26b-a4b")
        return cls(gpu_url=e.get("GPU_SCHEDULER_URL", cls.gpu_url), gpu_token=e.get("GPU_SCHEDULER_TOKEN", ""),
                   model=e.get("TRIAGE_MODEL", cls.model), fallback=[m for m in fb.split(",") if m.strip()],
                   fallback_after_seconds=int(e.get("TRIAGE_FALLBACK_AFTER_SECONDS", cls.fallback_after_seconds)),
                   fallback_max_prompt_tokens=int(e.get("TRIAGE_FALLBACK_MAX_PROMPT_TOKENS",
                                                        cls.fallback_max_prompt_tokens)),
                   deadline_seconds=int(e.get("TRIAGE_DEADLINE_SECONDS", cls.deadline_seconds)),
                   max_new_tokens=int(e.get("TRIAGE_MAX_NEW_TOKENS", cls.max_new_tokens)),
                   investigate_max_new_tokens=int(e.get("TRIAGE_INVESTIGATE_MAX_NEW_TOKENS",
                                                        cls.investigate_max_new_tokens)),
                   primer_path=e.get("PRIMER_PATH", cls.primer_path),
                   repeat_window_seconds=int(e.get("TRIAGE_REPEAT_WINDOW_SECONDS", cls.repeat_window_seconds)),
                   max_per_hour=int(e.get("TRIAGE_MAX_PER_HOUR", cls.max_per_hour)),
                   skip_resolved_after_seconds=int(e.get("TRIAGE_SKIP_RESOLVED_AFTER_SECONDS",
                                                         cls.skip_resolved_after_seconds)))

    def primer(self) -> str:
        """The environment primer (design §9.4): read on every use, so a ConfigMap update needs no restart."""
        p = Path(self.primer_path)
        return p.read_text().strip() if p.exists() else ""


def build_messages(incident: dict, evidence_text: str, primer: str) -> list[dict]:
    system = SYSTEM_PROMPT + ("\n\n" + primer if primer else "") + "\n\n" + ANSWER_FORMAT
    labels = incident["labels"]
    ann = incident["annotations"] or {}
    shown = {k: v for k, v in labels.items() if k not in ("alertname", "severity", "prometheus")}
    lines = [f"Alert: {incident['alertname']} (severity: {incident.get('severity') or 'unknown'}), firing since "
             f"{incident.get('starts_at') or incident['first_seen']}",
             "Labels: " + ", ".join(f"{k}={v}" for k, v in sorted(shown.items()))]
    for key in ("summary", "description", "message"):
        if ann.get(key):
            lines.append(f"{key.capitalize()}: {ann[key]}")
    user = "\n".join(lines) + "\n\nEvidence collected from the cluster:\n\n" + evidence_text
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def fits_fallback(messages: list[dict], cfg: "Config") -> bool:
    return sum(len(m.get("content") or "") for m in messages) / 3 <= cfg.fallback_max_prompt_tokens


class GpuScheduler:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _h(self):
        return {"Authorization": f"Bearer {self.cfg.gpu_token}"}

    def submit(self, messages: list[dict], json_schema: dict | None = None, max_new_tokens: int | None = None) -> str:
        import httpx
        item: dict = {"messages": messages, "max_new_tokens": max_new_tokens or self.cfg.max_new_tokens}
        if json_schema is not None:
            item["json_schema"] = json_schema
        body = {"model": self.cfg.model, "inputs": [item],
                "deadline_seconds": self.cfg.deadline_seconds, "max_attempts": 2}
        if self.cfg.fallback and fits_fallback(messages, self.cfg):
            body |= {"fallback": self.cfg.fallback, "fallback_after_seconds": self.cfg.fallback_after_seconds}
        r = httpx.post(self.cfg.gpu_url + "/v1/jobs", json=body, headers=self._h(), timeout=30)
        r.raise_for_status()
        return r.json()["job_ids"][0]

    def job(self, job_id) -> dict:
        import httpx
        r = httpx.get(f"{self.cfg.gpu_url}/v1/jobs/{job_id}", headers=self._h(), timeout=30)
        r.raise_for_status()
        return r.json()


class Engine:
    def __init__(self, conn, cfg: Config, k8s, gpu: GpuScheduler, notifier=None, library: Library | None = None,
                 runner: Runner | None = None):
        self.conn, self.cfg, self.k8s, self.gpu, self.notifier = conn, cfg, k8s, gpu, notifier
        self.library, self.runner = library, runner or Runner(k8s)
        self.stopping = False
        self.last_pass: float | None = None      # time of the last pass that completed, for /metrics

    def _fallback(self, messages: list[dict]) -> list[str] | None:
        """The fallback models this prompt is offered to, as recorded with the turn."""
        return (self.cfg.fallback or None) if fits_fallback(messages, self.cfg) else None

    def step(self) -> int:
        """One pass of each stage. Returns how many things moved."""
        if self.library:
            self.library.refresh()
        return self.start_new() + self.poll() + (fixes.step(self.conn, self.library, self.runner) if self.library
                                                 else 0) + self.notify()

    def start_new(self) -> int:
        n = 0
        budget = self.cfg.max_per_hour - db.triages_since(self.conn, 3600)
        for inc in db.incidents_with_status(self.conn, "new", limit=200):
            prev = db.last_triaged(self.conn, inc["problem_key"], self.cfg.repeat_window_seconds, inc["id"])
            if prev:
                orig = self.conn.execute("SELECT runbook FROM incidents WHERE id = %s", (prev,)).fetchone()
                db.set_status(self.conn, inc["id"], "repeat", repeat_of=prev, runbook=orig["runbook"])
                log.info("incident %s (%s): repeat of %s, not triaged", inc["id"], inc["alertname"], prev)
                n += 1
                continue
            resolved = inc["alert_resolved_at"]
            if resolved and (datetime.now(timezone.utc) - resolved).total_seconds() > self.cfg.skip_resolved_after_seconds:
                rb = self.library.match(inc["labels"]) if self.library else None
                db.set_status(self.conn, inc["id"], "skipped", runbook=rb.name if rb else None,
                              error="Resolved before its turn: the hourly triage limit was reached.")
                log.info("incident %s (%s): skipped, resolved while waiting", inc["id"], inc["alertname"])
                n += 1
                continue
            if budget <= 0:                           # waits for the next pass with room
                continue
            runbook = self.library.match(inc["labels"]) if self.library else None
            if runbook:
                if self._start_runbook(inc, runbook):
                    budget -= 1
                    n += 1
                continue
            items = evidence.collect(self.k8s, inc["labels"]) if self.k8s else []
            for it in items:
                db.add_evidence(self.conn, inc["id"], it.kind, it.source, it.content, it.truncated, it.error)
            messages = build_messages(inc, evidence.render(items), self.cfg.primer())
            try:
                job_id = self.gpu.submit(messages)
            except Exception as e:                    # gpu-scheduler unreachable: stays `new`, retried next pass
                log.warning("submit for incident %s failed: %s", inc["id"], e)
                self.conn.execute("DELETE FROM evidence WHERE incident_id = %s", (inc["id"],))
                continue
            db.add_turn(self.conn, inc["id"], session="triage", prompt_version=PROMPT_VERSION, model=self.cfg.model,
                        fallback=self._fallback(messages), messages=messages, gpu_job_id=job_id)
            db.set_status(self.conn, inc["id"], "triaging")
            budget -= 1
            log.info("incident %s (%s): %d evidence items, job %s", inc["id"], inc["alertname"], len(items), job_id)
            n += 1
        return n

    def poll(self) -> int:
        n = 0
        for turn in db.pending_turns(self.conn):
            try:
                job = self.gpu.job(str(turn["gpu_job_id"]))          # a UUID from Postgres
            except Exception as e:
                log.warning("poll of job %s failed: %s", turn["gpu_job_id"], e)
                continue
            if job["status"] == "done" and turn["session"] != "triage":
                n += self._runbook_turn_done(turn, job)
            elif job["status"] == "done":
                res = job.get("result") or {}
                db.finish_turn(self.conn, turn["id"], status="done", result=res, ran_model=job.get("ran_model"),
                               worker=job.get("worker"))
                db.set_status(self.conn, turn["incident_id"], "analysed", analysis=(res.get("text") or "").strip(),
                              analysis_model=job.get("ran_model") or turn["model"], analysis_worker=job.get("worker"))
                n += 1
            elif job["status"] in ("failed", "cancelled"):
                db.finish_turn(self.conn, turn["id"], status="failed", error=job.get("error"))
                db.set_status(self.conn, turn["incident_id"], "triage_failed", error=job.get("error"))
                n += 1
        return n

    # -- runbook path (design §6) -------------------------------------------------------------------------------------

    def _run_ops(self, names: list[str], target, labels, earlier: dict[str, Result]) -> list[Result]:
        out = []
        for name in names:
            r = self.runner.run(self.library.operations[name], target, labels, earlier)
            earlier[name] = r
            out.append(r)
        return out

    def _store(self, incident_id: int, results: list[Result], trigger: str, round: int | None = None) -> None:
        for r in results:
            db.add_evidence(self.conn, incident_id, r.operation, r.source, r.text, r.truncated, r.error,
                            trigger=trigger, operation=r.operation, round=round, data=_jsonable(r.data))

    def _start_runbook(self, inc: dict, rb) -> bool:
        try:
            target = resolve_target(rb, inc["labels"], self.k8s)
        except Exception as e:                        # the API was unreachable: operations needing it say so
            log.warning("incident %s: target resolution failed: %s", inc["id"], e)
            target = None
        precursors = self._run_ops(rb.precursors, target, inc["labels"], {})
        menu, menu_txt = investigate.menu_text(rb, self.library.operations)
        messages = investigate.first_messages(inc, rb, target, precursors, menu_txt, self.cfg.primer())
        schema = investigate.investigation_schema(menu, final=investigate.MAX_ROUNDS <= 1)
        try:
            job_id = self.gpu.submit(messages, schema, self.cfg.investigate_max_new_tokens)
        except Exception as e:                        # stays `new`; precursors run again next pass (read-only)
            log.warning("submit for incident %s failed: %s", inc["id"], e)
            return False
        self._store(inc["id"], precursors, "precursor")
        db.add_turn(self.conn, inc["id"], session="investigation", prompt_version=investigate.PROMPT_VERSION,
                    model=self.cfg.model, fallback=self._fallback(messages), messages=messages, gpu_job_id=job_id,
                    turn=1, json_schema=schema)
        db.set_status(self.conn, inc["id"], "investigating", runbook=rb.name, runbook_rev=rb.rev, target=target)
        log.info("incident %s (%s): runbook %s, target %s, %d precursors, job %s", inc["id"], inc["alertname"],
                 rb.name, target, len(precursors), job_id)
        return True

    def _runbook_turn_done(self, turn: dict, job: dict) -> int:
        res = job.get("result") or {}
        output = res.get("json")
        done = dict(status="done", result=res, ran_model=job.get("ran_model"), worker=job.get("worker"), output=output)
        inc = db.get_incident(self.conn, turn["incident_id"])
        if not isinstance(output, dict):
            db.finish_turn(self.conn, turn["id"], **done)
            db.set_status(self.conn, inc["id"], "triage_failed",
                          error=f"the model's {turn['session']} reply wasn't the JSON asked for: {res.get('text', '')[:200]}")
            return 1
        if turn["session"] == "remediation":
            db.finish_turn(self.conn, turn["id"], **done)
            rb = self.library.runbooks.get(inc["runbook"]) if self.library else None
            name = output.get("operation")
            if rb and name in rb.fixes and name in self.library.operations:   # the schema allows only these or "none"
                fixes.propose(self.conn, inc, rb, self.library.operations[name])
                log.info("incident %s: proposed fix %s (%s)", inc["id"], name, rb.fixes[name])
            db.set_status(self.conn, inc["id"], "analysed", advice=output,
                          analysis=investigate.render_analysis(inc["findings"], output),
                          analysis_model=job.get("ran_model") or turn["model"], analysis_worker=job.get("worker"))
            return 1
        rb = self.library.runbooks.get(inc["runbook"]) if self.library else None
        if rb is None:                                # the runbook was removed mid-incident: report what we have
            db.finish_turn(self.conn, turn["id"], **done)
            db.set_status(self.conn, inc["id"], "analysed", findings=output.get("findings"),
                          analysis=investigate.render_analysis(output.get("findings") or {}, None),
                          analysis_model=job.get("ran_model") or turn["model"], analysis_worker=job.get("worker"))
            return 1
        n = turn["turn"]
        menu, _ = investigate.menu_text(rb, self.library.operations)
        asked = list(dict.fromkeys(output.get("requests") or []))
        # Checks listed with "done" still run while rounds remain: seen in prod (a 9B listed the log it needed and
        # finished anyway). Read-only, so running one too many costs a turn, never a change.
        if asked and n < investigate.MAX_ROUNDS:
            earlier = {e["operation"]: Result(e["operation"], e["source"], e["content"], e["data"], e["truncated"],
                                              e["error"]) for e in inc["evidence"] if e["operation"]}
            run = [a for a in asked if a in menu]
            results = self._run_ops(run, inc["target"], inc["labels"], earlier)
            messages = investigate.next_messages(turn["messages"], res.get("text", ""), results,
                                                 [a for a in asked if a not in menu], investigate.MAX_ROUNDS - 1 - n)
            schema = investigate.investigation_schema(menu, final=n + 1 >= investigate.MAX_ROUNDS)
            try:
                job_id = self.gpu.submit(messages, schema, self.cfg.investigate_max_new_tokens)
            except Exception as e:
                log.warning("submit of turn %d for incident %s failed: %s", n + 1, inc["id"], e)
                return 0
            db.finish_turn(self.conn, turn["id"], **done)
            self._store(inc["id"], results, "requested", round=n)
            db.add_turn(self.conn, inc["id"], session="investigation", prompt_version=investigate.PROMPT_VERSION,
                        model=self.cfg.model, fallback=self._fallback(messages), messages=messages,
                        gpu_job_id=job_id, turn=n + 1, json_schema=schema)
            log.info("incident %s: turn %d asked for %s", inc["id"], n, asked)
            return 1
        findings = output.get("findings") or {}
        fix_names, fixes_txt = investigate.fixes_text(rb, self.library.operations)
        messages = investigate.remediation_messages(inc, rb, inc["target"], findings, self.cfg.primer(), fixes_txt)
        schema = investigate.advice_schema(fix_names)
        try:
            job_id = self.gpu.submit(messages, schema, self.cfg.investigate_max_new_tokens)
        except Exception as e:
            log.warning("submit of remediation for incident %s failed: %s", inc["id"], e)
            return 0
        db.finish_turn(self.conn, turn["id"], **done)
        db.add_turn(self.conn, inc["id"], session="remediation", prompt_version=investigate.PROMPT_VERSION,
                    model=self.cfg.model, fallback=self._fallback(messages), messages=messages, gpu_job_id=job_id,
                    turn=n + 1, json_schema=schema)
        db.set_status(self.conn, inc["id"], "advising", findings=findings)
        log.info("incident %s: findings after %d turn(s), confidence %s", inc["id"], n, findings.get("confidence"))
        return 1

    def notify(self) -> int:
        if not self.notifier:
            return 0
        n = 0
        for inc in db.to_notify(self.conn):
            full = db.get_incident(self.conn, inc["id"])
            try:
                self.notifier.send_incident(full)
            except Exception as e:
                log.warning("email for incident %s failed: %s", inc["id"], e)
                continue
            db.mark_notified(self.conn, inc["id"])
            n += 1
        for run in db.op_runs_to_notify(self.conn):
            try:
                self.notifier.send_fix(run, db.get_incident(self.conn, run["incident_id"]))
            except Exception as e:
                log.warning("email for fix run %s failed: %s", run["id"], e)
                continue
            db.mark_op_run_notified(self.conn, run["id"], run["status"])
            n += 1
        return n

    def run_forever(self, interval: float = 5.0) -> None:
        while not self.stopping:
            try:
                moved = self.step()
                self.last_pass = time.time()
                if moved == 0:
                    time.sleep(interval)
            except Exception:
                log.exception("triage pass failed")
                time.sleep(interval)


def _jsonable(v):
    import json
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return None
