"""Node probes (phase 2b): the nfs-dstate reading of /proc, and the Job the operator runs it in."""
import json
import os

import pytest

from incident_operator import probes
from incident_operator.library import Library
from incident_operator.ops import Runner, probe_job
from test_runbooks import CHART, FakeProm, load

BOOT = 1_700_000_000
HZ = os.sysconf("SC_CLK_TCK")


def task(root, pid, comm, state, ppid, start_s, tid=None):
    d = root / str(pid) / "task" / str(tid or pid)
    d.mkdir(parents=True, exist_ok=True)
    fields = [state, str(ppid)] + ["0"] * 17 + [str(int(start_s * HZ))] + ["0"] * 10
    (d / "stat").write_text(f"{tid or pid} ({comm}) " + " ".join(fields) + "\n")


@pytest.fixture
def wedged(tmp_path):
    """worker1 on 2026-10-22: kubelet's mount.nfs piling up in D as children of nfsplugin, no state manager."""
    (tmp_path / "stat").write_text(f"cpu 1 2 3\nbtime {BOOT}\n")
    task(tmp_path, 1, "systemd", "S", 0, 1)
    task(tmp_path, 500, "nfsplugin", "S", 400, 100)
    task(tmp_path, 500, "nfsplugin", "D", 400, 100, tid=501)          # a thread stuck in getattr
    for i in range(5):
        task(tmp_path, 1000 + i, "mount.nfs", "D", 500, 3600 + i * 120)
    task(tmp_path, 2000, "charger_manager", "I", 2, 1)                  # not an NFS manager
    return tmp_path


def test_nfs_dstate_reads_a_wedge(wedged):
    r = probes.nfs_dstate(str(wedged), now=BOOT + 3600 + 800)             # ages 800, 680, 560, ...s
    assert r["mount_nfs_dstate_total"] == 5 and r["dstate_total"] == 6
    assert r["mount_nfs_dstate_older_than_10m"] == 2                   # started at +0s and +120s
    assert r["parents_are_csi_nfs_node"] is True and r["mount_nfs_parents"] == {"nfsplugin": 5}
    assert r["nfsplugin_threads_dstate"] == 1
    assert r["state_managers"] == [] and r["state_manager_dstate"] is False
    assert r["mount_nfs_oldest_start"] == "2023-11-14T23:13:20Z"


def test_state_manager_thread_is_found(wedged):
    task(wedged, 3000, "192.168.0.15-manager", "D", 2, 50)
    r = probes.nfs_dstate(str(wedged), now=BOOT + 10_000)
    assert r["state_managers"] == [{"thread": "192.168.0.15-manager", "state": "D"}] and r["state_manager_dstate"]


def test_healthy_node(tmp_path):
    (tmp_path / "stat").write_text(f"btime {BOOT}\n")
    task(tmp_path, 1, "systemd", "S", 0, 1)
    r = probes.nfs_dstate(str(tmp_path), now=BOOT + 100)
    assert r["dstate_total"] == 0 and r["parents_are_csi_nfs_node"] is False and r["mount_nfs_oldest_start"] is None


def test_cli_prints_one_json_line(capsys):
    assert probes.main(["nope"]) == 2
    assert "usage" in json.loads(capsys.readouterr().out)["error"]


def test_probe_job_is_locked_down():
    spec = probe_job("probe-x", "k8s-worker1", "nfs-dstate", "img:1", 60)["spec"]
    pod = spec["template"]["spec"]
    c = pod["containers"][0]
    assert pod["hostPID"] and pod["nodeName"] == "k8s-worker1" and not pod["automountServiceAccountToken"]
    assert "volumes" not in pod and "volumeMounts" not in c and not pod.get("hostNetwork")
    assert c["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}}
    assert pod["securityContext"]["runAsNonRoot"] and spec["activeDeadlineSeconds"] == 60
    assert c["command"] == ["python", "-m", "incident_operator.probes", "nfs-dstate"]


class ProbeK8s:
    """Creates the Job, reports its pod finished with the given output, records deletes."""

    def __init__(self, output, phase="Succeeded"):
        self.output, self.phase, self.created, self.deleted = output, phase, [], []

    def post(self, path, body):
        self.created.append((path, body))
        return body

    def get(self, path, params=None, text=False):
        if path.endswith("/log"):
            return self.output
        job = self.created[-1][1]["metadata"]["name"]
        assert params == {"labelSelector": f"job-name={job}"}
        return {"items": [{"metadata": {"name": job + "-abc"}, "status": {"phase": self.phase}}]}

    def delete(self, path, params=None):
        self.deleted.append(path)


def test_runner_runs_the_probe_job_and_cleans_up():
    lib = load(CHART)
    out = json.dumps({"mount_nfs_dstate_total": 3, "parents_are_csi_nfs_node": True})
    k = ProbeK8s(out)
    r = Runner(k, FakeProm(), probe_namespace="incident-operator", probe_image="img:1")
    res = r.run(lib.operations["nfs-dstate"], {"kind": "Node", "name": "k8s-worker1"}, {}, {})
    assert res.error is None and res.data["mount_nfs_dstate_total"] == 3
    assert "mount_nfs_dstate_total: 3" in res.text and res.source == "probe nfs-dstate on node k8s-worker1"
    path, body = k.created[0]
    assert path == "/apis/batch/v1/namespaces/incident-operator/jobs"
    assert body["spec"]["template"]["spec"]["nodeName"] == "k8s-worker1"
    assert k.deleted == [f"/apis/batch/v1/namespaces/incident-operator/jobs/{body['metadata']['name']}"]


def test_failed_probe_is_an_error_and_still_cleaned_up():
    lib = load(CHART)
    k = ProbeK8s("Traceback ...\nPermissionError: /proc/1/task", phase="Failed")
    r = Runner(k, FakeProm(), probe_namespace="incident-operator", probe_image="img:1")
    res = r.run(lib.operations["nfs-dstate"], {"kind": "Node", "name": "n"}, {}, {})
    assert "PermissionError" in res.error and len(k.deleted) == 1


def test_probe_without_config_says_so():
    lib = load(CHART)
    res = Runner(ProbeK8s("{}"), FakeProm()).run(lib.operations["nfs-dstate"], {"kind": "Node", "name": "n"}, {}, {})
    assert "not configured" in res.error


def test_unknown_probe_is_rejected_on_load(tmp_path):
    import shutil
    shutil.copytree(CHART / "runbooks", tmp_path / "runbooks")
    shutil.copytree(CHART / "operations", tmp_path / "operations")
    (tmp_path / "operations" / "rootkit.yaml").write_text(
        "name: rootkit\ndescription: d\nparams: {node: {from: target.name}}\n"
        "run: {kind: probe, probe: 'sh -c id', node: '{node}'}\n")
    lib = Library(str(tmp_path / "runbooks"), str(tmp_path / "operations"))
    lib.refresh()
    assert "probes are code in the image" in lib.errors["operations/rootkit.yaml"]
