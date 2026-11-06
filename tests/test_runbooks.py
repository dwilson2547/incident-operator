"""Phase 2a: runbook and operation files, read-only operations, and the investigation -> remediation loop."""
import json
import shutil
import uuid
from pathlib import Path

import pytest
from conftest import alert, needs_pg

from incident_operator import db, investigate
from incident_operator.library import Library, resolve_target
from incident_operator.ops import Prometheus, Result, Runner
from incident_operator.triage import Config, Engine
from test_evidence import EVENTS, POD, FakeK8s

CHART = Path(__file__).resolve().parents[1] / "helm" / "incident-operator" / "files"


@pytest.fixture
def lib_dirs(tmp_path):
    shutil.copytree(CHART / "runbooks", tmp_path / "runbooks")
    shutil.copytree(CHART / "operations", tmp_path / "operations")
    return tmp_path


def load(d):
    lib = Library(str(d / "runbooks"), str(d / "operations"))
    lib.refresh()
    return lib


# -- the library ------------------------------------------------------------------------------------------------------

def test_shipped_files_load_cleanly():
    lib = load(CHART)
    assert lib.errors == {}
    assert set(lib.runbooks) == {"pod-crashloop", "backup-job-failed", "nfs-client-wedge", "scraper-failures",
                                "deployment-rollout", "job-failed", "tower-container-cpu"}
    assert not any(r.warnings for r in lib.runbooks.values())          # all under the 800-token warning
    for alert in ("ScrapeJobFailing", "ScrapeJobStale", "ScrapeJobSuspended"):
        assert lib.match({"alertname": alert, "scrape_job": "s", "exported_namespace": "g"}).name == "scraper-failures"


def test_matching_and_priority(lib_dirs):
    (lib_dirs / "runbooks" / "any-crash.md").write_text(
        "---\nname: any-crash\ndescription: x\nmatch: {alertname: '~Kube.*'}\npriority: 1\n---\nbody\n")
    lib = load(lib_dirs)
    assert lib.match({"alertname": "KubePodCrashLooping", "pod": "p"}).name == "pod-crashloop"   # priority 10 wins
    assert lib.match({"alertname": "KubeNodeNotReady"}).name == "any-crash"
    assert lib.match({"alertname": "OffsiteBackupJobFailed"}).name == "backup-job-failed"       # list match
    assert lib.match({"alertname": "Watchdog"}) is None


def test_bad_files_are_rejected_and_the_last_good_version_stays(lib_dirs):
    rb = lib_dirs / "runbooks" / "pod-crashloop.md"
    lib = load(lib_dirs)
    good_rev = lib.runbooks["pod-crashloop"].rev
    rb.write_text(rb.read_text().replace("operations: [", "operations: [no-such-op, "))
    lib.refresh()
    assert "no-such-op" in lib.errors["runbooks/pod-crashloop.md"]
    assert lib.runbooks["pod-crashloop"].rev == good_rev                       # still live
    (lib_dirs / "operations" / "fix.yaml").write_text(
        "name: fix\ndescription: d\nmutating: true\nrun: {kind: promql, queries: {a: up}}\n")
    (lib_dirs / "runbooks" / "long.md").write_text(
        "---\nname: long\ndescription: x\nmatch: {alertname: A}\n---\n" + "word " * 1200)
    lib.refresh()
    assert "run.verb" in lib.errors["operations/fix.yaml"] and "over 1200" in lib.errors["runbooks/long.md"]


def test_operation_validation(lib_dirs):
    op = lib_dirs / "operations" / "bad.yaml"
    op.write_text("name: bad\ndescription: d\nrun: {kind: promql, queries: {a: 'up{x=\"{nope}\"}'}}\n")
    assert "nope" in load(lib_dirs).errors["operations/bad.yaml"]
    op.write_text("name: bad\ndescription: d\nparams: {x: {from: model}}\nrun: {kind: promql, queries: {a: up}}\n")
    assert "from must be" in load(lib_dirs).errors["operations/bad.yaml"]


def test_runbook_sections(lib_dirs):
    rb = load(lib_dirs).runbooks["nfs-client-wedge"]
    assert rb.section("Remediation").startswith("- Stuck mount.nfs on this node")
    assert "Investigation" not in rb.section("Remediation")


