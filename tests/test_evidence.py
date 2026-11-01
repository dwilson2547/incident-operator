"""Evidence collection against a fake Kubernetes API (shapes as the real API returns them)."""
import pytest

from incident_operator import evidence
from incident_operator.evidence import BUDGET_CHARS, collect, render

POD = {"metadata": {"name": "scraper-abc", "creationTimestamp": "2026-11-01T09:00:00Z"},
       "spec": {"nodeName": "k8s-worker1", "containers": [{"name": "scraper", "resources": {"limits": {"memory": "512Mi"}}}]},
       "status": {"phase": "Running", "startTime": "2026-11-01T09:00:00Z",
                  "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady"}],
                  "containerStatuses": [{"name": "scraper", "ready": False, "restartCount": 4,
                                         "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                                         "lastState": {"terminated": {"reason": "Error", "exitCode": 65}}}]}}
EVENTS = {"items": [{"lastTimestamp": "2026-11-01T09:05:00Z", "type": "Warning", "reason": "BackOff",
                     "message": "Back-off restarting failed container", "count": 12, "metadata": {}}]}


class FakeK8s:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, path, params=None, text=False):
        self.calls.append((path, params))
        key = path + (("?previous=" + params.get("previous", "")) if path.endswith("/log") else "")
        if key not in self.routes:
            raise RuntimeError(f"404 {path}")
        val = self.routes[key]
        if isinstance(val, Exception):
            raise val
        return val


def test_pod_alert_gets_status_events_and_both_logs():
    k = FakeK8s({"/api/v1/namespaces/gyopart/pods/scraper-abc": POD, "/api/v1/namespaces/gyopart/events": EVENTS,
                 "/api/v1/namespaces/gyopart/pods/scraper-abc/log?previous=false": "starting\nfetching page 1",
                 "/api/v1/namespaces/gyopart/pods/scraper-abc/log?previous=true": "Traceback...\nBroken: 0 vehicles"})
    items = collect(k, {"namespace": "gyopart", "pod": "scraper-abc"})
    assert [i.kind for i in items] == ["pod", "events", "logs", "logs"]
    pod = items[0].content
    assert "restarts=4" in pod and "CrashLoopBackOff" in pod and "exitCode=65" in pod and "memory" in pod
    assert "BackOff" in items[1].content and "(x12)" in items[1].content
    assert "previous container" in items[3].source and "0 vehicles" in items[3].content
    text = render(items)
    assert "--- pod gyopart/scraper-abc" in text and "Broken: 0 vehicles" in text


def test_job_alert_reads_the_job_and_its_newest_pod():
    job = {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": 21600},
           "status": {"failed": 1, "startTime": "2026-11-01T02:00:00Z",
                      "conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded",
                                      "message": "Job was active longer than specified deadline"}]}}
    older = {**POD, "metadata": {"name": "old", "creationTimestamp": "2026-10-01T00:00:00Z"}}
    k = FakeK8s({"/apis/batch/v1/namespaces/postgres/jobs/backup-1": job, "/api/v1/namespaces/postgres/events": EVENTS,
                 "/api/v1/namespaces/postgres/pods": {"items": [older, {**POD, "metadata": {"name": "new", "creationTimestamp": "2026-11-01T02:00:00Z"}}]},
                 "/api/v1/namespaces/postgres/pods/new": POD,
                 "/api/v1/namespaces/postgres/pods/new/log?previous=false": "dumping",
                 "/api/v1/namespaces/postgres/pods/new/log?previous=true": "earlier"})
    items = collect(k, {"namespace": "postgres", "job_name": "backup-1"})
    assert items[0].kind == "job" and "DeadlineExceeded" in items[0].content and "activeDeadlineSeconds=21600" in items[0].content
    assert any(i.source == "pod postgres/new" for i in items)              # the newest pod, not the old one


