---
tier: project
---

# incident-operator

Kubernetes operator that turns Alertmanager alerts into tracked incidents. A small local LLM (via gpu-scheduler)
triages each one against a runbook, and approved, narrowly scoped remediation actions run with pre- and
post-checks. The model picks and explains; code decides what can run.

Status: **design only** (2026-10-31). See [`docs/design.md`](docs/design.md).
