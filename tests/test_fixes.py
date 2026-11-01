"""Phase 3: fixes (mutating operations) from proposal through approval, pre-checks, the executor and post-checks."""
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import alert, needs_pg
from fastapi.testclient import TestClient

from incident_operator import db, executor, fixes
from incident_operator.app import create_ui_app
from incident_operator.library import Library
from incident_operator.ops import Result, Runner
from incident_operator.triage import Config, Engine
from test_evidence import FakeK8s
from test_runbooks import CHART, FINDINGS, FakeProm, ScriptedGpu, load

NODE = {"metadata": {"name": "k8s-worker1"},
        "status": {"addresses": [{"type": "InternalIP", "address": "192.168.0.36"}],
                   "conditions": [{"type": "Ready", "status": "True"}]}}
WEDGED = {"mount_nfs_dstate_total": 40, "mount_nfs_dstate_older_than_10m": 38, "parents_are_csi_nfs_node": True}
CLEAR = {"mount_nfs_dstate_total": 0, "mount_nfs_dstate_older_than_10m": 0, "parents_are_csi_nfs_node": False}
ADVICE_FIX = {"suggestion": "Restart the node's CSI pod.", "operation": "restart-csi-nfs-node", "why": "w",
              "will_it_fix_itself": "No.", "escalate_to_human": ""}


class Cluster(FakeK8s):
    """FakeK8s that also records deletes and creates (the executor's verbs)."""

    def __init__(self, routes):
        super().__init__(routes)
        self.deleted, self.posted = [], []

    def delete(self, path, params=None):
        self.deleted.append(path)

    def post(self, path, body):
        self.posted.append((path, body))
        return body


class ProbeStub(Runner):
    """The nfs-dstate probe answers from `self.dstate` instead of running a Job on a node."""

    dstate = WEDGED

    def _probe(self, op, params):
        return Result(op.name, f"probe on {params['node']}", str(self.dstate), data=dict(self.dstate))


class Mailbox:
    def __init__(self):
        self.incidents, self.fixes = [], []

    def send_incident(self, inc):
        self.incidents.append(inc)

    def send_fix(self, run, inc):
        self.fixes.append(run)


def cluster(pods=1, ds_unavailable=None):
    ds = {"status": {"desiredNumberScheduled": 4, "numberReady": 4 - (ds_unavailable or 0)}}
    if ds_unavailable:
        ds["status"]["numberUnavailable"] = ds_unavailable
    return Cluster({
        "/api/v1/nodes": {"items": [NODE]}, "/api/v1/nodes/k8s-worker1": NODE,
        "/api/v1/events": {"items": []}, "/api/v1/namespaces/default/events": {"items": []},
        "/api/v1/namespaces/kube-system/pods": {"items": [{"metadata": {"name": f"csi-nfs-node-{i}"}} for i in range(pods)]},
        "/apis/apps/v1/namespaces/kube-system/daemonsets/csi-nfs-node": ds,
    })


def setup(conn, tmp_path, replies=None, k8s=None, lib=None):
    k8s = k8s or cluster()
    runner = ProbeStub(k8s, FakeProm())
    runner.dstate = WEDGED
    mail = Mailbox()
    primer = tmp_path / "primer.md"
    primer.write_text("")
    e = Engine(conn, Config(primer_path=str(primer)), k8s, ScriptedGpu(replies or [
        {"action": "done", "requests": [], "reason": "r", "findings": FINDINGS}, ADVICE_FIX]), mail,
        library=lib or load(CHART), runner=runner)
    return e, k8s, runner, mail


def wedge_alert(fp="w1"):
    return alert(fp, alertname="NodeLoadHighCPUIdle", instance="192.168.0.36:9100")


def analysed(conn, e):
    inc_id, _ = db.record_firing(conn, wedge_alert())
    for _ in range(4):
        e.step()
    return db.get_incident(conn, inc_id)