def test_target_resolution():
    lib = load(CHART)
    node = {"metadata": {"name": "k8s-worker1"}, "status": {"addresses": [{"type": "InternalIP", "address": "192.168.0.36"}]}}
    k = FakeK8s({"/api/v1/nodes": {"items": [node]}})
    nfs = lib.runbooks["nfs-client-wedge"]
    assert resolve_target(nfs, {"instance": "192.168.0.36:9100"}, k) == {"kind": "Node", "name": "k8s-worker1"}
    assert resolve_target(nfs, {"instance": "tower"}, k) is None
    sj = resolve_target(lib.runbooks["scraper-failures"], {"scrape_job": "s", "exported_namespace": "g",
                                                           "namespace": "scrape-job-manager"}, k)
    assert sj == {"kind": "ScrapeJob", "name": "s", "namespace": "g"}


# -- operations -------------------------------------------------------------------------------------------------------

class FakeProm(Prometheus):
    def __init__(self):
        self.queries = []

    def get(self, path, params):
        self.queries.append(params["query"])
        if path.endswith("query_range"):
            return {"result": [{"metric": {"container": "app"}, "values": [[i, str(i)] for i in range(60)]}]}
        return {"result": [{"metric": {"instance": "10.1.1.1:8080", "job": "kube-state-metrics", "cronjob": "c"},
                            "value": [0, "20.5"]}]}


def test_params_come_only_from_target_labels_and_results():
    lib = load(CHART)
    prom = FakeProm()
    r = Runner(FakeK8s({}), prom)
    res = r.run(lib.operations["pod-restarts-6h"], {"kind": "Pod", "name": "web-1", "namespace": "n"}, {}, {})
    assert 'pod="web-1"' in prom.queries[-1] and res.text.count(" ") >= 24     # downsampled to <= 24 points
    bad = r.run(lib.operations["pod-restarts-6h"], {"kind": "Pod", "name": 'x"} or up{a="', "namespace": "n"}, {}, {})
    assert bad.error and "plain name" in bad.error and len(prom.queries) == 1   # never reached Prometheus
    missing = r.run(lib.operations["scrape-last-run"], {"kind": "ScrapeJob", "name": "s", "namespace": "g"}, {}, {})
    assert "hasn't run" in missing.error
    hist = r.run(lib.operations["cronjob-history"], {"kind": "Job", "name": "j", "namespace": "n"}, {"cronjob": "c"}, {})
    assert "instance" not in hist.text and "= 20.5" in hist.text               # exporter labels dropped


def test_result_params_chain_through_earlier_operations():
    lib = load(CHART)
    sj = {"status": {"phase": "Suspended", "lastRun": {"k8sJob": "site-1-1", "result": "Failed"}}, "spec": {}}
    job = {"spec": {"backoffLimit": 0}, "status": {"failed": 1}}
    k = FakeK8s({"/apis/scrape.danwils.com/v1alpha1/namespaces/g/scrapejobs/site": sj,
                 "/apis/batch/v1/namespaces/g/jobs/site-1-1": job, "/api/v1/namespaces/g/events": {"items": []},
                 "/api/v1/namespaces/g/pods": {"items": []}})
    r, t, earlier = Runner(k, FakeProm()), {"kind": "ScrapeJob", "name": "site", "namespace": "g"}, {}
    first = r.run(lib.operations["scrapejob-status"], t, {}, earlier)
    earlier["scrapejob-status"] = first
    assert "status.phase: Suspended" in first.text
    second = r.run(lib.operations["scrape-last-run"], t, {}, earlier)
    assert second.error is None and "failed=1" in second.text and second.source == "Job g/site-1-1"


