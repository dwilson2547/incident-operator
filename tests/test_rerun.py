"""What else was going on (db.related, the block in the prompts) and re-running triage on stored evidence."""
import json
from datetime import timedelta

import pytest
from conftest import alert, needs_pg
from fastapi.testclient import TestClient

from incident_operator import db, investigate
from incident_operator.app import create_ui_app
from incident_operator.investigate import alert_subject, related_block
from incident_operator.triage import Engine, Config
from test_runbooks import ADVICE, CHART, FINDINGS, ScriptedGpu, crash_k8s, engine
from test_triage import FakeGpu, Mailbox, k8s

MODELS = [{"name": "qwen35-9b", "label": "9B", "max_prompt_tokens": 48000},
          {"name": "gemma4-26b-a4b", "label": "Gemma", "max_prompt_tokens": 12000}]


@pytest.fixture(autouse=True)
def rerun_models(monkeypatch):
    monkeypatch.setenv("TRIAGE_RERUN_MODELS", json.dumps(MODELS))


def fire(conn, fp, minutes=0, **labels):
    inc_id, _ = db.record_firing(conn, alert(fp, **labels))
    conn.execute("UPDATE incidents SET starts_at = starts_at + %s WHERE id = %s", (timedelta(minutes=minutes), inc_id))
    return db.get_incident(conn, inc_id)


def near(offset, name="ScrapeJobFailing", labels=None, fired=None):
    return {"alertname": name, "labels": labels or {"scrape_job": "a", "scraper": "b", "pod": "exporter"},
            "offset_seconds": offset, "fired_seconds": fired}


# -- the block -----------------------------------------------------------------------------------------------------


def test_block_says_so_when_nothing_else_fired():
    text = related_block({"nearby": [], "nearby_total": 0, "nearby_close": 0, "earlier": [], "earlier_total": 0})
    assert "none within 1 hour" in text and "no earlier firing on record" in text


def test_block_counts_in_code_and_caps_the_list():
    rows = [near(0), near(-420, fired=600), near(600)] + [near(1800 + i) for i in range(12)]
    text = related_block({"nearby": rows, "nearby_total": 40, "nearby_close": 3, "earlier": [{"age_seconds": 7200}],
                          "earlier_total": 1})
    assert "40 within 1 hour of this alert, 3 of them within 10 minutes." in text
    assert "- same time: ScrapeJobFailing scrape_job=a scraper=b (still firing)" in text
    assert "- 7m earlier: ScrapeJobFailing scrape_job=a scraper=b (resolved after 10m)" in text
    assert "- 10m later:" in text and "- and 30 more" in text
    assert text.count("\n- ") == investigate.RELATED_LINES + 1
    assert "fired 1 time before" in text and "2h" in text


def test_subject_prefers_the_object_over_the_exporter_pod():
    assert alert_subject({"scrape_job": "j", "scraper": "s", "pod": "kube-state", "namespace": "n"}) == "scrape_job=j scraper=s"
    assert alert_subject({"deployment": "d", "namespace": "n"}) == "deployment=d namespace=n"
    assert alert_subject({"severity": "warning"}) == ""


# -- the query -----------------------------------------------------------------------------------------------------


@needs_pg
def test_related_finds_neighbours_by_alert_time_and_earlier_firings(conn):
    inc = fire(conn, "this", alertname="ScrapeJobFailing", scrape_job="a")
    near_, other = fire(conn, "n1", 5, alertname="ScrapeJobFailing", scrape_job="b"), fire(conn, "n2", -40, alertname="Other", pod="x")
    fire(conn, "far", 90, alertname="Far", pod="y")
    conn.execute("UPDATE incidents SET problem_key = 'p' WHERE id = %s", (inc["id"],))
    old = fire(conn, "same", -300, alertname="ScrapeJobFailing", scrape_job="a")
    conn.execute("UPDATE incidents SET problem_key = 'p' WHERE id = %s", (old["id"],))
    conn.execute("UPDATE incidents SET alert_resolved_at = starts_at + interval '10 minutes' WHERE id = %s", (near_["id"],))
    r = db.related(conn, db.get_incident(conn, inc["id"]))
    assert [n["id"] for n in r["nearby"]] == [near_["id"], other["id"]]      # nearest first; the far one and itself out
    assert (r["nearby_total"], r["nearby_close"]) == (2, 1)
    assert r["nearby"][0]["offset_seconds"] == 300 and r["nearby"][0]["fired_seconds"] == 600
    assert r["nearby"][1]["offset_seconds"] == -2400 and r["nearby"][1]["fired_seconds"] is None
    assert [e["id"] for e in r["earlier"]] == [old["id"]] and r["earlier"][0]["age_seconds"] == 18000
    assert old["id"] not in [n["id"] for n in r["nearby"]]