def approve(conn, run_id, env=None):
    with TestClient(create_ui_app(conn.info.dsn, None, {"ALLOW_ANONYMOUS_DECISIONS": "1"} if env is None else env)) as c:
        return c.post(f"/api/fixes/{run_id}/approve", json={})


# -- files ------------------------------------------------------------------------------------------------------------

def test_shipped_fix_loads_and_stays_off_the_investigation_menu():
    lib = load(CHART)
    op, rb = lib.operations["restart-csi-nfs-node"], lib.runbooks["nfs-client-wedge"]
    assert op.mutating and op.autonomy == "suggest" and rb.fixes == {"restart-csi-nfs-node": "approve"}
    assert "restart-csi-nfs-node" not in rb.operations
    assert op.limits == {"perTarget": (1, 3600), "global": (3, 86400)} and op.settle_seconds == 120
    assert fixes.command(op, {"node": "k8s-worker1"}) == \
        "kubectl -n kube-system delete pod -l app=csi-nfs-node --field-selector spec.nodeName=k8s-worker1"


@pytest.mark.parametrize("change, error", [
    (("post:", "nopost:"), "post is required"),
    (("namespaces: [kube-system]", "namespaces: [default]"), "run.namespace"),
    (("== 0\"}", "is 0\"}"), "expect"),
    (("node: {from: target.name}", "node: {from: result:nfs-dstate.node}"), "never from check results"),
    (("autonomy: suggest ", "autonomy: auto "), "autonomy"),
    (("maxPods: 1", "maxPods: 50"), "maxPods"),
    (("{op: csi-nfs-node-status,", "{op: restart-csi-nfs-node,"), "mutating operations"),
])
def test_fix_validation(tmp_path, change, error):
    shutil.copytree(CHART / "runbooks", tmp_path / "runbooks")
    shutil.copytree(CHART / "operations", tmp_path / "operations")
    p = tmp_path / "operations" / "restart-csi-nfs-node.yaml"
    assert change[0] in p.read_text()
    p.write_text(p.read_text().replace(change[0], change[1]))
    lib = Library(str(tmp_path / "runbooks"), str(tmp_path / "operations"))
    lib.refresh()
    assert error in lib.errors["operations/restart-csi-nfs-node.yaml"]
    assert "restart-csi-nfs-node" not in lib.operations                  # and the NFS runbook can't load without it
    assert "runbooks/nfs-client-wedge.md" in lib.errors


def test_expectations():
    assert fixes.evaluate("a.b > 0", {"a": {"b": 3}}) == (True, "a.b = 3")
    assert fixes.evaluate("x == true", {"x": True})[0] and not fixes.evaluate("x == true", {"x": 1})[0]
    assert fixes.evaluate("missing == null", {})[0]
    assert not fixes.evaluate("x > 0", {"x": "5"})[0]                   # strings never compare as numbers
    assert fixes.evaluate('s == "Running"', {"s": "Running"})[0]


# -- the whole path ---------------------------------------------------------------------------------------------------

