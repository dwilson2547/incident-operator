"""One email per incident once its triage is done (or failed), alongside Alertmanager's own alert email (design §10),
saying when a fix awaits approval; then one short email per outcome of an approved fix. Same SMTP settings and env
names as scrape-job-manager's notifier."""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING

from .settings import Env
from .states import FixStatus, IncidentStatus, Session, Trigger

if TYPE_CHECKING:
    from .models import Incident, OpRun

log = logging.getLogger("incident_operator.notify")

TARGET_LABELS = ("namespace", "pod", "job_name", "deployment", "statefulset", "daemonset", "node", "instance")


@dataclass
class SmtpConfig:
    host: str                   # empty: email is off
    port: int
    starttls: bool
    username: str
    password: str
    sender: str
    to: list[str]
    public_url: str             # base of the links in each email

    @classmethod
    def from_env(cls, env: Env) -> SmtpConfig:
        """SMTP_HOST and PUBLIC_URL always; the rest only when SMTP_HOST is set (an empty one turns email off)."""
        host, public_url = env.str("SMTP_HOST"), env.str("PUBLIC_URL").rstrip("/")
        if not host:
            return cls(host="", port=0, starttls=False, username="", password="", sender="", to=[],
                       public_url=public_url)
        return cls(host=host, port=env.int("SMTP_PORT"), starttls=env.bool("SMTP_STARTTLS"),
                   username=env.str("SMTP_USERNAME"), password=env.str("SMTP_PASSWORD"),
                   sender=env.str("SMTP_FROM"), to=env.list("SMTP_TO"), public_url=public_url)

    @property
    def usable(self) -> bool:
        return bool(self.host and self.to and self.sender)


def target(labels: dict) -> str:
    return " ".join(f"{k}={labels[k]}" for k in TARGET_LABELS if labels.get(k))


def compose(inc: Incident, cfg: SmtpConfig) -> EmailMessage:
    labels = inc.labels
    t = inc.target
    where = (f"{t['kind']} {t['namespace'] + '/' if t.get('namespace') else ''}{t['name']}" if t else target(labels))
    ok = inc.status == IncidentStatus.ANALYSED
    msg = EmailMessage()
    msg["Subject"] = (f"[triage] {inc.alertname}" + (f" · {where}" if where else "")
                      + ("" if ok else " (analysis failed)"))
    msg["From"], msg["To"] = cfg.sender, ", ".join(cfg.to)
    ann = inc.annotations or {}
    since = inc.starts_at or inc.first_seen
    lines = [f"{inc.alertname} ({inc.severity or 'no severity'}) since {since}"]
    if where:
        lines.append(where)
    if ann.get("summary"):
        lines.append(ann["summary"])
    lines.append("")
    if ok:
        turns = len({x.turn for x in inc.turns or [] if x.session == Session.INVESTIGATION})
        lines += [inc.analysis or "(empty answer)", "",
                  f"Answered by {inc.analysis_model or '?'} on {inc.analysis_worker or '?'}"
                  + (f" after {turns} investigation turn{'s' if turns != 1 else ''}" if turns else "")
                  + ". Suggestion only: nothing was changed."]
        for f in inc.fixes or []:
            if f.status == FixStatus.PROPOSED:
                lines += ["", f"Proposed fix, waiting for your approval: {f.operation}", f"  {f.command}",
                          "  Approve or reject it on the incident page; pre-checks run when you approve."]
            elif f.status == FixStatus.SUGGESTED:
                lines += ["", f"Suggested fix (not run automatically; to do it yourself): {f.operation}",
                          f"  {f.command}"]
            elif f.status == FixStatus.REFUSED:
                lines += ["", f"The model proposed {f.operation}, but it can't run here: {f.error}"]
    else:
        lines += [f"The model couldn't analyse this one: {inc.error or 'unknown error'}."]
    ev = inc.evidence or []
    if ev:
        lines += ["", "Evidence it was given:"] + [
            f"  - {e.operation or e.source}"
            + (" (asked for)" if e.trigger == Trigger.REQUESTED else "")
            + (" (could not read)" if e.error else "") for e in ev]
    lines += ["", f"Runbook: {inc.runbook} ({inc.runbook_rev or '?'})." if inc.runbook
              else "No runbook covers this alert yet."]
    if cfg.public_url:
        lines.append(f"Incident: {cfg.public_url}/#/incident/{inc.id}")
    msg.set_content("\n".join(lines))
    return msg


FIX_OUTCOME = {FixStatus.SUCCEEDED: "done, post-checks passed",
               FixStatus.POST_FAILED: "ran, but the post-checks FAILED", FixStatus.FAILED: "FAILED to run",
               FixStatus.REFUSED: "not run: refused", FixStatus.REJECTED: "rejected"}


def compose_fix(run: OpRun, inc: Incident, cfg: SmtpConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[triage] fix {run.operation}: {FIX_OUTCOME.get(run.status, run.status)}"
    msg["From"], msg["To"] = cfg.sender, ", ".join(cfg.to)
    lines = [f"Incident {inc.id}: {inc.alertname}",
             f"Fix: {run.operation}, approved by {run.decided_by}",
             f"  {run.command or ''}", ""]
    if run.result:
        lines.append(f"What it did: {run.result}")
    if run.checks:
        lines += ["Checks:"] + [f"  {c}" for c in run.checks.splitlines()]
    if run.error:
        lines.append(f"Problem: {run.error}")
    if cfg.public_url:
        lines += ["", f"Incident: {cfg.public_url}/#/incident/{inc.id}"]
    msg.set_content("\n".join(lines))
    return msg


class Notifier:
    def __init__(self, cfg: SmtpConfig):
        self.cfg = cfg

    def send_incident(self, inc: Incident) -> None:
        msg = compose(inc, self.cfg)
        self._send(msg)
        log.info("mailed incident %s to %s", inc.id, ", ".join(self.cfg.to))

    def send_fix(self, run: OpRun, inc: Incident) -> None:
        msg = compose_fix(run, inc, self.cfg)
        self._send(msg)
        log.info("mailed fix run %s (%s) to %s", run.id, run.status, ", ".join(self.cfg.to))

    def _send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=30) as s:
            if self.cfg.starttls:
                s.starttls(context=ssl.create_default_context())
            if self.cfg.username:
                s.login(self.cfg.username, self.cfg.password)
            s.send_message(msg)
