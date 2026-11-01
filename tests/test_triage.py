"""Intake, the triage engine and the email, with a fake gpu-scheduler, a fake Kubernetes and a real local SMTP server."""
import uuid

import pytest
from conftest import URL, alert, needs_pg
from fastapi.testclient import TestClient

from incident_operator import db
from incident_operator.app import create_ui_app, create_webhook_app
from incident_operator.notify import Notifier, SmtpConfig, compose
from incident_operator.triage import PROMPT_VERSION, Config, Engine, build_messages
from test_evidence import EVENTS, POD, FakeK8s

pytestmark = needs_pg


class FakeGpu:
    def __init__(self):
        self.jobs, self.submitted, self.down = {}, [], False

    def submit(self, messages, json_schema=None, max_new_tokens=None):
        if self.down:
            raise ConnectionError("gpu-scheduler unreachable")
        jid = str(uuid.uuid4())
        self.jobs[jid] = {"status": "queued"}
        self.submitted.append(messages)
        return jid

    def job(self, jid):
        return self.jobs[jid]

    def finish(self, text="Where it failed: ...", ran_model="gemma4-26b-a4b", worker="mac-mini"):
        for j in self.jobs.values():
            j.update(status="done", result={"text": text, "input_tokens": 900, "output_tokens": 200},
                     ran_model=ran_model, worker=worker)


class Mailbox:
    def __init__(self):
        self.sent = []

    def send_incident(self, inc):
        self.sent.append(inc)


def k8s():
    return FakeK8s({"/api/v1/namespaces/gyopart/pods/x": POD, "/api/v1/namespaces/gyopart/events": EVENTS,
                    "/api/v1/namespaces/gyopart/pods/x/log?previous=false": "fetching",
                    "/api/v1/namespaces/gyopart/pods/x/log?previous=true": "Broken: 0 vehicles"})


def engine(conn, gpu, mail, tmp_path):
    primer = tmp_path / "primer.md"
    primer.write_text("Exit 65 = Broken: permanent.")
    return Engine(conn, Config(primer_path=str(primer)), k8s(), gpu, mail)


def test_intake_dedupes_and_resolves(conn):
    a1, created = db.record_firing(conn, alert(namespace="gyopart", pod="x"))
    a2, again = db.record_firing(conn, alert(namespace="gyopart", pod="x"))
    assert created and not again and a1 == a2
    assert db.record_resolved(conn, alert(status="resolved")) == a1
    assert db.record_resolved(conn, alert(status="resolved")) is None            # nothing open any more
    a3, created3 = db.record_firing(conn, alert())                                # fires again: a new incident
    assert created3 and a3 != a1


def test_full_triage_flow(conn, tmp_path):
    gpu, mail = FakeGpu(), Mailbox()
    e = engine(conn, gpu, mail, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="gyopart", pod="x"))
    assert e.step() == 1                                                          # evidence + submit
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "triaging" and [x["kind"] for x in inc["evidence"]] == ["pod", "events", "logs", "logs"]
    (turn,) = inc["turns"]
    assert turn["prompt_version"] == PROMPT_VERSION and turn["fallback"] == ["gemma4-26b-a4b"]
    system, user = turn["messages"]
    assert "Exit 65 = Broken" in system["content"] and "Where it failed:" in system["content"]
    assert "KubePodCrashLooping" in user["content"] and "Broken: 0 vehicles" in user["content"]
    assert e.step() == 0                                                          # still queued: nothing moves
    gpu.finish()
    assert e.step() == 2                                                          # analysed, then mailed
    inc = db.get_incident(conn, inc_id)
    assert (inc["status"], inc["analysis_model"], inc["analysis_worker"]) == ("analysed", "gemma4-26b-a4b", "mac-mini")
    assert inc["turns"][0]["ran_model"] == "gemma4-26b-a4b" and inc["notified_at"] is not None
    assert len(mail.sent) == 1 and e.step() == 0                                   # mailed once


def test_failed_job_is_reported(conn, tmp_path):
    gpu, mail = FakeGpu(), Mailbox()
    e = engine(conn, gpu, mail, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="gyopart", pod="x"))
    e.step()
    for j in gpu.jobs.values():
        j.update(status="failed", error="LM Studio refused the job")
    e.step()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "triage_failed" and "refused" in inc["error"] and len(mail.sent) == 1


