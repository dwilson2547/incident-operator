"""Phase 1 triage (docs/design.md §6.5): one model call per incident, no runbook, suggestion only.

The engine is a loop of three idempotent passes over the database, so a restart picks up where it stopped:
1. `new` incidents: collect evidence, build the prompt, submit it to gpu-scheduler -> `triaging`
2. submitted turns: poll gpu-scheduler; on done/failed record the result -> `analysed` / `triage_failed`
3. analysed or failed incidents not yet mailed: send the email

The model is a gpu-scheduler model with a fallback (design §9.2): qwen35-9b on the 3080, gemma4-26b-a4b on the Mac
after `fallback_after_seconds`, so a busy GPU doesn't hold up triage. Every prompt is stored exactly as sent.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db, evidence

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
    deadline_seconds: int = 900
    max_new_tokens: int = 1024
    primer_path: str = "/etc/incident-operator/primer.md"

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        fb = e.get("TRIAGE_FALLBACK", "gemma4-26b-a4b")
        return cls(gpu_url=e.get("GPU_SCHEDULER_URL", cls.gpu_url), gpu_token=e.get("GPU_SCHEDULER_TOKEN", ""),
                   model=e.get("TRIAGE_MODEL", cls.model), fallback=[m for m in fb.split(",") if m.strip()],
                   fallback_after_seconds=int(e.get("TRIAGE_FALLBACK_AFTER_SECONDS", cls.fallback_after_seconds)),
                   deadline_seconds=int(e.get("TRIAGE_DEADLINE_SECONDS", cls.deadline_seconds)),
                   max_new_tokens=int(e.get("TRIAGE_MAX_NEW_TOKENS", cls.max_new_tokens)),
                   primer_path=e.get("PRIMER_PATH", cls.primer_path))

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


class GpuScheduler:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _h(self):
        return {"Authorization": f"Bearer {self.cfg.gpu_token}"}

    def submit(self, messages: list[dict]) -> str:
        import httpx
        body = {"model": self.cfg.model, "inputs": [{"messages": messages, "max_new_tokens": self.cfg.max_new_tokens}],
                "deadline_seconds": self.cfg.deadline_seconds, "max_attempts": 2}
        if self.cfg.fallback:
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
    def __init__(self, conn, cfg: Config, k8s, gpu: GpuScheduler, notifier=None):
        self.conn, self.cfg, self.k8s, self.gpu, self.notifier = conn, cfg, k8s, gpu, notifier
        self.stopping = False

    def step(self) -> int:
        """One pass of each stage. Returns how many things moved."""
        return self.start_new() + self.poll() + self.notify()

    def start_new(self) -> int:
        n = 0
        for inc in db.incidents_with_status(self.conn, "new"):
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
                        fallback=self.cfg.fallback or None, messages=messages, gpu_job_id=job_id)
            db.set_status(self.conn, inc["id"], "triaging")
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
            if job["status"] == "done":
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
        return n

    def run_forever(self, interval: float = 5.0) -> None:
        while not self.stopping:
            try:
                if self.step() == 0:
                    time.sleep(interval)
            except Exception:
                log.exception("triage pass failed")
                time.sleep(interval)