@needs_pg
def test_investigation_and_remediation_both_see_other_alerts(conn, tmp_path):
    gpu = ScriptedGpu([{"action": "done", "requests": [], "reason": "ok", "findings": FINDINGS}, ADVICE])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert("main", namespace="n", pod="web-7d9f8-abcde"))
    db.set_status(conn, fire(conn, "before", 1, alertname="ScrapeJobFailing", scrape_job="first", scraper="one")["id"], "repeat")
    e.start_new()
    assert "scrape_job=first" in gpu.submitted[0][0][1]["content"]
    fire(conn, "during", 2, alertname="ScrapeJobFailing", scrape_job="second", scraper="two")   # while it investigates
    e.poll()
    remediation = gpu.submitted[1][0][1]["content"]
    assert "scrape_job=first" in remediation and "scrape_job=second" in remediation
    assert "2 within 1 hour" in remediation
    assert db.get_incident(conn, inc_id)["status"] == "advising"


# -- re-running ----------------------------------------------------------------------------------------------------


def answered(conn, tmp_path, extra=()):
    gpu = ScriptedGpu([{"action": "done", "requests": [], "reason": "ok", "findings": FINDINGS}, ADVICE, *extra])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert("main", namespace="n", pod="web-7d9f8-abcde"))
    for _ in range(3):
        e.step()
    assert db.get_incident(conn, inc_id)["status"] == "analysed"
    return e, gpu, inc_id


@needs_pg
def test_rerun_reads_stored_evidence_with_the_picked_model_and_keeps_the_old_answer(conn, tmp_path):
    second = {**FINDINGS, "summary": "A different reading."}
    e, gpu, inc_id = answered(conn, tmp_path, [{"action": "done", "requests": [], "reason": "ok", "findings": second}, ADVICE])
    mail = e.notifier = Mailbox()
    conn.execute("UPDATE incidents SET notified_at = now() WHERE id = %s", (inc_id,))
    e.k8s, e.runner = None, None                                   # any read of the cluster now blows up
    evidence_before = len(db.get_incident(conn, inc_id)["evidence"])
    assert db.begin_rerun(conn, inc_id, "gemma4-26b-a4b", "dan") is None
    inc = db.get_incident(conn, inc_id)
    assert (inc["status"], inc["attempt"], inc["analysis"], inc["findings"]) == ("rerun", 2, None, None)
    for _ in range(3):
        e.step()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "analysed" and inc["findings"]["summary"] == "A different reading."
    assert gpu.models[-2:] == ["gemma4-26b-a4b", "gemma4-26b-a4b"] and gpu.models[:2] == [None, None]
    messages, schema = gpu.submitted[2]
    assert "allocating cache" in messages[1]["content"] and "no more checks can run" in messages[1]["content"]
    assert schema["properties"]["action"]["enum"] == ["done"]
    assert len(inc["evidence"]) == evidence_before
    turns = [t for t in inc["turns"] if t["attempt"] == 2]
    assert [(t["session"], t["model"], t["fallback"]) for t in turns] == [("investigation", "gemma4-26b-a4b", None),
                                                                          ("remediation", "gemma4-26b-a4b", None)]
    [was] = inc["earlier_attempts"]
    assert was["attempt"] == 1 and was["findings"] == FINDINGS and was["advice"] == ADVICE
    assert mail.sent == []


@needs_pg
def test_a_model_that_asks_for_checks_anyway_cannot_reach_the_cluster(conn, tmp_path):
    e, gpu, inc_id = answered(conn, tmp_path, [{"action": "request", "requests": ["pod-memory-6h"], "reason": "more",
                                                "findings": FINDINGS}, ADVICE])
    e.runner = None
    db.begin_rerun(conn, inc_id, "qwen35-9b", "dan")
    for _ in range(3):
        e.step()
    assert db.get_incident(conn, inc_id)["status"] == "analysed"


@needs_pg
def test_rerun_doesnt_touch_the_brakes(conn, tmp_path):
    e, gpu, inc_id = answered(conn, tmp_path, [{"action": "done", "requests": [], "reason": "ok", "findings": FINDINGS}, ADVICE])
    conn.execute("UPDATE incidents SET notified_at = now(), first_seen = now() - interval '3 days' WHERE id = %s", (inc_id,))
    used = db.triages_since(conn, 3600)
    db.begin_rerun(conn, inc_id, "qwen35-9b", "dan")
    assert db.health(conn)["oldest_waiting"] == 0
    e.start_reruns()
    assert db.triages_since(conn, 3600) == used
    assert db.health(conn)["oldest_waiting"] == 0