def test_unreachable_gpu_scheduler_leaves_the_incident_for_the_next_pass(conn, tmp_path):
    gpu, mail = FakeGpu(), Mailbox()
    gpu.down = True
    e = engine(conn, gpu, mail, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="gyopart", pod="x"))
    assert e.step() == 0
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "new" and inc["evidence"] == []                       # no half-recorded attempt
    gpu.down = False
    assert e.step() == 1 and len(db.get_incident(conn, inc_id)["evidence"]) == 4  # collected once


def test_build_messages_shows_labels_and_annotations():
    inc = {"alertname": "KubeJobFailed", "severity": "warning", "starts_at": None, "first_seen": "now",
           "labels": {"alertname": "KubeJobFailed", "namespace": "postgres", "job_name": "b1", "prometheus": "x"},
           "annotations": {"summary": "Job failed to complete."}}
    system, user = build_messages(inc, "--- job postgres/b1\nfailed=1", "")
    assert "job_name=b1" in user["content"] and "prometheus" not in user["content"]
    assert "Summary: Job failed to complete." in user["content"] and "failed=1" in user["content"]


def test_email_composition(conn):
    inc_id, _ = db.record_firing(conn, alert(namespace="gyopart", pod="x"))
    db.set_status(conn, inc_id, "analysed", analysis="Where it failed: the scraper.", analysis_model="qwen35-9b",
                  analysis_worker="ainode")
    msg = compose(db.get_incident(conn, inc_id), SmtpConfig(sender="a@b", to=["c@d"], public_url="http://io.local"))
    assert msg["Subject"] == "[triage] KubePodCrashLooping · namespace=gyopart pod=x"
    body = msg.get_content()
    assert "Where it failed: the scraper." in body and "Answered by qwen35-9b on ainode" in body
    assert f"http://io.local/#/incident/{inc_id}" in body and "nothing was changed" in body


def test_email_goes_through_smtp(conn):
    from aiosmtpd.controller import Controller
    got = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            got.append(envelope)
            return "250 OK"
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    ctl = Controller(Handler(), hostname="127.0.0.1", port=port)
    ctl.start()
    try:
        inc_id, _ = db.record_firing(conn, alert())
        db.set_status(conn, inc_id, "analysed", analysis="ok")
        Notifier(SmtpConfig(host="127.0.0.1", port=port, starttls=False,
                            sender="io@x", to=["me@x"])).send_incident(db.get_incident(conn, inc_id))
    finally:
        ctl.stop()
    assert got and got[0].rcpt_tos == ["me@x"] and b"[triage] KubePodCrashLooping" in got[0].content


def test_webhook_and_ui_apps(conn):
    with TestClient(create_webhook_app(URL)) as wh, TestClient(create_ui_app(URL)) as ui:
        payload = {"version": "4", "status": "firing", "alerts": [alert("a", namespace="n", pod="p"), alert("b"),
                                                                    {"status": "firing", "labels": {}}]}
        assert wh.post("/alertmanager", json=payload).json() == {"opened": 2, "updated": 0, "resolved": 0}
        assert wh.post("/alertmanager", json=payload).json() == {"opened": 0, "updated": 2, "resolved": 0}
        assert wh.post("/alertmanager", json={"alerts": [alert("a", status="resolved")]}).json()["resolved"] == 1
        d = ui.get("/api/incidents").json()
        assert len(d["incidents"]) == 2 and d["needs_runbook"][0]["alertname"] == "KubePodCrashLooping"
        assert d["needs_runbook"][0]["incidents"] == 2 and d["needs_runbook"][0]["open"] == 1
        one = ui.get(f"/api/incidents/{d['incidents'][0]['id']}").json()
        assert one["evidence"] == [] and one["turns"] == []
        assert ui.get("/api/incidents/99999").status_code == 404
        assert "<title>Incidents</title>" in ui.get("/").text
        assert ui.post("/alertmanager", json=payload).status_code in (404, 405)    # not on the UI's port


# -- brakes: repeats and the hourly cap -----------------------------------------------------------------------------

