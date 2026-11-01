"""One email per incident once its triage is done (or failed), alongside Alertmanager's own alert email (design §10),
saying when a fix awaits approval; then one short email per outcome of an approved fix. Same SMTP settings and env
names as scrape-job-manager's notifier."""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

log = logging.getLogger("incident_operator.notify")

TARGET_LABELS = ("namespace", "pod", "job_name", "deployment", "statefulset", "daemonset", "node", "instance")


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    starttls: bool = True
    username: str = ""
    password: str = ""
    sender: str = ""
    to: list[str] = field(default_factory=list)
    public_url: str = ""

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        e = os.environ
        return cls(host=e.get("SMTP_HOST", ""), port=int(e.get("SMTP_PORT", "587")),
                   starttls=e.get("SMTP_STARTTLS", "true").lower() == "true",
                   username=e.get("SMTP_USERNAME", ""), password=e.get("SMTP_PASSWORD", ""),
                   sender=e.get("SMTP_FROM", "") or e.get("SMTP_USERNAME", ""),
                   to=[a.strip() for a in e.get("SMTP_TO", "").split(",") if a.strip()],
                   public_url=e.get("PUBLIC_URL", "").rstrip("/"))

    @property
    def usable(self) -> bool:
        return bool(self.host and self.to and self.sender)


def target(labels: dict) -> str:
    return " ".join(f"{k}={labels[k]}" for k in TARGET_LABELS if labels.get(k))


def compose(inc: dict, cfg: SmtpConfig) -> EmailMessage:
    labels = inc["labels"]
    t = inc.get("target")
    where = (f"{t['kind']} {t['namespace'] + '/' if t.get('namespace') else ''}{t['name']}" if t else target(labels))
    ok = inc["status"] == "analysed"
    msg = EmailMessage()
    msg["Subject"] = f"[triage] {inc['alertname']}" + (f" · {where}" if where else "") + ("" if ok else " (analysis failed)")
    msg["From"], msg["To"] = cfg.sender, ", ".join(cfg.to)
    ann = inc.get("annotations") or {}
    lines = [f"{inc['alertname']} ({inc.get('severity') or 'no severity'}) since {inc.get('starts_at') or inc['first_seen']}"]
    if where:
        lines.append(where)
    if ann.get("summary"):
        lines.append(ann["summary"])
    lines.append("")
    if ok:
        turns = sum(1 for x in inc.get("turns") or [] if x.get("session") == "investigation")
        lines += [inc.get("analysis") or "(empty answer)", "",
                  f"Answered by {inc.get('analysis_model') or '?'} on {inc.get('analysis_worker') or '?'}"
                  + (f" after {turns} investigation turn{'s' if turns != 1 else ''}" if turns else "")
                  + ". Suggestion only: nothing was changed."]
        for f in inc.get("fixes") or []:
            if f["status"] == "proposed":
                lines += ["", f"Proposed fix, waiting for your approval: {f['operation']}", f"  {f['command']}",
                          "  Approve or reject it on the incident page; pre-checks run when you approve."]
            elif f["status"] == "suggested":
                lines += ["", f"Suggested fix (not run automatically; to do it yourself): {f['operation']}",
                          f"  {f['command']}"]
            elif f["status"] == "refused":
                lines += ["", f"The model proposed {f['operation']}, but it can't run here: {f.get('error')}"]
    else:
        lines += [f"The model couldn't analyse this one: {inc.get('error') or 'unknown error'}."]
    ev = inc.get("evidence") or []
    if ev:
        lines += ["", "Evidence it was given:"] + [
            f"  - {e.get('operation') or e['source']}" + (" (asked for)" if e.get("trigger") == "requested" else "")
            + (" (could not read)" if e.get("error") else "") for e in ev]
    lines += ["", f"Runbook: {inc['runbook']} ({inc.get('runbook_rev') or '?'})." if inc.get("runbook")
              else "No runbook covers this alert yet."]
    if cfg.public_url:
        lines.append(f"Incident: {cfg.public_url}/#/incident/{inc['id']}")
    msg.set_content("\n".join(lines))
    return msg


FIX_OUTCOME = {"succeeded": "done, post-checks passed", "post_failed": "ran, but the post-checks FAILED",
               "failed": "FAILED to run", "refused": "not run: refused", "rejected": "rejected"}


def compose_fix(run: dict, inc: dict, cfg: SmtpConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[triage] fix {run['operation']}: {FIX_OUTCOME.get(run['status'], run['status'])}"
    msg["From"], msg["To"] = cfg.sender, ", ".join(cfg.to)
    lines = [f"Incident {inc['id']}: {inc['alertname']}", f"Fix: {run['operation']}, approved by {run.get('decided_by')}",
             f"  {run.get('command') or ''}", ""]
    if run.get("result"):
        lines.append(f"What it did: {run['result']}")
    if run.get("checks"):
        lines += ["Checks:"] + [f"  {c}" for c in run["checks"].splitlines()]
    if run.get("error"):
        lines.append(f"Problem: {run['error']}")
    if cfg.public_url:
        lines += ["", f"Incident: {cfg.public_url}/#/incident/{inc['id']}"]
    msg.set_content("\n".join(lines))
    return msg


class Notifier:
    def __init__(self, cfg: SmtpConfig):
        self.cfg = cfg

    def send_incident(self, inc: dict) -> None:
        msg = compose(inc, self.cfg)
        self._send(msg)
        log.info("mailed incident %s to %s", inc["id"], ", ".join(self.cfg.to))

    def send_fix(self, run: dict, inc: dict) -> None:
        msg = compose_fix(run, inc, self.cfg)
        self._send(msg)
        log.info("mailed fix run %s (%s) to %s", run["id"], run["status"], ", ".join(self.cfg.to))

    def _send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=30) as s:
            if self.cfg.starttls:
                s.starttls(context=ssl.create_default_context())
            if self.cfg.username:
                s.login(self.cfg.username, self.cfg.password)
            s.send_message(msg)