def test_events_filtered_to_the_node():
    lib = load(CHART)
    evs = {"items": [
        {"reason": "FailedMount", "type": "Warning", "message": "timed out", "lastTimestamp": "2026-11-01T10:00:00Z",
         "involvedObject": {"kind": "Pod", "namespace": "postgres", "name": "backup-1"}, "source": {"host": "k8s-worker1"}},
        {"reason": "FailedMount", "type": "Warning", "message": "other node", "lastTimestamp": "2026-11-01T10:00:00Z",
         "involvedObject": {"kind": "Pod", "namespace": "x", "name": "y"}, "source": {"host": "k8s-worker2"}}]}
    k = FakeK8s({"/api/v1/events": evs})
    res = Runner(k, FakeProm()).run(lib.operations["node-failedmount-events"], {"kind": "Node", "name": "k8s-worker1"}, {}, {})
    assert "backup-1" in res.text and "other node" not in res.text
    assert k.calls[-1][1] == {"fieldSelector": "reason=FailedMount"}


def test_investigation_schema_only_allows_done_on_the_last_round():
    s = investigate.investigation_schema(["a", "b"], final=False)
    assert s["properties"]["action"]["enum"] == ["request", "done"]
    assert s["properties"]["requests"]["items"]["enum"] == ["a", "b"]
    assert investigate.investigation_schema(["a"], final=True)["properties"]["action"]["enum"] == ["done"]


# -- the loop, end to end ---------------------------------------------------------------------------------------------

CUT_OFF = object()                 # a reply that ran into max_new_tokens: unfinished JSON


class ScriptedGpu:
    """Answers each submitted job with the next scripted JSON reply."""

    def __init__(self, replies):
        self.replies, self.jobs, self.submitted, self.down, self.models = list(replies), {}, [], False, []

    def submit(self, messages, json_schema=None, max_new_tokens=None, model=None):
        if self.down:
            raise ConnectionError("down")
        jid = str(uuid.uuid4())
        reply = self.replies.pop(0)
        result = ({"text": '{"action":"done","reason":"again and again', "json": None, "output_tokens": 1536}
                  if reply is CUT_OFF else {"text": json.dumps(reply), "json": reply, "output_tokens": 300})
        self.jobs[jid] = {"status": "done", "ran_model": "qwen35-9b", "worker": "ainode", "result": result}
        self.submitted.append((messages, json_schema))
        self.models.append(model)
        return jid

    def job(self, jid):
        return self.jobs[jid]


FINDINGS = {"what_failed": "container app OOMKilled", "where": "Pod n/web-7d9f8-abcde", "evidence_refs": ["pod-status"],
            "confidence": "high", "matches_runbook": True, "summary": "Memory rises to the 256Mi limit."}
ADVICE = {"suggestion": "Raise the memory limit in values.", "why": "It hits the limit every time.",
          "will_it_fix_itself": "No.", "escalate_to_human": ""}