def test_problem_key_ignores_generated_names():
    a = db.problem_key({"alertname": "KubeJobFailed", "namespace": "n", "job_name": "backup-29886270",
                        "instance": "10.1.2.3:8080", "uid": "u1"})
    b = db.problem_key({"alertname": "KubeJobFailed", "namespace": "n", "job_name": "backup-29886271",
                        "instance": "10.1.9.9:8080", "uid": "u2"})
    assert a == b
    assert a != db.problem_key({"alertname": "KubeJobFailed", "namespace": "n", "job_name": "restore-29886270"})
    node = {"alertname": "NodeMemoryHighUtilization", "instance": "192.168.0.36:9100"}
    assert db.problem_key(node) != db.problem_key({**node, "instance": "192.168.0.37:9100"})


def test_flapping_alert_is_triaged_and_mailed_once(conn, tmp_path):
    gpu, mail = FakeGpu(), Mailbox()
    e = engine(conn, gpu, mail, tmp_path)
    first, _ = db.record_firing(conn, alert(namespace="gyopart", pod="x"))
    e.step(); gpu.finish(); e.step()
    for _ in range(5):                                                            # resolve, fire, resolve, ...
        db.record_resolved(conn, alert(status="resolved"))
        db.record_firing(conn, alert(namespace="gyopart", pod="x"))
        e.step()
    assert len(gpu.submitted) == 1 and len(mail.sent) == 1
    with TestClient(create_ui_app(URL)) as ui:
        d = ui.get("/api/incidents").json()
    assert [(i["id"], i["repeats"], i["repeats_open"]) for i in d["incidents"]] == [(first, 5, 1)]
    assert len(db.get_incident(conn, first)["repeats"]) == 5


def test_cronjob_runs_under_new_names_are_repeats(conn, tmp_path):
    gpu, mail = FakeGpu(), Mailbox()
    e = engine(conn, gpu, mail, tmp_path)
    first, _ = db.record_firing(conn, alert("r1", namespace="gyopart", job_name="backup-29886270"))
    e.step()
    second, _ = db.record_firing(conn, alert("r2", namespace="gyopart", job_name="backup-29886271"))
    e.step()
    inc = db.get_incident(conn, second)
    assert (inc["status"], inc["repeat_of"]) == ("repeat", first) and len(gpu.submitted) == 1


def test_after_the_window_it_is_triaged_again(conn, tmp_path):
    gpu, mail = FakeGpu(), Mailbox()
    e = engine(conn, gpu, mail, tmp_path)
    first, _ = db.record_firing(conn, alert(namespace="gyopart", pod="x"))
    e.step()
    conn.execute("UPDATE incidents SET first_seen = now() - interval '7 hours' WHERE id = %s", (first,))
    db.record_resolved(conn, alert(status="resolved"))
    db.record_firing(conn, alert(namespace="gyopart", pod="x"))
    e.step()
    assert len(gpu.submitted) == 2


def test_hourly_cap_holds_the_rest_and_skips_long_resolved(conn, tmp_path):
    gpu, mail = FakeGpu(), Mailbox()
    e = engine(conn, gpu, mail, tmp_path)
    e.cfg.max_per_hour = 2
    ids = [db.record_firing(conn, alert(f"s{k}", namespace="gyopart", pod=f"app{k}-x"))[0] for k in range(4)]
    e.step()
    assert [db.get_incident(conn, i)["status"] for i in ids] == ["triaging", "triaging", "new", "new"]
    # one waiting incident resolves long before there is room again: dropped, the other still waits
    conn.execute("UPDATE incidents SET alert_resolved_at = now() - interval '2 hours' WHERE id = %s", (ids[2],))
    e.step()
    assert [db.get_incident(conn, i)["status"] for i in ids[2:]] == ["skipped", "new"]
    conn.execute("UPDATE model_turns SET submitted_at = now() - interval '2 hours'")    # the hour passes
    e.step()
    assert db.get_incident(conn, ids[3])["status"] == "triaging" and len(gpu.submitted) == 3


def test_migrates_a_phase_1_database(conn):
    conn.execute("DROP TABLE op_runs, evidence, model_turns, incidents")
    old = db.SCHEMA.split("-- Added after the first release")[0].replace(", 'repeat', 'skipped'", "")
    conn.execute(old)
    conn.execute("INSERT INTO incidents (fingerprint, alertname, labels, annotations) VALUES ('f', 'A', '{}', '{}')")
    db.migrate(conn)
    db.migrate(conn)                                                               # and again: idempotent
    conn.execute("UPDATE incidents SET status = 'repeat'")
