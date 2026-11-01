"""Read-only evidence for an alert, collected by code before any model call (docs/design.md §5).

Phase 1 has no runbooks, so this is the generic set (design §6.5): whatever the alert's labels name. A pod gets its
status, events and log tail (previous container's too, after a restart); a Job its status, events and its newest pod;
a Deployment/StatefulSet/DaemonSet its status and events; a node (label `node`, or `instance` mapped to a node by IP)
its conditions and events. Every item is capped, and the whole set has a budget, so a small model's prompt stays small.

Kubernetes is read through the API with the pod's own service-account token (a ClusterRole that can only get/list pods,
pods/log, events, jobs, workloads and nodes). Nothing here can change anything.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("incident_operator.evidence")

ITEM_CHARS = 8000
LOG_CHARS = 16000
LOG_LINES = 300
EVENTS = 30
BUDGET_CHARS = 48000            # all evidence for one incident: ~14k tokens, well inside the 9B's 64k context


@dataclass
class Item:
    kind: str
    source: str
    content: str | None
    truncated: bool = False
    error: str | None = None


class K8s:
    """Minimal read-only Kubernetes API client. In the cluster it uses the service-account token and CA; tests pass
    a fake with the same `get`."""

    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self, base: str | None = None, token: str | None = None, verify: Any = True):
        if base is None:
            host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
            base = f"https://{host}:{port}"
            token = open(f"{self.SA}/token").read().strip()
            verify = f"{self.SA}/ca.crt"
        self.base, self.token, self.verify = base, token, verify

    def get(self, path: str, params: dict | None = None, text: bool = False):
        import httpx
        r = httpx.get(self.base + path, params=params, timeout=15, verify=self.verify,
                      headers={"Authorization": f"Bearer {self.token}"} if self.token else {})
        r.raise_for_status()
        return r.text if text else r.json()

    # Used only to create and remove probe Jobs (ops.Runner._probe), in the operator's own namespace.
    def post(self, path: str, body: dict) -> dict:
        import httpx
        r = httpx.post(self.base + path, json=body, timeout=15, verify=self.verify,
                       headers={"Authorization": f"Bearer {self.token}"} if self.token else {})
        r.raise_for_status()
        return r.json()

    def delete(self, path: str, params: dict | None = None) -> None:
        import httpx
        r = httpx.delete(self.base + path, params=params, timeout=15, verify=self.verify,
                         headers={"Authorization": f"Bearer {self.token}"} if self.token else {})
        if r.status_code != 404:
            r.raise_for_status()


def collect(k8s: K8s, labels: dict[str, str]) -> list[Item]:
    ns = labels.get("namespace") or labels.get("exported_namespace")
    steps: list[Callable[[], list[Item]]] = []
    if ns and labels.get("pod"):
        steps.append(lambda: _pod(k8s, ns, labels["pod"], labels.get("container")))
    if ns and labels.get("job_name"):
        steps.append(lambda: _job(k8s, ns, labels["job_name"]))
    for kind, plural, api in (("deployment", "deployments", "apps/v1"), ("statefulset", "statefulsets", "apps/v1"),
                              ("daemonset", "daemonsets", "apps/v1")):
        if ns and labels.get(kind):
            steps.append(lambda k=kind, p=plural, a=api: _workload(k8s, ns, k, p, a, labels[k]))
    node = labels.get("node")
    if not node and labels.get("instance"):
        steps.append(lambda: _node_by_instance(k8s, labels["instance"]))
    elif node:
        steps.append(lambda: _node(k8s, node))

    items: list[Item] = []
    used = 0
    for step in steps:
        try:
            got = step()
        except Exception as e:                                   # one failed read doesn't lose the rest
            log.warning("evidence step failed: %s", e)
            got = [Item("error", "collecting evidence", None, error=f"{type(e).__name__}: {e}"[:300])]
        for it in got:
            size = len(it.content or "")
            if used + size > BUDGET_CHARS:
                keep = max(0, BUDGET_CHARS - used)
                it.content, it.truncated = (it.content or "")[-keep:] if keep else None, True
                if not keep:
                    it.error = it.error or "left out: evidence budget used up"
            used += len(it.content or "")
            items.append(it)
    return items


# -- collectors ------------------------------------------------------------------------------------------------------

def _cap(text: str, limit: int = ITEM_CHARS, tail: bool = False) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return (("…" + text[-limit:]) if tail else (text[:limit] + "…")), True


def _events(k8s: K8s, ns: str, kind: str, name: str) -> Item:
    source = f"events for {kind} {ns}/{name}"
    try:
        evs = k8s.get(f"/api/v1/namespaces/{ns}/events",
                      {"fieldSelector": f"involvedObject.name={name},involvedObject.kind={kind}"})["items"]
    except Exception as e:
        return Item("events", source, None, error=f"{type(e).__name__}: {e}"[:300])
    if not evs:
        return Item("events", source, "(no events: they expire after an hour)")
    evs.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or e["metadata"].get("creationTimestamp") or "")
    lines = [f"{e.get('lastTimestamp') or e.get('eventTime') or ''} {e.get('type', '')} {e.get('reason', '')}: "
             f"{(e.get('message') or '').strip()}" + (f" (x{e['count']})" if e.get("count", 1) > 1 else "")
             for e in evs[-EVENTS:]]
    text, cut = _cap("\n".join(lines))
    return Item("events", source, text, cut)


def _state(s: dict | None) -> str:
    if not s:
        return "-"
    k, v = next(iter(s.items()))                                  # one of waiting / running / terminated
    bits = [k] + [f"{f}={v[f]}" for f in ("reason", "exitCode", "startedAt", "finishedAt") if v.get(f) is not None]
    if v.get("message"):
        bits.append(f"message={v['message'][:300]!r}")
    return " ".join(bits)


def _pod(k8s: K8s, ns: str, name: str, container: str | None = None, log_chars: int = LOG_CHARS) -> list[Item]:
    p = k8s.get(f"/api/v1/namespaces/{ns}/pods/{name}")
    st = p.get("status", {})
    lines = [f"phase: {st.get('phase')}  node: {p['spec'].get('nodeName')}  started: {st.get('startTime')}"]
    for c in st.get("conditions", []):
        if c.get("status") != "True":
            lines.append(f"condition {c['type']}={c['status']} {c.get('reason', '')} {c.get('message', '')}".rstrip())
    restarted = []
    for cs in st.get("initContainerStatuses", []) + st.get("containerStatuses", []):
        lines.append(f"container {cs['name']}: ready={cs.get('ready')} restarts={cs.get('restartCount', 0)} "
                     f"state: {_state(cs.get('state'))}  last: {_state(cs.get('lastState'))}")
        if cs.get("restartCount", 0) > 0:
            restarted.append(cs["name"])
    limits = [f"{c['name']}: {c.get('resources', {}).get('limits', {})}" for c in p["spec"].get("containers", [])]
    lines.append("limits: " + "; ".join(limits))
    text, cut = _cap("\n".join(lines))
    items = [Item("pod", f"pod {ns}/{name}", text, cut), _events(k8s, ns, "Pod", name)]
    names = [container] if container else [c["name"] for c in p["spec"].get("containers", [])]
    for c in names[:2]:
        items.append(_logs(k8s, ns, name, c, previous=False, limit=log_chars))
        if c in restarted:
            items.append(_logs(k8s, ns, name, c, previous=True, limit=log_chars))
    return items


def _logs(k8s: K8s, ns: str, pod: str, container: str, previous: bool, limit: int = LOG_CHARS) -> Item:
    source = f"logs {ns}/{pod} [{container}]" + (" (previous container)" if previous else "")
    try:
        text = k8s.get(f"/api/v1/namespaces/{ns}/pods/{pod}/log",
                       {"container": container, "tailLines": LOG_LINES, "previous": str(previous).lower()}, text=True)
    except Exception as e:
        return Item("logs", source, None, error=f"{type(e).__name__}: {e}"[:300])
    text, cut = _cap(text.strip() or "(empty)", limit, tail=True)
    return Item("logs", source, text, cut)


def _job(k8s: K8s, ns: str, name: str) -> list[Item]:
    j = k8s.get(f"/apis/batch/v1/namespaces/{ns}/jobs/{name}")
    st = j.get("status", {})
    lines = [f"active={st.get('active', 0)} succeeded={st.get('succeeded', 0)} failed={st.get('failed', 0)} "
             f"started: {st.get('startTime')}  completed: {st.get('completionTime')}",
             f"backoffLimit={j['spec'].get('backoffLimit')} activeDeadlineSeconds={j['spec'].get('activeDeadlineSeconds')}"]
    for c in st.get("conditions", []):
        lines.append(f"condition {c['type']}={c['status']} {c.get('reason', '')}: {c.get('message', '')}".rstrip(": "))
    items = [Item("job", f"job {ns}/{name}", _cap("\n".join(lines))[0]), _events(k8s, ns, "Job", name)]
    pods = sorted(k8s.get(f"/api/v1/namespaces/{ns}/pods", {"labelSelector": f"job-name={name}"})["items"],
                  key=lambda p: p["metadata"].get("creationTimestamp", ""))
    if pods:
        # Every attempt, oldest first: with a backoff limit the last attempt can fail differently because of what the
        # first one left behind (2026-11-01: the first was OOMKilled, its retry tripped over the leftover lock).
        attempts = [f"{p['metadata']['name']} created {p['metadata'].get('creationTimestamp')} "
                    f"phase={p.get('status', {}).get('phase')}: "
                    + "; ".join(f"{cs['name']} {_state(cs.get('state'))}"
                                for cs in p.get("status", {}).get("containerStatuses", []))
                    for p in pods]
        items.append(Item("pod", f"attempts of job {ns}/{name}, oldest first", _cap("\n".join(attempts))[0]))
        first_failed = next((p for p in pods if p.get("status", {}).get("phase") == "Failed"), None)
        shown = ([first_failed] if first_failed is not None and first_failed is not pods[-1] else []) + [pods[-1]]
        for p in shown:                               # two pods share what one would get, so the op's budget holds both
            items += _pod(k8s, ns, p["metadata"]["name"], log_chars=LOG_CHARS // len(shown))
    else:
        items.append(Item("pod", f"pods of job {ns}/{name}", "(none left: the job's pods were deleted)"))
    return items


def _workload(k8s: K8s, ns: str, kind: str, plural: str, api: str, name: str) -> list[Item]:
    w = k8s.get(f"/apis/{api}/namespaces/{ns}/{plural}/{name}")
    st = w.get("status", {})
    fields = ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas", "unavailableReplicas",
              "desiredNumberScheduled", "numberReady", "numberUnavailable")
    lines = [" ".join(f"{f}={st[f]}" for f in fields if f in st)]
    for c in st.get("conditions", []):
        lines.append(f"condition {c['type']}={c['status']} {c.get('reason', '')}: {c.get('message', '')}".rstrip(": "))
    return [Item(kind, f"{kind} {ns}/{name}", _cap("\n".join(lines))[0]),
            _events(k8s, ns, kind.capitalize() if kind != "statefulset" else "StatefulSet", name)]


def _node(k8s: K8s, name: str) -> list[Item]:
    n = k8s.get(f"/api/v1/nodes/{name}")
    lines = [f"{c['type']}={c['status']} {c.get('reason', '')}: {c.get('message', '')}".rstrip(": ")
             for c in n.get("status", {}).get("conditions", [])]
    alloc = n.get("status", {}).get("allocatable", {})
    lines.append(f"allocatable: cpu={alloc.get('cpu')} memory={alloc.get('memory')} gpu={alloc.get('nvidia.com/gpu', 0)}")
    return [Item("node", f"node {name}", _cap("\n".join(lines))[0]), _events(k8s, "default", "Node", name)]


def _node_by_instance(k8s: K8s, instance: str) -> list[Item]:
    """node-exporter alerts carry instance="IP:9100" and no node name (design §5.4)."""
    ip = instance.rsplit(":", 1)[0]
    for n in k8s.get("/api/v1/nodes")["items"]:
        if any(a.get("address") == ip for a in n.get("status", {}).get("addresses", [])):
            return _node(k8s, n["metadata"]["name"])
    return []                                                    # not a cluster node (e.g. Tower)


def render(items: list[Item]) -> str:
    """The evidence block as the model sees it."""
    out = []
    for it in items:
        head = f"--- {it.source}" + (" (truncated)" if it.truncated else "")
        body = it.content if it.content is not None else f"(could not read: {it.error})"
        out.append(f"{head}\n{body}")
    return "\n\n".join(out) if out else "(no evidence: the alert names no pod, job, workload or node)"