@needs_pg
def test_rerun_supersedes_a_waiting_fix_and_refuses_while_one_runs(conn, tmp_path):
    e, gpu, inc_id = answered(conn, tmp_path)
    rid = db.add_op_run(conn, inc_id, "op", "r", {}, "approve", "proposed")
    assert db.begin_rerun(conn, inc_id, "qwen35-9b", "dan") is None
    assert conn.execute("SELECT status, decided_by FROM op_runs WHERE id = %s", (rid,)).fetchone() == {
        "status": "superseded", "decided_by": None}
    db.set_status(conn, inc_id, "analysed")
    conn.execute("UPDATE op_runs SET status = 'running' WHERE id = %s", (rid,))
    assert "under way" in db.begin_rerun(conn, inc_id, "qwen35-9b", "dan")
    conn.execute("UPDATE op_runs SET status = 'succeeded' WHERE id = %s", (rid,))
    db.set_status(conn, inc_id, "investigating")
    assert "investigating" in db.begin_rerun(conn, inc_id, "qwen35-9b", "dan")
    assert db.begin_rerun(conn, 999999, "qwen35-9b", "dan") == "no such incident"


@needs_pg
def test_rerun_over_the_models_limit_fails_without_sending(conn, tmp_path, monkeypatch):
    e, gpu, inc_id = answered(conn, tmp_path)
    monkeypatch.setenv("TRIAGE_RERUN_MODELS", json.dumps([{"name": "tiny", "max_prompt_tokens": 10}]))
    sent = len(gpu.submitted)
    db.begin_rerun(conn, inc_id, "tiny", "dan")
    e.start_reruns()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "triage_failed" and "over tiny's limit of 10" in inc["error"]
    assert len(gpu.submitted) == sent


@needs_pg
def test_submit_failure_stays_queued_and_is_retried(conn, tmp_path):
    e, gpu, inc_id = answered(conn, tmp_path, [{"action": "done", "requests": [], "reason": "ok", "findings": FINDINGS}])
    db.begin_rerun(conn, inc_id, "qwen35-9b", "dan")
    gpu.down = True
    e.start_reruns()
    assert db.get_incident(conn, inc_id)["status"] == "rerun"
    gpu.down = False
    e.start_reruns()
    assert db.get_incident(conn, inc_id)["status"] == "investigating"


@needs_pg
def test_rerun_without_a_runbook_is_one_plain_turn(conn, tmp_path):
    gpu, mail = FakeGpu(), Mailbox()
    primer = tmp_path / "primer.md"
    primer.write_text("primer")
    e = Engine(conn, Config(primer_path=str(primer)), k8s(), gpu, mail)
    inc_id, _ = db.record_firing(conn, alert(namespace="gyopart", pod="x"))
    e.step(); gpu.finish(); e.step()
    assert db.get_incident(conn, inc_id)["status"] == "analysed"
    e.k8s = None
    db.begin_rerun(conn, inc_id, "gemma4-26b-a4b", "dan")
    e.start_reruns()
    assert "Broken: 0 vehicles" in gpu.submitted[-1][1]["content"]
    gpu.finish(text="Second answer", ran_model="gemma4-26b-a4b")
    e.step()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "analysed" and inc["analysis"] == "Second answer" and inc["attempt"] == 2
    assert inc["earlier_attempts"][0]["analysis"] == "Where it failed: ..."
    assert len(mail.sent) == 1                                     # the first answer only


# -- the page ------------------------------------------------------------------------------------------------------


@needs_pg
def test_rerun_api(conn, tmp_path):
    e, gpu, inc_id = answered(conn, tmp_path)
    with TestClient(create_ui_app(conn.info.dsn, None, {})) as c:             # no sign-in configured
        assert c.post(f"/api/incidents/{inc_id}/rerun", json={"model": "qwen35-9b"}).status_code == 403
    env = {"ALLOW_ANONYMOUS_DECISIONS": "1", "TRIAGE_RERUN_MODELS": json.dumps(MODELS)}
    with TestClient(create_ui_app(conn.info.dsn, None, env)) as c:
        got = c.get(f"/api/incidents/{inc_id}").json()
        assert got["rerun_models"] == MODELS and got["related"]["nearby"] == []
        url = f"/api/incidents/{inc_id}/rerun"
        assert c.post(url, content="model=x", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415
        assert c.post(url, json={"model": "not-offered"}).status_code == 422
        assert c.post("/api/incidents/999999/rerun", json={"model": "qwen35-9b"}).status_code == 404
        assert c.post(url, json={"model": "qwen35-9b"}).json() == {"ok": True}
        assert c.post(url, json={"model": "qwen35-9b"}).status_code == 409    # already queued