@needs_pg
def test_proposed_approved_executed_and_verified(conn, tmp_path):
    e, k8s, runner, mail = setup(conn, tmp_path)
    inc = analysed(conn, e)
    assert inc["status"] == "analysed" and "Proposed fix: restart-csi-nfs-node" in inc["analysis"]
    remediation = e.gpu.submitted[-1]
    assert remediation[1]["properties"]["operation"]["enum"] == ["restart-csi-nfs-node", "none"]
    assert "restart-csi-nfs-node:" in remediation[0][0]["content"]
    [run] = inc["fixes"]
    assert run["status"] == "proposed" and run["params"] == {"node": "k8s-worker1"} and run["autonomy"] == "approve"
    assert "Proposed fix, waiting for your approval" in _mail_text(e, inc)

    e.step()                                                          # nothing runs without approval
    assert db.get_op_run(conn, run["id"])["status"] == "proposed" and not k8s.deleted

    assert approve(conn, run["id"]).status_code == 200
    assert approve(conn, run["id"]).status_code == 409                # once
    e.step()                                                          # pre-checks against a fresh probe
    r = db.get_op_run(conn, run["id"])
    assert r["status"] == "ready" and r["decided_by"] == "anonymous (local)" and r["checks"].count("pass:") == 2
    pre = [x for x in db.get_incident(conn, inc["id"])["evidence"] if x["trigger"] == "pre"]
    assert [x["operation"] for x in pre] == ["nfs-dstate"]            # one probe for both expectations

    lib = Library(None, str(CHART / "operations"))
    assert executor.step(conn, lib, k8s) == 1
    assert k8s.deleted == ["/api/v1/namespaces/kube-system/pods/csi-nfs-node-0"]
    r = db.get_op_run(conn, run["id"])
    assert r["status"] == "settling" and "deleted pod kube-system/csi-nfs-node-0" in r["result"]

    e.step()
    assert db.get_op_run(conn, run["id"])["status"] == "settling"     # waits out `settle`
    conn.execute("UPDATE op_runs SET post_due_at = now() WHERE id = %s", (run["id"],))
    runner.dstate = CLEAR
    e.step()
    r = db.get_op_run(conn, run["id"])
    assert r["status"] == "succeeded" and r["finished_at"] and "FAIL" not in r["checks"]
    assert [f["status"] for f in mail.fixes] == ["succeeded"]
    e.step()
    assert len(mail.fixes) == 1                                       # each outcome mailed once
    rec = {x["operation"]: x for x in db.operation_records(conn)}["restart-csi-nfs-node"]
    assert (rec["ran"], rec["succeeded"]) == (1, 1)


@needs_pg
def test_post_check_failure_is_reported(conn, tmp_path):
    e, k8s, runner, mail = setup(conn, tmp_path)
    run = analysed(conn, e)["fixes"][0]
    approve(conn, run["id"]); e.step()
    executor.step(conn, Library(None, str(CHART / "operations")), k8s)
    conn.execute("UPDATE op_runs SET post_due_at = now()")
    e.step()                                                          # the mounts came back: Tower's side
    r = db.get_op_run(conn, run["id"])
    assert r["status"] == "post_failed" and "FAIL: nfs-dstate expects mount_nfs_dstate_total == 0" in r["checks"]
    assert mail.fixes[-1]["status"] == "post_failed"


@needs_pg
def test_pre_check_refuses_when_the_wedge_has_cleared(conn, tmp_path):
    e, k8s, runner, mail = setup(conn, tmp_path)
    run = analysed(conn, e)["fixes"][0]
    runner.dstate = CLEAR
    approve(conn, run["id"]); e.step()
    r = db.get_op_run(conn, run["id"])
    assert r["status"] == "refused" and "FAIL" in r["checks"] and not k8s.deleted
    assert mail.fixes[-1]["status"] == "refused"


@needs_pg
def test_limits_and_changed_operation_refuse(conn, tmp_path):
    e, k8s, runner, mail = setup(conn, tmp_path)
    first = analysed(conn, e)["fixes"][0]
    approve(conn, first["id"]); e.step()
    assert db.get_op_run(conn, first["id"])["status"] == "ready"
    second = db.add_op_run(conn, first["incident_id"], first["operation"], first["op_rev"], first["params"],
                           "approve", "proposed")
    approve(conn, second); e.step()
    assert db.get_op_run(conn, second)["error"].startswith("limit: restart-csi-nfs-node already ran 1 time")
    third = db.add_op_run(conn, first["incident_id"], first["operation"], "0ldrev", {"node": "k8s-worker2"},
                          "approve", "proposed")
    approve(conn, third); e.step()
    assert "changed since it was proposed" in db.get_op_run(conn, third)["error"]


