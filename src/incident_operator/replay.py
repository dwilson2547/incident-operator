"""Replay (docs/design.md §9.5): re-run stored incidents against another model, or against edited runbooks and
prompts, and print the outcome next to what happened at the time.

    python -m incident_operator replay [--model M] [--runbooks DIR] [--operations DIR] ID [ID ...]

Needs DATABASE_URL, GPU_SCHEDULER_URL and GPU_SCHEDULER_TOKEN (locally: port-forwards to the cluster's Postgres and
gpu-scheduler). Runbooks and operations default to the working tree's chart files, so a runbook edit can be tried
before it is pushed.

Operation results come from what was stored for the incident, never from the live cluster: the cluster has moved on,
and replay must not depend on it. A check that wasn't run at the time is answered "not stored", which limits how far a
different model can explore (the design accepts that). The primer is the working tree's too.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from . import db, investigate
from .library import Library
from .ops import Result
from .triage import Config, GpuScheduler

CHART = Path(__file__).resolve().parents[2] / "helm" / "incident-operator" / "files"


def _wait(gpu: GpuScheduler, job_id: str, timeout: float) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = gpu.job(job_id)
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(3)
    raise TimeoutError(f"job {job_id} didn't finish in {timeout:.0f}s")


def _ask(gpu, messages, schema, cfg, timeout) -> tuple[dict | None, str, dict]:
    job = _wait(gpu, gpu.submit(messages, schema, cfg.investigate_max_new_tokens), timeout)
    if job["status"] != "done":
        raise RuntimeError(f"job {job['status']}: {job.get('error')}")
    res = job.get("result") or {}
    return res.get("json"), res.get("text", ""), job


def replay_runbook(inc: dict, lib: Library, gpu: GpuScheduler, cfg: Config, timeout: float) -> dict:
    rb = lib.match(inc["labels"])
    if rb is None:
        return {"error": "no runbook in the working tree matches this alert"}
    stored = {e["operation"]: Result(e["operation"], e["source"], e["content"], e["data"], e["truncated"], e["error"])
              for e in inc["evidence"] if e["operation"]}
    missing = lambda n: Result(n, n, None, error="not stored for this incident (replay doesn't run checks)")
    pre = [stored.get(n) or missing(n) for n in rb.precursors]
    menu, menu_txt = investigate.menu_text(rb, lib.operations)
    messages = investigate.first_messages(inc, rb, inc["target"], pre, menu_txt, cfg.primer())
    turns, findings, model = [], {}, None
    for n in range(1, investigate.MAX_ROUNDS + 1):
        schema = investigate.investigation_schema(menu, final=n >= investigate.MAX_ROUNDS)
        out, text, job = _ask(gpu, messages, schema, cfg, timeout)
        model = f"{job.get('ran_model')} on {job.get('worker')}"
        turns.append(out)
        if not isinstance(out, dict):
            return {"runbook": rb.name, "turns": turns, "error": f"reply wasn't JSON: {text[:200]}"}
        asked = list(dict.fromkeys(out.get("requests") or []))
        if out.get("action") != "request" or not asked or n >= investigate.MAX_ROUNDS:
            findings = out.get("findings") or {}
            break
        results = [stored.get(a) or missing(a) for a in asked if a in menu]
        messages = investigate.next_messages(messages, text, results, [a for a in asked if a not in menu],
                                             investigate.MAX_ROUNDS - 1 - n)
    advice, _, _ = _ask(gpu, investigate.remediation_messages(inc, rb, inc["target"], findings, cfg.primer()),
                        investigate.ADVICE, cfg, timeout)
    return {"runbook": f"{rb.name}@{rb.rev}", "model": model, "turns": turns, "findings": findings, "advice": advice}


def replay_triage(inc: dict, gpu: GpuScheduler, cfg: Config, timeout: float) -> dict:
    first = next((t for t in inc["turns"] if t["session"] == "triage"), None)
    if first is None:
        return {"error": "no stored triage turn"}
    job = _wait(gpu, gpu.submit(first["messages"]), timeout)
    return {"model": f"{job.get('ran_model')} on {job.get('worker')}",
            "analysis": (job.get("result") or {}).get("text") or job.get("error")}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="incident_operator replay", description=__doc__.split("\n\n")[0])
    ap.add_argument("ids", nargs="+", type=int)
    ap.add_argument("--model", help="gpu-scheduler model to ask (default: TRIAGE_MODEL), no fallback")
    ap.add_argument("--runbooks", default=str(CHART / "runbooks"))
    ap.add_argument("--operations", default=str(CHART / "operations"))
    ap.add_argument("--primer", default=str(CHART / "primer.md"))
    ap.add_argument("--timeout", type=float, default=900)
    a = ap.parse_args(argv)
    cfg = Config.from_env()
    cfg.primer_path, cfg.fallback = a.primer, []
    if a.model:
        cfg.model = a.model
    gpu = GpuScheduler(cfg)
    lib = Library(a.runbooks, a.operations)
    lib.refresh()
    for path, err in lib.errors.items():
        print(f"! {path}: {err}")
    conn = db.connect(os.environ["DATABASE_URL"])
    for i in a.ids:
        inc = db.get_incident(conn, i)
        if inc is None:
            print(f"#{i}: no such incident")
            continue
        then = {"runbook": inc["runbook"], "model": f"{inc['analysis_model']} on {inc['analysis_worker']}",
                "findings": inc["findings"], "advice": inc["advice"],
                "analysis": None if inc["runbook"] else inc["analysis"]}
        try:
            now = (replay_runbook(inc, lib, gpu, cfg, a.timeout) if lib.match(inc["labels"])
                   else replay_triage(inc, gpu, cfg, a.timeout))
        except Exception as e:
            now = {"error": f"{type(e).__name__}: {e}"}
        print(f"\n=== #{i} {inc['alertname']} {inc['labels'].get('namespace', '')}")
        print("--- then:\n" + json.dumps({k: v for k, v in then.items() if v}, indent=1, ensure_ascii=False))
        print("--- now:\n" + json.dumps(now, indent=1, ensure_ascii=False))
    return 0
