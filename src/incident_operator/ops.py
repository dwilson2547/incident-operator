"""Running read-only operations (docs/design.md §5): resolve parameters, run the query, cap the result.

Parameters come only from the derived target, the alert's labels and earlier results in the same incident, never
from model output (design §6.6), and a value must look like a Kubernetes name or label value before it goes into a
path or a query. Every result is text for the model plus, where it has one, a structured value that a later
operation's `result:<operation>.<path>` parameter can read. Nothing here writes anything.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from . import evidence
from .library import Operation

log = logging.getLogger("incident_operator.ops")

SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,252}$")
SERIES_MAX = 8            # promql: series shown per query
POINTS_MAX = 24           # promql-range: points per series after downsampling


@dataclass
class Result:
    operation: str
    source: str              # what was read, for the page and the prompt heading
    text: str | None
    data: Any = None         # structured result, for result:<op>.<path> params
    truncated: bool = False
    error: str | None = None


class Prometheus:
    def __init__(self, url: str | None = None):
        self.url = (url or os.environ.get("PROMETHEUS_URL")
                    or "http://prometheus-kube-prometheus-prometheus.monitoring.svc:9090").rstrip("/")

    def get(self, path: str, params: dict) -> dict:
        import httpx
        r = httpx.get(self.url + path, params=params, timeout=20)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(body.get("error") or "prometheus query failed")
        return body["data"]


class ParamError(ValueError):
    pass


def dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def resolve_params(op: Operation, target: dict | None, labels: dict, earlier: dict[str, Result]) -> dict[str, str]:
    out = {}
    for name, spec in op.params.items():
        src = spec["from"]
        if src.startswith("target."):
            val = (target or {}).get(src.split(".", 1)[1])
        elif src.startswith("alertLabel:"):
            val = labels.get(src.split(":", 1)[1])
        else:                                           # result:<operation>.<path>
            other, path = src.split(":", 1)[1].split(".", 1)
            if other not in earlier or earlier[other].data is None:
                raise ParamError(f"{name} needs the result of {other}, which hasn't run (or failed)")
            val = dig(earlier[other].data, path)
        if val is None or val == "":
            raise ParamError(f"{name} ({src}) has no value for this alert")
        val = str(val)
        if not SAFE_VALUE.match(val):
            raise ParamError(f"{name} ({src}) has a value that isn't a plain name: refusing to use it")
        out[name] = val
    return out


def _fill(template: Any, params: dict[str, str], escape=lambda v: v) -> Any:
    if isinstance(template, str):
        return re.sub(r"\{(\w+)\}", lambda m: escape(params[m.group(1)]), template)
    if isinstance(template, dict):
        return {k: _fill(v, params, escape) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, params, escape) for v in template]
    return template


def _promql_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def _cap(text: str, limit: int, tail: bool = False) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return (("…" + text[-limit:]) if tail else (text[:limit] + "…")), True


def probe_job(name: str, node: str, probe: str, image: str, deadline: int) -> dict:
    """The probe Job (design §5.3): pinned to the node, host PID namespace so /proc shows the node's tasks, and
    otherwise as little as Kubernetes allows: no volumes, no token, non-root, no capabilities, read-only root."""
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/part-of": "incident-operator",
                                              "incident-operator/probe": probe}},
        "spec": {
            "backoffLimit": 0, "activeDeadlineSeconds": deadline, "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {"labels": {"incident-operator/probe": probe}},
                "spec": {
                    "nodeName": node,                  # pinned: no scheduler, so a cordoned node can still be read
                    "hostPID": True,
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "restartPolicy": "Never",
                    "tolerations": [{"operator": "Exists"}],
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "probe", "image": image,
                        "command": ["python", "-m", "incident_operator.probes", probe],
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                                      "limits": {"memory": "128Mi"}},
                    }],
                },
            },
        },
    }


class Runner:
    def __init__(self, k8s, prom: Prometheus | None = None, probe_namespace: str | None = None,
                 probe_image: str | None = None):
        self.k8s, self.prom = k8s, prom or Prometheus()
        self.probe_namespace = probe_namespace or os.environ.get("PROBE_NAMESPACE")
        self.probe_image = probe_image or os.environ.get("PROBE_IMAGE")

    def run(self, op: Operation, target: dict | None, labels: dict, earlier: dict[str, Result]) -> Result:
        try:
            params = resolve_params(op, target, labels, earlier)
        except ParamError as e:
            return Result(op.name, op.name, None, error=str(e))
        kind = op.run["kind"]
        try:
            if kind.startswith("promql"):
                res = self._promql(op, params)
            elif kind == "alert-objects":
                res = self._items(op, evidence.collect(self.k8s, labels))
            elif kind == "k8s-summary":
                res = self._summary(op, params)
            elif kind == "k8s-get":
                res = self._get(op, params)
            elif kind == "k8s-events":
                res = self._events(op, params)
            elif kind == "probe":
                res = self._probe(op, params)
            else:
                res = self._logs(op, params)
        except Exception as e:                          # one failed read never stops the incident
            log.info("operation %s failed: %s", op.name, e)
            return Result(op.name, op.name, None, error=f"{type(e).__name__}: {e}"[:400])
        if res.text is not None:
            res.text, cut = _cap(res.text, op.budget_chars, tail=kind == "k8s-logs")
            res.truncated = res.truncated or cut
        return res

    # -- kinds -----------------------------------------------------------------------------------------------------

    def _items(self, op: Operation, items: list) -> Result:
        text = evidence.render(items) if items else "(the alert names no pod, job, workload or node)"
        return Result(op.name, op.name, text, truncated=any(i.truncated for i in items))

    def _summary(self, op: Operation, params: dict) -> Result:
        obj = _fill(op.run["object"], params)
        kind, ns, name = obj["kind"], obj.get("namespace"), obj["name"]
        if kind == "Pod":
            items = evidence._pod(self.k8s, ns, name)
        elif kind == "Job":
            items = evidence._job(self.k8s, ns, name)
        elif kind == "Node":
            items = evidence._node(self.k8s, name)
        else:
            plural = {"Deployment": "deployments", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets"}[kind]
            items = evidence._workload(self.k8s, ns, kind.lower(), plural, "apps/v1", name)
        if not op.run.get("logs", True):
            items = [i for i in items if i.kind != "logs"]
        res = self._items(op, items)
        res.source = f"{kind} {ns + '/' if ns else ''}{name}"
        return res

    def _get(self, op: Operation, params: dict) -> Result:
        path = _fill(op.run["path"], params)
        obj = self.k8s.get(path)
        lines = []
        for f in op.run["fields"]:
            v = dig(obj, f)
            lines.append(f"{f}: {_compact(v)}")
        return Result(op.name, path, "\n".join(lines), data=obj)

    def _events(self, op: Operation, params: dict) -> Result:
        run = _fill(op.run, params)
        ns = run.get("namespace")
        path = f"/api/v1/namespaces/{ns}/events" if ns else "/api/v1/events"
        sel = []
        if run.get("reason"):
            sel.append(f"reason={run['reason']}")
        if obj := run.get("object"):
            sel.append(f"involvedObject.name={obj}")
        evs = self.k8s.get(path, {"fieldSelector": ",".join(sel)} if sel else None).get("items", [])
        if host := run.get("host"):                     # kubelet-reported events carry the node they came from
            evs = [e for e in evs if host in ((e.get("source") or {}).get("host"), e.get("reportingInstance"))]
        evs.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
        limit = int(run.get("limit", 15))
        lines = [f"{e.get('lastTimestamp') or e.get('eventTime')} {e.get('type')} {e.get('reason')} "
                 f"{e['involvedObject'].get('kind')}/{e['involvedObject'].get('namespace', '')}/"
                 f"{e['involvedObject'].get('name')}: {' '.join((e.get('message') or '').split())[:300]}"
                 + (f" (x{e['count']})" if (e.get("count") or 1) > 1 else "") for e in evs[:limit]]
        desc = " ".join(f"{k}={run[k]}" for k in ("namespace", "reason", "host", "object") if run.get(k))
        return Result(op.name, f"events {desc}", "\n".join(lines) or "(no events: they are kept about an hour)",
                      data={"count": len(evs)}, truncated=len(evs) > limit)

    def _logs(self, op: Operation, params: dict) -> Result:
        run = _fill(op.run, params)
        ns, pod = run["namespace"], run.get("pod")
        if not pod:                                     # the Job's newest pod
            pods = self.k8s.get(f"/api/v1/namespaces/{ns}/pods", {"labelSelector": f"job-name={run['job']}"})["items"]
            if not pods:
                return Result(op.name, f"logs of Job {ns}/{run['job']}", "(the Job has no pods left)")
            pod = max(pods, key=lambda p: p["metadata"].get("creationTimestamp", ""))["metadata"]["name"]
        q = {"tailLines": str(int(run.get("lines", 80))), "previous": "true" if run.get("previous") else "false"}
        if run.get("container"):
            q["container"] = run["container"]
        text = self.k8s.get(f"/api/v1/namespaces/{ns}/pods/{pod}/log", q, text=True)
        return Result(op.name, f"logs {ns}/{pod}" + (" (previous container)" if run.get("previous") else ""),
                      text.rstrip() or "(empty)")

    def _probe(self, op: Operation, params: dict) -> Result:
        """Run a probe (probes.py) as a Job pinned to the node, wait for its one line of JSON, delete the Job.
        The pod gets hostPID and nothing else (design §5.3). A probe that doesn't finish in time is reported as such:
        on a wedged node that is itself a finding."""
        import json as _json
        import uuid
        node, name = _fill(op.run["node"], params), op.run["probe"]
        ns, image = self.probe_namespace, self.probe_image
        if not (ns and image):
            raise RuntimeError("probes are not configured (PROBE_NAMESPACE, PROBE_IMAGE)")
        job = f"probe-{name}-{uuid.uuid4().hex[:6]}"
        deadline = int(op.run.get("timeoutSeconds", 60))
        self.k8s.post(f"/apis/batch/v1/namespaces/{ns}/jobs", probe_job(job, node, name, image, deadline))
        try:
            end = time.time() + deadline + 30                 # + time to pull the image and start
            while time.time() < end:
                pods = self.k8s.get(f"/api/v1/namespaces/{ns}/pods", {"labelSelector": f"job-name={job}"})["items"]
                phase = pods[0]["status"].get("phase") if pods else None
                if phase in ("Succeeded", "Failed"):
                    out = self.k8s.get(f"/api/v1/namespaces/{ns}/pods/{pods[0]['metadata']['name']}/log", {},
                                       text=True).strip().splitlines()
                    if phase == "Failed" or not out:
                        raise RuntimeError(f"probe pod {phase.lower()}: {(out or ['no output'])[-1][:300]}")
                    data = _json.loads(out[-1])
                    text = "\n".join(f"{k}: {_compact(v)}" for k, v in data.items())
                    return Result(op.name, f"probe {name} on node {node}", text, data=data)
                time.sleep(2)
            raise TimeoutError(f"probe didn't finish in {deadline + 30}s on {node} (a node that can't run a "
                               "60-second /proc read is itself in trouble)")
        finally:
            try:
                self.k8s.delete(f"/apis/batch/v1/namespaces/{ns}/jobs/{job}", {"propagationPolicy": "Background"})
            except Exception as e:                            # ttlSecondsAfterFinished cleans up anyway
                log.warning("could not delete probe job %s: %s", job, e)

    def _promql(self, op: Operation, params: dict) -> Result:
        run = op.run
        out, data = [], {}
        for qname, expr in run["queries"].items():
            expr = _fill(expr, params, _promql_escape)
            if run["kind"] == "promql":
                res = self.prom.get("/api/v1/query", {"query": expr})["result"]
                data[qname] = [{"metric": r["metric"], "value": float(r["value"][1])} for r in res]
                out.append(f"{qname}:" + ("".join(f"\n  {_series(r['metric'])} = {_num(r['value'][1])}"
                                                   for r in res[:SERIES_MAX]) or " (no data)"))
            else:
                end = time.time()
                start = end - _seconds(run["range"])
                res = self.prom.get("/api/v1/query_range", {"query": expr, "start": start, "end": end,
                                                            "step": run["step"]})["result"]
                data[qname] = [{"metric": r["metric"], "last": float(r["values"][-1][1])} for r in res if r["values"]]
                lines = []
                for r in res[:SERIES_MAX]:
                    vals = [v for _, v in r["values"]]
                    every = max(1, -(-len(vals) // POINTS_MAX))
                    lines.append(f"\n  {_series(r['metric'])}: " + " ".join(_num(v) for v in vals[::every]))
                out.append(f"{qname} (oldest to newest, every {run['step']}"
                           + (f" x{every}" if res and every > 1 else "") + f", last {run['range']}):"
                           + ("".join(lines) or " (no data)"))
        return Result(op.name, op.name, "\n".join(out), data=data,
                      truncated=any(len(v) > SERIES_MAX for v in data.values()))


def _series(metric: dict) -> str:
    # the exporter's own labels (kube-state-metrics' address and pod) say nothing about the target
    noise = {"__name__", "job", "endpoint", "service", "prometheus", "uid"}
    if metric.get("job") == "kube-state-metrics" or "exported_namespace" in metric:
        noise |= {"instance", "pod", "container"}
    keep = {k: v for k, v in metric.items() if k not in noise}
    return "{" + ",".join(f"{k}={v}" for k, v in sorted(keep.items())) + "}" if keep else "{}"


def _num(v) -> str:
    f = float(v)
    return f"{f:.0f}" if abs(f) >= 100 else f"{f:.3g}"


def _seconds(span: str) -> int:
    m = re.fullmatch(r"(\d+)([smhd])", span)
    if not m:
        raise ValueError(f"bad duration {span!r}")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _compact(v: Any) -> str:
    import json
    s = v if isinstance(v, str) else json.dumps(v, separators=(", ", ": "), default=str)
    return s if len(s) <= 600 else s[:600] + "…"