def crash_k8s():
    oom = {**POD, "status": {**POD["status"], "containerStatuses": [{"name": "scraper", "restartCount": 3, "state": {
        "waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}
    return FakeK8s({"/api/v1/namespaces/n/pods/web-7d9f8-abcde": oom, "/api/v1/namespaces/n/events": EVENTS,
                    "/api/v1/namespaces/n/pods/web-7d9f8-abcde/log?previous=false": "starting",
                    "/api/v1/namespaces/n/pods/web-7d9f8-abcde/log?previous=true": "allocating cache"})


def engine(conn, gpu, tmp_path, k8s=None):
    primer = tmp_path / "primer.md"
    primer.write_text("Exit 137 = OOMKilled.")
    k8s = k8s or crash_k8s()
    return Engine(conn, Config(primer_path=str(primer)), k8s, gpu, None, library=load(CHART),
                  runner=Runner(k8s, FakeProm()))


@needs_pg
def test_investigation_requests_a_check_then_finishes_and_gets_advice(conn, tmp_path):
    gpu = ScriptedGpu([
        {"action": "request", "requests": ["pod-memory-6h", "pod-memory-6h"], "reason": "is it a leak?", "findings": FINDINGS},
        {"action": "done", "requests": [], "reason": "enough", "findings": FINDINGS},
        ADVICE])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="n", pod="web-7d9f8-abcde"))
    e.step()                                     # precursors + turn 1, answered + turn 2, answered + remediation, ...
    for _ in range(4):
        e.step()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "analysed" and inc["runbook"] == "pod-crashloop" and inc["runbook_rev"]
    assert inc["target"] == {"kind": "Pod", "name": "web-7d9f8-abcde", "namespace": "n"}
    assert [(e_["trigger"], e_["operation"]) for e_ in inc["evidence"]] == [
        ("precursor", "pod-status"), ("precursor", "pod-previous-logs"), ("requested", "pod-memory-6h")]
    assert [(t["session"], t["turn"]) for t in inc["turns"]] == [("investigation", 1), ("investigation", 2),
                                                                 ("remediation", 3)]
    first_user = gpu.submitted[0][0][1]["content"]
    assert "OOMKilled" in first_user and "pod-memory-6h:" in first_user and "Exit 137 = OOMKilled." in gpu.submitted[0][0][0]["content"]
    assert gpu.submitted[0][1]["properties"]["action"]["enum"] == ["request", "done"]
    remediation = gpu.submitted[2][0]
    assert "allocating cache" not in json.dumps(remediation)            # no raw evidence reaches remediation
    assert "Raise the memory limit" in inc["analysis"] and "Confidence: high" in inc["analysis"]
    assert inc["findings"] == FINDINGS and inc["advice"] == ADVICE
    # no runbook-matched incident shows up as needing one
    assert all(r["alertname"] != "KubePodCrashLooping" for r in db.needs_runbook(conn))


@needs_pg
def test_last_round_must_finish(conn, tmp_path):
    ask = {"action": "request", "requests": ["pod-restarts-6h"], "reason": "more", "findings": FINDINGS}
    gpu = ScriptedGpu([ask, ask, {**ask, "action": "done", "requests": []}, ADVICE])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="n", pod="web-7d9f8-abcde"))
    for _ in range(6):
        e.step()
    assert db.get_incident(conn, inc_id)["status"] == "analysed"
    assert gpu.submitted[2][1]["properties"]["action"]["enum"] == ["done"]          # turn 3 of 3
    assert "last turn" in gpu.submitted[2][0][-1]["content"]


@needs_pg
def test_gpu_outage_mid_loop_resumes(conn, tmp_path):
    gpu = ScriptedGpu([{"action": "done", "requests": [], "reason": "r", "findings": FINDINGS}, ADVICE])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="n", pod="web-7d9f8-abcde"))
    e.library.refresh()
    e.start_new()                                # turn 1 submitted (and, scripted, already answered)
    gpu.down = True                              # ...but the remediation turn can't be submitted
    e.poll(); e.poll()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "investigating" and inc["turns"][0]["status"] == "submitted"   # waits, nothing lost
    gpu.down = False
    e.step(); e.step()
    assert db.get_incident(conn, inc_id)["status"] == "analysed"


@needs_pg
def test_checks_listed_with_done_still_run_while_rounds_remain(conn, tmp_path):
    gpu = ScriptedGpu([{"action": "done", "requests": ["pod-restarts-6h"], "reason": "r", "findings": FINDINGS},
                       {"action": "done", "requests": [], "reason": "r", "findings": FINDINGS}, ADVICE])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="n", pod="web-7d9f8-abcde"))
    for _ in range(4):
        e.step()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "analysed" and [t["session"] for t in inc["turns"]] == ["investigation"] * 2 + ["remediation"]
    assert any(x["operation"] == "pod-restarts-6h" for x in inc["evidence"])


@needs_pg
def test_reply_that_isnt_json_fails_the_incident(conn, tmp_path):
    gpu = ScriptedGpu([None])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="n", pod="web-7d9f8-abcde"))
    e.step(); e.step()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "triage_failed" and "wasn't the JSON" in inc["error"]


DONE = {"action": "done", "requests": [], "reason": "enough", "findings": FINDINGS}


def test_evidence_refs_are_bounded():
    s = investigate.investigation_schema(["a"], final=False)
    assert s["properties"]["findings"]["properties"]["evidence_refs"]["maxItems"] == 8


