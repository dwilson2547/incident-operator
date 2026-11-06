"""Runbooks and operations (docs/design.md §4): files in two folders, validated on load, re-read when they change.

The chart renders `files/runbooks/*.md` and `files/operations/*.yaml` into ConfigMaps mounted as folders, so a push
reaches the pod within a minute and `refresh()` (called on every engine pass) picks it up with no restart. A file that
fails validation is rejected and the last good version of it stays live; the error is listed on the Runbooks page.
With a store (db.LastGood), every file that loads is also saved, so the last good version survives a restart: a pod
that starts with a bad file (a new chart ahead of its image, 2026-11-01) loads the saved one instead. Only files still
present fall back; a file removed from the chart is gone.

A runbook's frontmatter is what code uses (match, target, precursors, operations); its body is what the model reads.
An operation's revision is the hash of its file, a runbook's the hash of its file plus those of the operations it
names, so an incident records exactly what it ran with (design §8.2).

Mutating operations (phase 3, design §7) are the same format with `mutating: true`, a `run.verb` from the executor's
fixed set, `pre` and `post` checks (read-only operations with an expectation each), `limits`, and an `autonomy`. A
runbook offers them in its `operations` list, optionally raising the autonomy: `{restart-csi-nfs-node: {autonomy:
approve}}`. They never appear on the investigation's menu; only the remediation step can propose one.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("incident_operator.library")

# design §4.1, in tokens estimated as characters / 4
BODY_MAX, BODY_WARN, REMEDIATION_MAX, DESCRIPTION_MAX = 1200, 800, 400, 60
READ_KINDS = {"alert-objects", "k8s-summary", "k8s-get", "k8s-events", "k8s-logs", "promql", "promql-range", "probe"}
TARGET_KINDS = {"Pod", "Job", "Deployment", "StatefulSet", "DaemonSet", "Node", "ScrapeJob"}
PARAM_SOURCE = re.compile(r"^(target\.(name|namespace|kind)|alertLabel:[A-Za-z_][A-Za-z0-9_]*|result:[a-z0-9-]+\.[\w.]+)$")
NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
VERBS = {"DeletePods", "CreateJobFrom", "AnnotateScrapeJob"}   # executor.py; the set grows by code change (design §7.2)
SCRAPEJOB_ACTIONS = ("run-now", "resume")          # the only annotations AnnotateScrapeJob may set
AUTONOMY = ("suggest", "approve")                  # `auto` is phase 4
# An expectation on a check's structured result: `<path> <op> <literal>`, e.g. `mount_nfs_dstate_total == 0`.
EXPECT = re.compile(r"^\s*([\w.]+)\s*(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?|true|false|null|\"[^\"]*\")\s*$")
LIMIT = re.compile(r"^(\d+)/(\d+)([mhd])$")


def tokens(text: str) -> int:
    return (len(text) + 3) // 4


@dataclass
class Check:
    op: str
    expect: str


@dataclass
class Operation:
    name: str
    description: str
    params: dict[str, dict]
    run: dict
    budget_chars: int
    rev: str
    path: str
    mutating: bool = False
    autonomy: str = "suggest"
    pre: list[Check] = field(default_factory=list)
    post: list[Check] = field(default_factory=list)
    settle_seconds: int = 0
    limits: dict[str, tuple[int, int]] = field(default_factory=dict)    # perTarget / global -> (runs, seconds)


@dataclass
class Runbook:
    name: str
    description: str
    match: dict[str, Any]
    priority: int
    target: dict | None
    precursors: list[str]
    operations: list[str]          # on-demand menu (precursors are not repeated here unless also listed)
    body: str
    rev: str
    path: str
    warnings: list[str] = field(default_factory=list)
    fixes: dict[str, str] = field(default_factory=dict)    # mutating operation -> effective autonomy

    def section(self, heading: str) -> str | None:
        """The text under `## <heading>` up to the next `## `, or None."""
        m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", self.body, re.M | re.S | re.I)
        return m.group(1).strip() if m else None

    def matches(self, labels: dict[str, str]) -> bool:
        for key, want in self.match.items():
            have = labels.get(key)
            if have is None:
                return False
            options = want if isinstance(want, list) else [want]
            if not any(re.fullmatch(o[1:], have) if o.startswith("~") else o == have for o in options):
                return False
        return True


class LoadError(ValueError):
    pass


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def parse_operation(path: Path, raw: bytes | None = None) -> Operation:
    raw = path.read_bytes() if raw is None else raw
    try:
        d = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise LoadError(f"not valid YAML: {e}") from None
    if not isinstance(d, dict):
        raise LoadError("must be a YAML mapping")
    name = d.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise LoadError("name must be lowercase letters, digits and dashes")
    if name != path.stem:
        raise LoadError(f"name {name!r} must match the file name {path.stem!r}")
    desc = d.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise LoadError("description is required: the model chooses from it")
    if tokens(desc) > DESCRIPTION_MAX:
        raise LoadError(f"description is ~{tokens(desc)} tokens, over {DESCRIPTION_MAX}")
    run = d.get("run")
    mutating = bool(d.get("mutating"))
    if not isinstance(run, dict):
        raise LoadError("run is required")
    if mutating and run.get("verb") not in VERBS:
        raise LoadError(f"a mutating operation's run.verb must be one of {sorted(VERBS)}")
    if not mutating and run.get("kind") not in READ_KINDS:
        raise LoadError(f"run.kind must be one of {sorted(READ_KINDS)}")
    params = d.get("params") or {}
    if not isinstance(params, dict):
        raise LoadError("params must be a mapping")
    for p, spec in params.items():
        src = spec.get("from") if isinstance(spec, dict) else None
        if not isinstance(src, str) or not PARAM_SOURCE.match(src):
            raise LoadError(f"param {p}: from must be target.name|namespace|kind, alertLabel:<label> or "
                            "result:<operation>.<path>")
    budget = (d.get("budget") or {}).get("maxChars", 3000)
    if not isinstance(budget, int) or not 200 <= budget <= 32000:
        raise LoadError("budget.maxChars must be 200..32000")
    op = Operation(name=name, description=" ".join(desc.split()), params=params, run=run, budget_chars=budget,
                   rev=_hash(raw), path=str(path), mutating=mutating)
    if mutating:
        _check_mutating(op, d, set(params))
    else:
        if any(d.get(k) for k in ("pre", "post", "limits", "autonomy")):
            raise LoadError("pre, post, limits and autonomy are for mutating operations")
        _check_run(run, set(params))
    return op


def _checks(items, where: str) -> list[Check]:
    if not isinstance(items, list) or not items:
        raise LoadError(f"{where} must list checks: - {{op: <read-only operation>, expect: \"<path> == <value>\"}}")
    out = []
    for c in items:
        if not isinstance(c, dict) or not isinstance(c.get("op"), str) or not isinstance(c.get("expect"), str):
            raise LoadError(f"{where}: each check needs op and expect")
        if not EXPECT.match(c["expect"]):
            raise LoadError(f"{where}: expect {c['expect']!r} must be `<path> <==|!=|>|>=|<|<=> <number|true|false|"
                            "null|\"text\">`")
        out.append(Check(c["op"], c["expect"]))
    return out


def _check_mutating(op: Operation, d: dict, params: set[str]) -> None:
    """design §4.2 and §7. The executor's RBAC is generated from these files, so what a verb may touch must be
    visible here: the namespaces are listed, never only templated."""
    run = op.run
    if any(spec["from"].startswith("result:") for spec in op.params.values()):
        raise LoadError("a fix's params come from target.* or alertLabel: only, never from check results")
    used = set(re.findall(r"\{(\w+)\}", yaml.safe_dump(run)))
    if unknown := used - params:
        raise LoadError(f"run uses {sorted(unknown)} but params doesn't define them")
    nss = run.get("namespaces")
    if not isinstance(nss, list) or not nss or not all(isinstance(n, str) and NAME.match(n) for n in nss):
        raise LoadError("run.namespaces must list the namespaces this operation may act in (its RBAC is made from it)")
    ns = run.get("namespace")
    if not isinstance(ns, str) or ("{" not in ns and ns not in nss):
        raise LoadError("run.namespace must be one of run.namespaces, or a {param} checked against them")
    if run["verb"] == "DeletePods":
        sel = run.get("selector")
        if not isinstance(sel, dict) or not sel:
            raise LoadError("DeletePods needs a label selector (never every pod in a namespace)")
        if not isinstance(run.get("maxPods", 1), int) or not 1 <= run.get("maxPods", 1) <= 5:
            raise LoadError("DeletePods maxPods must be 1..5")
    elif run["verb"] == "CreateJobFrom":
        if not isinstance(run.get("cronjob"), str):
            raise LoadError("CreateJobFrom needs cronjob")
    elif run["verb"] == "AnnotateScrapeJob":
        if not isinstance(run.get("name"), str):
            raise LoadError("AnnotateScrapeJob needs name")
        if run.get("action") not in SCRAPEJOB_ACTIONS:
            raise LoadError(f"AnnotateScrapeJob action must be one of {SCRAPEJOB_ACTIONS}")
    op.autonomy = d.get("autonomy", "suggest")
    if op.autonomy not in AUTONOMY:
        raise LoadError(f"autonomy must be one of {AUTONOMY} (auto comes later)")
    op.pre = _checks(d["pre"], "pre") if d.get("pre") is not None else []
    post = d.get("post")
    if not isinstance(post, dict):
        raise LoadError("post is required: {settle: <seconds>, checks: [...]}; a fix nobody verifies isn't one")
    op.post = _checks(post.get("checks"), "post.checks")
    op.settle_seconds = post.get("settle", 60)
    if not isinstance(op.settle_seconds, int) or not 0 <= op.settle_seconds <= 3600:
        raise LoadError("post.settle must be 0..3600 seconds")
    limits = d.get("limits")
    if not isinstance(limits, dict) or not {"perTarget", "global"} <= set(limits):
        raise LoadError("limits needs perTarget and global, e.g. {perTarget: 1/1h, global: 3/24h}")
    for k in ("perTarget", "global"):
        m = LIMIT.match(str(limits[k]))
        if not m:
            raise LoadError(f"limits.{k} must look like 1/1h, 3/24h or 2/30m")
        op.limits[k] = (int(m.group(1)), int(m.group(2)) * {"m": 60, "h": 3600, "d": 86400}[m.group(3)])


def _check_run(run: dict, params: set[str]) -> None:
    kind = run["kind"]
    used = set(re.findall(r"\{(\w+)\}", yaml.safe_dump(run)))
    if unknown := used - params:
        raise LoadError(f"run uses {sorted(unknown)} but params doesn't define them")
    if kind in ("promql", "promql-range"):
        q = run.get("queries")
        if not isinstance(q, dict) or not q or not all(isinstance(v, str) for v in q.values()):
            raise LoadError("run.queries must map names to PromQL expressions")
        if kind == "promql-range" and not (isinstance(run.get("range"), str) and isinstance(run.get("step"), str)):
            raise LoadError("promql-range needs range and step, e.g. 6h and 10m")
    elif kind == "k8s-summary":
        obj = run.get("object") or {}
        if obj.get("kind") not in TARGET_KINDS - {"ScrapeJob"} or not obj.get("name"):
            raise LoadError("run.object needs kind (Pod, Job, Deployment, StatefulSet, DaemonSet, Node) and name")
    elif kind == "k8s-get":
        if not isinstance(run.get("path"), str) or not run["path"].startswith("/"):
            raise LoadError("run.path must be an API path such as /apis/<group>/<version>/namespaces/{ns}/<plural>/{name}")
        if not isinstance(run.get("fields"), list) or not run["fields"]:
            raise LoadError("run.fields must list the dotted paths to show, e.g. status.lastRun")
    elif kind == "k8s-logs":
        if not (run.get("pod") or run.get("job")) or not run.get("namespace"):
            raise LoadError("run needs namespace and pod (or job: the Job's newest pod)")
    elif kind == "probe":
        from .probes import PROBES
        if run.get("probe") not in PROBES:
            raise LoadError(f"run.probe must be one of {sorted(PROBES)} (probes are code in the image)")
        if not run.get("node"):
            raise LoadError("run.node is required: the node to probe, e.g. \"{node}\" from target.name")
    elif kind == "k8s-events":
        if not any(run.get(k) for k in ("reason", "host", "object")):
            raise LoadError("run needs at least one of reason, host, object (all events in a namespace is too much)")


def parse_runbook(path: Path, operations: dict[str, Operation], raw: bytes | None = None) -> Runbook:
    raw = path.read_bytes() if raw is None else raw
    text = raw.decode()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise LoadError("needs YAML frontmatter between --- lines, then the body")
    try:
        d = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise LoadError(f"frontmatter is not valid YAML: {e}") from None
    if not isinstance(d, dict):
        raise LoadError("frontmatter must be a YAML mapping")
    body = m.group(2).strip()
    name = d.get("name")
    if not isinstance(name, str) or not NAME.match(name) or name != path.stem:
        raise LoadError("name must be lowercase-with-dashes and match the file name")
    desc = " ".join(str(d.get("description") or "").split())
    if not desc:
        raise LoadError("description is required")
    match = d.get("match")
    if not isinstance(match, dict) or "alertname" not in match:
        raise LoadError("match must be a mapping with at least alertname")
    for k, v in match.items():
        opts = v if isinstance(v, list) else [v]
        if not opts or not all(isinstance(o, str) for o in opts):
            raise LoadError(f"match.{k} must be a string or a list of strings (prefix ~ for a regex)")
        for o in opts:
            if o.startswith("~"):
                try:
                    re.compile(o[1:])
                except re.error as e:
                    raise LoadError(f"match.{k}: bad regex {o!r}: {e}") from None
    target = d.get("target")
    if target is not None:
        if not isinstance(target, dict) or target.get("kind") not in TARGET_KINDS:
            raise LoadError(f"target.kind must be one of {sorted(TARGET_KINDS)}")
        if target.get("from") not in (None, "instanceToNode"):
            raise LoadError("target.from can only be instanceToNode; otherwise name the labels with name/namespace")
        if not target.get("from") and not target.get("name"):
            raise LoadError("target needs from: instanceToNode, or name: <label> (and namespace: <label>)")
    pre, ops, fixes = d.get("precursors") or [], [], {}
    for entry in d.get("operations") or []:            # a name, or {name: {autonomy: approve}} for a fix
        if isinstance(entry, dict) and len(entry) == 1:
            (n, opts), = entry.items()
            if not isinstance(opts, dict) or set(opts) - {"autonomy"}:
                raise LoadError(f"operations: {n} takes only {{autonomy: ...}}")
            if n in operations and not operations[n].mutating:
                raise LoadError(f"operations: {n} is read-only; autonomy is for mutating operations")
            if opts.get("autonomy") not in (None, *AUTONOMY):
                raise LoadError(f"operations: {n} autonomy must be one of {AUTONOMY}")
            fixes[n] = opts.get("autonomy")
        elif isinstance(entry, str):
            ops.append(entry)
        else:
            raise LoadError("operations entries are names, or {name: {autonomy: ...}}")
    for field_name, names in (("precursors", pre), ("operations", ops + list(fixes))):
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise LoadError(f"{field_name} must be a list of operation names")
        if missing := [n for n in names if n not in operations]:
            raise LoadError(f"{field_name} names operations that aren't loaded: {missing}")
    if bad := [n for n in pre if operations[n].mutating]:
        raise LoadError(f"precursors must be read-only: {bad}")
    for n in [o for o in ops if operations[o].mutating]:            # listed plainly: a fix at its own autonomy
        ops.remove(n)
        fixes[n] = None
    fixes = {n: a or operations[n].autonomy for n, a in fixes.items()}
    for n in set(pre) | set(ops) | set(fixes):
        if any(p["from"].startswith("target.") for p in operations[n].params.values()) and target is None:
            raise LoadError(f"operation {n} needs a target, and this runbook has none")
    # sizes (design §4.1)
    warnings = []
    if tokens(desc) > DESCRIPTION_MAX:
        raise LoadError(f"description is ~{tokens(desc)} tokens, over {DESCRIPTION_MAX}")
    if tokens(body) > BODY_MAX:
        raise LoadError(f"body is ~{tokens(body)} tokens, over {BODY_MAX}: runbooks are for a small model")
    if tokens(body) > BODY_WARN:
        warnings.append(f"body is ~{tokens(body)} tokens (over {BODY_WARN})")
    rb = Runbook(name=name, description=desc, match=match, priority=int(d.get("priority", 0)), target=target,
                 precursors=pre, operations=ops, body=body, rev="", path=str(path), warnings=warnings, fixes=fixes)
    if (rem := rb.section("Remediation")) and tokens(rem) > REMEDIATION_MAX:
        raise LoadError(f"## Remediation is ~{tokens(rem)} tokens, over {REMEDIATION_MAX}")
    used = sorted(set(pre) | set(ops) | set(fixes))
    rb.rev = _hash(raw + "".join(operations[n].rev for n in used).encode())
    return rb


class Library:
    """The loaded runbooks and operations. `refresh()` re-reads the folders when anything in them changed."""

    def __init__(self, runbooks_dir: str | None, operations_dir: str | None, store=None):
        self.store = store                           # db.LastGood, or None (executor, replay, tests)
        self.runbooks_dir = Path(runbooks_dir) if runbooks_dir else None
        self.operations_dir = Path(operations_dir) if operations_dir else None
        self.runbooks: dict[str, Runbook] = {}
        self.operations: dict[str, Operation] = {}
        self.errors: dict[str, str] = {}           # file name -> why it was rejected
        self._seen: tuple | None = None

    @staticmethod
    def _files(d: Path | None, pattern: str) -> list[Path]:
        # ConfigMap mounts hold the files behind ..data symlinks; hidden entries are the mount's own bookkeeping
        return sorted(p for p in d.glob(pattern) if not p.name.startswith(".")) if d and d.is_dir() else []

    def refresh(self) -> bool:
        """Reload if any file changed. Returns True when it reloaded."""
        files = self._files(self.operations_dir, "*.y*ml") + self._files(self.runbooks_dir, "*.md")
        seen = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
        if seen == self._seen:
            return False
        self._seen = seen
        errors: dict[str, str] = {}
        ops: dict[str, Operation] = {}
        for p in self._files(self.operations_dir, "*.y*ml"):
            key = f"operations/{p.name}"
            try:
                raw = p.read_bytes()
                op = parse_operation(p, raw)
                ops[op.name] = op
                self._save(key, raw, op.rev)
            except (LoadError, OSError, UnicodeDecodeError) as e:
                errors[key] = str(e)
                if p.stem in self.operations:                   # the last good version stays live
                    ops[p.stem] = self.operations[p.stem]
                elif old := self._saved(key, lambda raw: parse_operation(p, raw)):
                    ops[p.stem], errors[key] = old[0], f"{e} (serving the last good version, saved {old[1]})"
        def bad_checks(op: Operation) -> list[str]:     # a fix's checks must be loaded read-only operations
            return [c.op for c in op.pre + op.post if c.op not in ops or ops[c.op].mutating]
        for op in [o for o in ops.values() if o.mutating]:
            if bad := bad_checks(op):
                errors[f"operations/{Path(op.path).name}"] = f"pre/post checks name unknown or mutating operations: {bad}"
                old = self.operations.get(op.name)
                if old and old.mutating and not bad_checks(old):
                    ops[op.name] = old
                else:
                    del ops[op.name]
        rbs: dict[str, Runbook] = {}
        for p in self._files(self.runbooks_dir, "*.md"):
            key = f"runbooks/{p.name}"
            try:
                raw = p.read_bytes()
                rb = parse_runbook(p, ops, raw)
                rbs[rb.name] = rb
                self._save(key, raw, rb.rev)
            except (LoadError, OSError, UnicodeDecodeError) as e:
                errors[key] = str(e)
                old = self.runbooks.get(p.stem)
                if old and all(n in ops for n in old.precursors + old.operations + list(old.fixes)):
                    rbs[p.stem] = old
                elif saved := self._saved(key, lambda raw: parse_runbook(p, ops, raw)):
                    rbs[p.stem], errors[key] = saved[0], f"{e} (serving the last good version, saved {saved[1]})"
        self.operations, self.runbooks, self.errors = ops, rbs, errors
        log.info("library: %d runbooks, %d operations, %d rejected%s", len(rbs), len(ops), len(errors),
                 "".join(f"\n  {k}: {v}" for k, v in errors.items()))
        return True

    def _save(self, key: str, raw: bytes, rev: str) -> None:
        if self.store is not None:
            try:
                self.store.save(key, raw, rev)
            except Exception as e:                      # the files still load; only the fallback is missing
                log.warning("library: could not save %s as last good: %s", key, e)

    def _saved(self, key: str, parse) -> tuple | None:
        """The stored last good version of a rejected file, parsed now (it must still load against what's loaded
        now, e.g. a runbook's operations). (object, when it was saved) or None."""
        if self.store is None:
            return None
        try:
            found = self.store.load(key)
            return (parse(found[0]), found[1].strftime("%Y-%m-%d %H:%M UTC")) if found else None
        except Exception as e:
            log.warning("library: no usable last good version of %s: %s", key, e)
            return None

    def match(self, labels: dict[str, str]) -> Runbook | None:
        """The matching runbook with the highest priority (ties: by name), or None."""
        found = [r for r in self.runbooks.values() if r.matches(labels)]
        return min(found, key=lambda r: (-r.priority, r.name)) if found else None

    def describe(self) -> dict:
        return {
            "runbooks": [{"name": r.name, "description": r.description, "match": r.match, "priority": r.priority,
                          "target": r.target, "precursors": r.precursors, "operations": r.operations, "rev": r.rev,
                          "fixes": r.fixes, "warnings": r.warnings, "tokens": tokens(r.body)}
                         for r in sorted(self.runbooks.values(), key=lambda r: r.name)],
            "operations": [{"name": o.name, "description": o.description, "rev": o.rev, "mutating": o.mutating,
                            "kind": o.run.get("verb") if o.mutating else o.run["kind"],
                            **({"autonomy": o.autonomy, "pre": [vars(c) for c in o.pre],
                                "post": [vars(c) for c in o.post], "settle_seconds": o.settle_seconds,
                                "limits": {k: f"{n} per {s // 3600}h" if s >= 3600 else f"{n} per {s // 60}m"
                                           for k, (n, s) in o.limits.items()},
                                "namespaces": o.run["namespaces"]} if o.mutating else {})}
                           for o in sorted(self.operations.values(), key=lambda o: o.name)],
            "errors": [{"file": k, "error": v} for k, v in sorted(self.errors.items())],
        }


def resolve_target(runbook: Runbook, labels: dict[str, str], k8s) -> dict | None:
    """The object the incident is about, from the alert's labels only, never from model output (design §5.4)."""
    t = runbook.target
    if not t:
        return None
    if t.get("from") == "instanceToNode":
        ip = (labels.get("instance") or "").rsplit(":", 1)[0]
        if not ip or k8s is None:
            return None
        for n in k8s.get("/api/v1/nodes").get("items", []):
            if any(a.get("address") == ip for a in n.get("status", {}).get("addresses", [])):
                return {"kind": "Node", "name": n["metadata"]["name"]}
        return None                                    # e.g. Tower: not a node, so no node-targeted operation
    name = labels.get(t["name"])
    if not name:
        return None
    out = {"kind": t["kind"], "name": name}
    if t.get("namespace"):
        out["namespace"] = labels.get(t["namespace"])
    return out