def test_job_with_retries_shows_every_attempt_and_the_first_failure():
    """2026-11-01: attempt 1 was OOMKilled after saving its snapshot; attempt 2 failed on the lock it left. The model
    only saw attempt 2 and blamed the script."""
    job = {"spec": {"backoffLimit": 1}, "status": {"failed": 2, "conditions": []}}
    def pod(name, ts, term):
        return {"metadata": {"name": name, "creationTimestamp": ts},
                "spec": {"containers": [{"name": "restic"}]},
                "status": {"phase": "Failed", "containerStatuses": [{"name": "restic", "state": {"terminated": term}}]}}
    first = pod("bk-1", "2026-11-01T12:00:00Z", {"reason": "OOMKilled", "exitCode": 137})
    second = pod("bk-2", "2026-11-01T12:08:00Z", {"reason": "Error", "exitCode": 1})
    k = FakeK8s({"/apis/batch/v1/namespaces/ob/jobs/bk": job, "/api/v1/namespaces/ob/events": {"items": []},
                 "/api/v1/namespaces/ob/pods": {"items": [second, first]},
                 "/api/v1/namespaces/ob/pods/bk-1": first, "/api/v1/namespaces/ob/pods/bk-2": second,
                 "/api/v1/namespaces/ob/pods/bk-1/log?previous=false": "snapshot saved\n== check" + "x" * 5000,
                 "/api/v1/namespaces/ob/pods/bk-2/log?previous=false": "already initialized"})
    items = collect(k, {"namespace": "ob", "job_name": "bk"})
    attempts = next(i for i in items if i.source.startswith("attempts of job"))
    assert attempts.content.index("bk-1") < attempts.content.index("bk-2") and "OOMKilled" in attempts.content
    pods = [i.source for i in items if i.kind == "pod" and i.source.startswith("pod ")]
    assert pods == ["pod ob/bk-1", "pod ob/bk-2"]                          # first failure, then the newest
    logs = [i for i in items if i.kind == "logs"]
    assert len(logs[0].content) <= evidence.LOG_CHARS // 2 + 1 and "already initialized" in logs[1].content


def test_node_exporter_instance_maps_to_a_node_and_tower_to_nothing():
    node = {"metadata": {"name": "k8s-worker1"},
            "status": {"addresses": [{"type": "InternalIP", "address": "192.168.0.36"}],
                       "conditions": [{"type": "Ready", "status": "True", "reason": "KubeletReady"}],
                       "allocatable": {"cpu": "8", "memory": "32Gi"}}}
    k = FakeK8s({"/api/v1/nodes": {"items": [node]}, "/api/v1/nodes/k8s-worker1": node,
                 "/api/v1/namespaces/default/events": {"items": []}})
    items = collect(k, {"instance": "192.168.0.36:9100"})
    assert items[0].source == "node k8s-worker1" and "Ready=True" in items[0].content
    assert collect(k, {"instance": "tower"}) == []


def test_one_failed_read_keeps_the_rest():
    k = FakeK8s({"/api/v1/namespaces/a/pods/p": POD, "/api/v1/namespaces/a/events": RuntimeError("403 forbidden"),
                 "/api/v1/namespaces/a/pods/p/log?previous=false": "ok", "/api/v1/namespaces/a/pods/p/log?previous=true": "ok2"})
    items = collect(k, {"namespace": "a", "pod": "p"})
    ev = next(i for i in items if i.kind == "events")
    assert ev.content is None and "403" in ev.error and items[0].content
    assert "could not read: RuntimeError: 403 forbidden" in render(items)


def test_whole_set_stays_within_the_budget():
    big = "x" * 4000
    k = FakeK8s({"/api/v1/namespaces/a/pods/p": {**POD, "spec": {**POD["spec"], "containers": [{"name": "c1"}, {"name": "c2"}]},
                                                 "status": {**POD["status"], "containerStatuses": [
                                                     {"name": "c1", "restartCount": 1}, {"name": "c2", "restartCount": 1}]}},
                 "/api/v1/namespaces/a/events": EVENTS,
                 "/api/v1/namespaces/a/pods/p/log?previous=false": big, "/api/v1/namespaces/a/pods/p/log?previous=true": big})
    items = collect(k, {"namespace": "a", "pod": "p", "deployment": "d"})
    assert sum(len(i.content or "") for i in items) <= BUDGET_CHARS
    assert any(i.truncated for i in items)


def test_nothing_to_collect():
    assert collect(FakeK8s({}), {"alertname": "Watchdog"}) == []
    assert "no evidence" in render([])