@needs_pg
def test_cut_off_investigation_reply_is_retried_with_a_changed_prompt(conn, tmp_path):
    gpu = ScriptedGpu([CUT_OFF, DONE, ADVICE])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="n", pod="web-7d9f8-abcde"))
    for _ in range(4):
        e.step()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "analysed"
    assert [(t["session"], t["turn"]) for t in inc["turns"]] == [("investigation", 1), ("investigation", 1),
                                                                 ("remediation", 2)]
    (first, schema1), (second, schema2) = gpu.submitted[:2]
    assert first != second and "token limit" in second[-1]["content"] and "token limit" not in first[-1]["content"]
    assert second[-1]["content"].startswith(first[-1]["content"]) and schema1 == schema2


@needs_pg
def test_cut_off_remediation_reply_is_retried(conn, tmp_path):
    gpu = ScriptedGpu([DONE, CUT_OFF, ADVICE])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="n", pod="web-7d9f8-abcde"))
    for _ in range(4):
        e.step()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "analysed" and [t["session"] for t in inc["turns"]] == ["investigation", "remediation",
                                                                                    "remediation"]


@needs_pg
def test_second_cut_off_reply_fails_and_says_why(conn, tmp_path):
    gpu = ScriptedGpu([CUT_OFF, CUT_OFF])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="n", pod="web-7d9f8-abcde"))
    for _ in range(4):
        e.step()
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "triage_failed" and "1536-token limit" in inc["error"] and len(gpu.submitted) == 2


@needs_pg
def test_retry_waits_for_an_unreachable_gpu_scheduler(conn, tmp_path):
    gpu = ScriptedGpu([CUT_OFF, DONE, ADVICE])
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(namespace="n", pod="web-7d9f8-abcde"))
    e.start_new()
    gpu.down = True
    assert e.poll() == 0
    inc = db.get_incident(conn, inc_id)
    assert inc["status"] == "investigating" and [t["status"] for t in inc["turns"]] == ["submitted"]
    gpu.down = False
    for _ in range(3):
        e.step()
    assert db.get_incident(conn, inc_id)["status"] == "analysed"


@needs_pg
def test_alert_without_runbook_still_gets_phase_1_triage(conn, tmp_path):
    from test_triage import FakeGpu
    gpu = FakeGpu()
    e = engine(conn, gpu, tmp_path)
    inc_id, _ = db.record_firing(conn, alert(alertname="TargetDown"))
    e.step()
    assert db.get_incident(conn, inc_id)["status"] == "triaging" and db.get_incident(conn, inc_id)["runbook"] is None


def test_render_analysis_skips_empty_escalation():
    text = investigate.render_analysis(FINDINGS, ADVICE)
    assert "Escalate" not in text and text.startswith("What failed: container app OOMKilled")
    assert "Escalate to a human: reboot" in investigate.render_analysis(FINDINGS, {**ADVICE, "escalate_to_human": "reboot"})


def test_results_respect_the_budget():
    big = [Result("a", "a", "x" * 9000), Result("b", "b", "y" * 9000)]
    text = investigate.render_results(big, 10000)
    assert len(text) < 10600 and "cut to fit" in text


@needs_pg
def test_last_good_version_survives_a_restart(conn, lib_dirs):
    """2026-11-01: a new chart reached a pod running an older image, which rejected the NFS runbook; the in-memory
    last good version was gone because the pod had restarted."""
    good = load_with(lib_dirs, conn)
    rev = good.runbooks["pod-crashloop"].rev
    rb = lib_dirs / "runbooks" / "pod-crashloop.md"
    rb.write_text(rb.read_text().replace("operations: [", "operations: [no-such-op, "))
    after_restart = load_with(lib_dirs, conn)                     # a new process: nothing in memory
    assert after_restart.runbooks["pod-crashloop"].rev == rev
    assert "serving the last good version, saved" in after_restart.errors["runbooks/pod-crashloop.md"]
    (lib_dirs / "runbooks" / "nfs-client-wedge.md").unlink()       # removed from the chart: not brought back
    assert "nfs-client-wedge" not in load_with(lib_dirs, conn).runbooks
    assert load(lib_dirs).runbooks.get("pod-crashloop") is None    # without a store, nothing to fall back on


def load_with(d, conn):
    lib = Library(str(d / "runbooks"), str(d / "operations"), store=db.LastGood(conn))
    lib.refresh()
    return lib