@needs_pg
def test_executor_refuses_what_the_operation_does_not_allow(conn, tmp_path):
    lib = Library(None, str(CHART / "operations"))
    lib.refresh()
    op = lib.operations["restart-csi-nfs-node"]
    inc_id, _ = db.record_firing(conn, wedge_alert())
    for params in ({"node": "k8s-worker1 --all"}, {"node": "k8s-worker1", "namespace": "default"}):
        rid = db.add_op_run(conn, inc_id, op.name, op.rev, params, "approve", "ready")
        executor.step(conn, lib, cluster())
        assert db.get_op_run(conn, rid)["status"] == "failed"
    k8s = cluster(pods=2)                                             # selector matches more than maxPods
    rid = db.add_op_run(conn, inc_id, op.name, op.rev, {"node": "k8s-worker1"}, "approve", "ready")
    executor.step(conn, lib, k8s)
    r = db.get_op_run(conn, rid)
    assert r["status"] == "failed" and "more than maxPods=1" in r["error"] and not k8s.deleted


@needs_pg
def test_decisions_need_sign_in_and_json(conn):
    inc_id, _ = db.record_firing(conn, wedge_alert())
    rid = db.add_op_run(conn, inc_id, "restart-csi-nfs-node", "r", {"node": "n"}, "approve", "proposed")
    assert approve(conn, rid, env={}).status_code == 403              # open LAN page: no approvals
    with TestClient(create_ui_app(conn.info.dsn, None, {"ALLOW_ANONYMOUS_DECISIONS": "1"})) as c:
        assert c.post(f"/api/fixes/{rid}/approve", content="x",
                      headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415
        assert c.get("/api/me").json() == {"user": None, "can_decide": True}
        assert c.post(f"/api/fixes/{rid}/reject", json={}).json()["status"] == "rejected"
    sid = db.add_op_run(conn, inc_id, "restart-csi-nfs-node", "r", {"node": "n"}, "suggest", "suggested")
    assert approve(conn, sid).status_code == 409                      # suggest-only never runs from here


def test_signed_in_ui_redirects_and_guards_the_api():
    app = create_ui_app("postgresql://unused", None, {"OIDC_ISSUER": "https://auth.example/application/o/x/",
                                                      "OIDC_CLIENT_SECRET": "s", "PUBLIC_URL": "http://io.local"})
    c = TestClient(app)                                               # no `with`: the pool isn't needed
    assert c.get("/api/incidents").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("/api/auth/oidc/login")
    assert c.post("/api/fixes/1/approve", json={}).status_code == 401


def test_chart_generates_rbac_from_the_fix_files(tmp_path):
    if not shutil.which("helm"):
        pytest.skip("helm not installed")
    chart = tmp_path / "incident-operator"
    shutil.copytree(CHART.parent, chart)
    (chart / "files" / "operations" / "rerun.yaml").write_text(
        "name: rerun\ndescription: d\nmutating: true\nparams: {ns: {from: alertLabel:namespace}, cj: {from: "
        "alertLabel:cronjob}}\nrun: {verb: CreateJobFrom, namespaces: [postgres, offsite-backup], namespace: '{ns}', "
        "cronjob: '{cj}'}\npost: {settle: 30, checks: [{op: cronjob-history, expect: 'x == 1'}]}\n"
        "limits: {perTarget: 1/1h, global: 2/24h}\n")
    out = subprocess.run(["helm", "template", "t", str(chart), "-n", "incident-operator"], capture_output=True,
                         text=True, check=True).stdout
    roles = [d for d in out.split("\n---\n") if "kind: Role\n" in d and "incident-operator-fix-" in d]
    assert sorted((r.split("namespace: ")[1].split("\n")[0], "delete" in r, "create" in r) for r in roles) == [
        ("kube-system", True, False), ("offsite-backup", False, True), ("postgres", False, True)]


def _mail_text(e, inc):
    from incident_operator.notify import SmtpConfig, compose
    return compose(db.get_incident(e.conn, inc["id"]), SmtpConfig(sender="a@b", to=["c@d"])).get_content()
