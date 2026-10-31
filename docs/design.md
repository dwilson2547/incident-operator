# incident-operator: design

Status: **draft, 2026-10-31.** Nothing is built. Name and placement are provisional (§13).

An in-cluster operator that turns firing alerts into tracked incidents, has a small local LLM triage each one
against a runbook, suggests fixes, and runs narrowly scoped remediation operations you have approved in advance. It
runs in stages: suggestions only at first, then runbooks once a failure recurs, then approved operations, then
automatic ones for fixes that have proven safe.

The rule the whole design follows: **the model picks and explains; code decides what can run.** Matching an alert
to a runbook, choosing a target, checking preconditions, rate limiting and running an operation are all
deterministic code. The model reads, reasons and chooses among operations the runbook allows. Nothing the model
writes becomes a command, a target or a permission.

The second rule: **be strict where there is power, loose everywhere else.** Operations (§4.2) are precise and
versioned, because they act on the cluster. Runbooks (§4.1) are mostly prose the model reads, and they can be edited
freely. Ordering, hints and "check X before Y" live in that prose, not in a state machine. Where an ordering really
matters for safety, it is a precondition on the operation, checked in code whenever the operation is requested.

---

## 1. Why

- Alerts arrive as email (`infra/cluster-config/docs/alerting.md`). Diagnosing one means opening Grafana,
  `kubectl`, Loki and the relevant write-up in `docs/issues/` by hand. The knowledge is already in the repo, in
  `playbooks/` and `docs/issues/`, but nothing connects an alert to it.
- Quick tests (2026-10-30/31, user's own) gave Bonsai 27B, Mistral 3 3B and Gemma 4 12B the system prompt "You are a
  triage agent…" and pasted scraper stack traces. Their suggestions were useful. That is the core step of this
  design. The rest is about feeding the model the right evidence and limiting what its answers can trigger.
- Some failures recur with a known, testable fix. The worked example (§12) is the NFS client wedge, where deleting
  one node's `csi-nfs-node` pod cleared 102 stuck mounts without a reboot (2026-10-22). Fixes like that should
  become one click, and later automatic, without giving an LLM a shell.

## 2. Scope

In scope:
- Intake of Alertmanager alerts; incident records with dedup, timeline and outcome.
- Runbooks: which alerts they match, the precursor data to collect, the operations allowed, and the prose the
  model reads.
- Operations: read-only checks and mutating fixes, with preconditions, postconditions, rate limits and per-operation
  autonomy (§7).
- A two-session triage per incident (§6), run as gpu-scheduler text-generation jobs.
- A small UI: incidents, evidence, analysis, approve/reject, "alerts without a runbook".
- Replaying stored incidents against another model or prompt (§9.3).

Not in scope:
- Free-form tool use or shell access for the model. It can only call operations its runbook lists.
- Replacing Alertmanager's email. Email keeps working on its own, so an outage of this operator, the GPU or the
  ainode never silences alerts (§11).
- Paging, on-call rotations, hosted LLMs (cost-free tooling only).
- CRDs, for now (§4.3).

## 3. Build vs. buy

**HolmesGPT** (Apache-2.0, CNCF Sandbox) was looked at on 2026-10-31 and rejected for this cluster:
- Its docs call Ollama support "experimental" and recommend hosted models first. Its published evals (the latest run
  is 2026-10-10) cover only proprietary frontier models. In its self-hosted run, open models were served through
  hosted APIs, and two scored 0% and 2%.
- It is an open-ended investigation loop. A sample log in its docs shows a single call sending 45,290 input tokens.
  That does not fit a 12 GB card running a ~7.5 GB model.
- It matches runbooks ("skills") by LLM description, not by alert. Its remediation add-on gets a broad ClusterRole
  plus arbitrary `kubectl` behind an approval step. Both are the opposite of this design.

What we take from it: runbooks are markdown with a frontmatter `description`, close to its `SKILL.md`, so the same
runbooks could be pointed at HolmesGPT later if a stronger model ever becomes available.

Building our own also follows the existing operator pattern in this workspace (scrape-job-manager: Python,
`kubernetes` client, a pure reconcile tick, Postgres for history).

## 4. Runbooks and operations

| Thing | Format | Where |
|---|---|---|
| Runbook | Markdown with YAML frontmatter | `helm/incident-operator/files/runbooks/<name>.md` |
| Operation | YAML | `helm/incident-operator/files/operations/<name>.yaml` |
| Incident, sessions, operation runs, approvals | Postgres rows | `incident_operator` DB on the plain `postgres` instance |

### 4.1 Runbook

The frontmatter is what code uses. The body is what the model reads.

```markdown
---
name: nfs-client-wedge
description: >-
  A worker's kernel NFS client to Tower is wedged: mount.nfs piles up in D state, load climbs with idle CPU,
  pods mounting the share hang in ContainerCreating.
match:                       # deterministic; highest priority match wins
  alertname: NodeLoadHighCPUIdle
priority: 100
target: {kind: Node, from: instanceToNode}   # §5.4; never from model output
precursors:                  # read-only operations run automatically when the incident opens (§5)
  - node-load-cpu-6h
  - node-failedmount-events
  - nfs-dstate
operations:                  # everything the agent may request in this incident
  - nfs-dstate
  - node-kmsg-nfs
  - tower-load
  - restart-csi-nfs-node: {autonomy: approve}   # per-runbook override of the operation's default
sourceDocs:
  - infra/cluster-config/docs/issues/2026_10_18_unraid_nfs_v41_client_wedge.md
  - infra/cluster-config/docs/postgres_backup.md
---

## What this looks like
...

## Investigation
Check whether the stuck mount.nfs predate Tower's last reboot ...

## Remediation
If the node has D-state mount.nfs older than 10 minutes and Tower looks healthy, restart the node's CSI pod ...
Never suggest probing with a new mount from a k8s node.
```

The headings `## Investigation` and `## Remediation` are conventions: the investigation session (§6.1) gets the whole
body, and the remediation session (§6.3) gets `## Remediation` only. A runbook without those headings still works,
and both sessions then get the whole body.

**Runbooks are short.** They are written for a small local model with a few thousand tokens to work with, not for
a person or a frontier model. The long write-ups in `docs/issues/` and the cluster-config docs stay where they are,
as the source material. A runbook links to them with `sourceDocs:` and **is never allowed to include them**:
`sourceDocs` is for humans and for whoever writes the runbook, and nothing in it reaches a prompt.

| Limit | Value | On load |
|---|---|---|
| Whole body | 1,200 tokens ⬜ | over: rejected |
| `## Remediation` section | 400 tokens ⬜ | over: rejected |
| `description` | 60 tokens ⬜ | over: rejected |
| Whole body | 800 tokens ⬜ | over: warning in the UI |

Tokens are counted with the deployed model's tokenizer when it is known, otherwise as characters ÷ 4. With the
precursors (§6.4), the first investigation prompt then stays near half of its 6k budget, which leaves room for
requested results.

How to write one:
- **What it looks like:** the symptoms this alert means, in two to four lines. Include what it is *not*, if a
  look-alike exists ("high load with busy CPU is a different problem").
- **Investigation:** what to look at and what each result means, as short bullets. Name operations by their name.
  No commands for a human to type: the model can't run them, and the operations already do.
- **Remediation:** the decision, as a short if/then list, ending with what must go to a human.
- **No history and no narrative.** Dates, timelines and root-cause stories stay in the write-up. A runbook keeps
  only the conclusion ("deleting the node's CSI pod clears stuck mount.nfs; it does not fix a deadlocked Tower").
- **No numbers the code already checks.** Thresholds in an operation's `pre` don't need repeating in prose.

The §12 runbook, written out in full, is the reference example and should stay within the limits.

### 4.2 Operation

One format for both read-only checks and mutating fixes:

```yaml
name: restart-csi-nfs-node
description: Delete the csi-nfs-node pod on one node; the DaemonSet recreates it.
mutating: true
autonomy: suggest                 # default; runbooks may raise it, only by commit
params:
  node: {from: target}            # only sources: target | alertLabel:<name> | result:<operation>.<jsonpath>
requires: [nfs-dstate]            # optional: these must have run in this incident (rarely needed)
pre:                              # checked in code, against fresh results, right before running
  - op: nfs-dstate
    expect: "$.mount_nfs_dstate_older_than_10m > 0"
run:
  verb: DeletePods                # built-in executor verbs (§7.2); never an arbitrary command
  namespace: kube-system
  selector: {app: csi-nfs-node}
  fieldSelector: "spec.nodeName={node}"
post:                             # after `settle`; failure escalates the incident
  settle: 120s
  checks:
    - op: nfs-dstate
      expect: "$.mount_nfs_dstate_total == 0"
    - k8s: {kind: Pod, namespace: kube-system, selector: {app: csi-nfs-node},
            fieldSelector: "spec.nodeName={node}", expect: Ready}
limits: {perTarget: 1/1h, global: 3/24h}   # over the limit = escalate instead of running
```

A read-only operation is the same shape with `mutating: false`, a `run` of a query kind (§5.1), and usually no
`pre`, `post` or `limits`:

```yaml
name: node-load-cpu-6h
description: Load averages and CPU busy fraction for the node over the last 6 hours, 10-minute steps.
mutating: false
params: {instance: {from: alertLabel:instance}}
run:
  kind: promql-range
  queries:
    load1: 'node_load1{instance="{instance}"}'
    load15: 'node_load15{instance="{instance}"}'
    cpu_busy: '1 - avg(rate(node_cpu_seconds_total{instance="{instance}",mode="idle"}[10m]))'
  range: 6h
  step: 10m
budget: {maxBytes: 4000}
```

`requires` covers the rare case where an ordering matters for more than safety ("only offer this fix once that
probe has run"). Safety conditions belong in `pre`. Neither is needed for most operations.

### 4.3 Files now, CRDs maybe later

**Loading: a ConfigMap built by the chart, deployed by Argo** (decided 2026-10-31). The chart renders
`files/runbooks/` and `files/operations/` into ConfigMaps (`.Files.Glob`). Helm can only read files inside the
chart folder, which is why they live there and not at the repo root. The operator watches those ConfigMaps through
the API and re-validates on change, so a sync takes effect without a restart.

Why not a git-sync sidecar:
- The executor's RBAC is generated from the same `operations/` files at render time (§7.2). Through the chart, an
  operation and the permissions it needs arrive in the same sync. With git-sync, a new operation would go live on
  push while its RBAC waited for Argo, and it would fail with 403 in between. Runbooks go the same way so they
  never refer to an operation that isn't deployed yet.
- No deploy key for the private repo inside the cluster, and no extra sidecar to keep healthy.
- The runbook revision recorded on an incident (§8.2) is the commit Argo deployed.

The fast loop is local: the replay CLI (§9.3) runs stored incidents against the working-tree files, so prose and
prompt changes are tested before anything is pushed. A ConfigMap holds at most 1 MiB, far more than short runbooks
need (§4.1).

Each file is validated on load (schema, operation references, size limits). An invalid file is rejected, the
previous version stays live, and the error shows in the UI. Changing a field is an edit, not a schema version bump.

Once the format stops changing, promoting it to CRDs (`Runbook`, `Operation`) is mechanical. Grouping runbooks into
a "system under the umbrella" also waits until there are enough runbooks to know what a system is. Until then, a
runbook's `match` defines what is covered.

## 5. Precursors and on-demand checks

When an incident opens, the operator runs the runbook's **precursors**, deterministically and before any model
call, and puts their results in the first prompt. This is the data the runbook author knows is always needed for
this alert.

Every other read-only operation on the runbook's list is **on demand**. It runs only when the agent asks for it
during investigation (§6.1), so expensive or node-touching checks run only when the case needs them.

Mutating operations are never precursors.

### 5.1 Read-only operation kinds

| `kind` | What | Notes |
|---|---|---|
| `promql` / `promql-range` | Queries against `prometheus-kube-prometheus-prometheus.monitoring:9090` | Range results downsampled to at most N points |
| `logql` | Loki via `loki-gateway.monitoring` | Line and byte caps; newest first |
| `k8s-events` | Events for the target, or for objects named by alert labels | Last N, deduplicated by reason |
| `k8s-summary` | A structured summary of one object (status, conditions, recent events). Not raw `kubectl describe` | |
| `k8s-logs` | Last N lines of a container | Byte-capped |
| `probe` | A short Job on the target node that reads `/proc` and prints JSON (§5.3) | |

The alert's own labels and annotations are always included and need no operation.

### 5.2 Size discipline

Every operation has a byte budget and a summariser (for example top-k series, last N lines, or a count by reason).
The precursor block as a whole has a token budget (§6.4). An oversized result is truncated, and the truncation is
noted to the model, never silently dropped.

### 5.3 Probes on nodes

The NFS write-up's first rule: **never probe a suspect NFS server with a new mount from a k8s node.** A new mount
reuses the wedged client and wedges that node too. Probes therefore:
- mount no volumes beyond `/proc` (via `hostPID: true`) and a tmpfs;
- run with a read-only root filesystem, no host network, no service-account token, and only the capabilities the
  probe declares (reading other processes' `/proc/<pid>/stack` needs `CAP_SYS_PTRACE` ⚠ unverified for this kernel);
- have a hard `activeDeadlineSeconds`, so a probe that hangs fails instead of blocking the incident.

### 5.4 Target resolution

node-exporter alerts carry `instance="192.168.0.36:9100"`, with no node name (verified 2026-10-31 on
`node_procs_blocked`). The `instanceToNode` resolver maps the IP to the Node whose `InternalIP` matches. Tower
(`instance="tower"`, job `tower-node`) resolves to no Node, so node-targeted operations are impossible for it by
construction.

## 6. Triage: two sessions

```
alert ─▶ match runbook ─▶ precursors ─▶ INVESTIGATION SESSION ─────────────▶ findings ─▶ REMEDIATION SESSION ─▶ report
                                       (loop: request read-only ops, budgeted)  (fixed JSON)  (no raw evidence)
```

Each model call is greedy and returns JSON that is validated before code acts on it. The two sessions share no
conversation. The only thing passed from one to the other is the findings object.

### 6.1 Investigation session

A loop of model calls, with a growing conversation:

- **First turn in:** the runbook's full body, the alert, the precursor results, and the menu of on-demand read-only
  operations (name and description only).
- **Each turn out:** either
  ```json
  {"request": ["node-kmsg-nfs"], "reason": "..."}
  ```
  or
  ```json
  {"done": true, "findings": {...}}
  ```
- Code runs any requested operations that are on the menu and appends their results as the next turn. Names not on
  the menu are ignored, recorded, and reported back to the model as unavailable.
- Bounds: `maxRounds` (default 3 ⬜) and a token budget for the whole conversation (§6.4). At either limit, the next
  turn tells the model it must finish now.

**Findings** (fixed shape, the hand-off):

```json
{"what_failed": "...", "where": {"kind": "Node", "name": "..."}, "evidence_refs": ["nfs-dstate", "node-load-cpu-6h"],
 "confidence": "low|medium|high", "matches_runbook": true, "summary": "..."}
```

`where` is checked against the derived target. A mismatch is recorded and shown, and any operation still uses the
derived target.

### 6.2 Why the model can choose the order

The runbook prose suggests an order, and a model may not follow it. That's acceptable because:
- read-only operations can't change anything;
- every mutating operation's `pre` runs in code at execution time, whatever the model did before;
- rounds and tokens are bounded, so a wandering small model ends with low-confidence findings rather than a loop.

Replay (§9.3) shows whether a given model wanders often enough to need tighter prompts or `requires`.

### 6.3 Remediation session

A single call:

- **In:** the findings, the runbook's `## Remediation` section, and the allowed mutating operations with their
  descriptions and effective autonomy. **No raw evidence and no log lines.**
- **Out:**
  ```json
  {"suggestion": "...", "operation": "restart-csi-nfs-node" | null, "why": "...",
   "escalate_to_human": "e.g. check Tower's nfsd for D-state threads (needs root on Tower)"}
  ```
- `operation` must be a mutating operation on the runbook's list. Anything else becomes null and is recorded.
  Parameters are not part of the output, because they come from the operation's `params` (§4.2).

### 6.4 Budgets

Per session: `maxInputTokens` (default 6k ⬜ for investigation, including precursors; 2k ⬜ for remediation) and
`maxNewTokens` (default 512, gpu-scheduler's `DEFAULT_MAX_NEW_TOKENS`). gpu-scheduler's text path rejects inputs
over 60,000 characters (`MAX_INPUT_CHARS` in `models/textgen.py`), which is the hard ceiling.

### 6.5 No runbook

A single call with the user's tested system prompt ("You are a triage agent, users will provide you with stack
traces, error logs, or grafana metrics and your job is to identify where the failure happened and advise the user"),
the alert, and generic precursors: events and logs for the objects named in the alert's labels. The output is a
suggestion only. The incident is flagged **needs runbook** (§8).

### 6.6 Untrusted text

Log lines, events and alert annotations can contain text that someone else controls. Scraper errors include
content from third-party pages. Defences, in order:
1. Only the investigation session sees raw evidence, and it can only request read-only operations from its menu.
2. Findings have a fixed shape. The remediation session never sees raw text.
3. Mutating operations come only from the runbook's list. Parameters come only from the target, alert labels and
   operation results. Preconditions run in code against fresh results.

So a model fooled by injected text can at most suggest an operation the runbook already allows, on the host that is
alerting, and only if that host passes the precondition at that moment.

## 7. Mutating operations

### 7.1 Autonomy

| Level | What happens when the remediation session proposes it |
|---|---|
| `suggest` | Shown on the incident. A human runs the fix (the UI shows the exact `kubectl` equivalent) |
| `approve` | One click in the UI runs it through the executor. Pre-checks run at click time, not proposal time |
| `auto` | The executor runs it when proposed, if pre-checks pass and limits allow |

How an operation moves up a level (a convention, not enforced): `suggest` → `approve` once the fix is confirmed
correct, then `approve` → `auto` after several approved runs with passing post-checks. The UI shows each
operation's record (runs, post-check pass rate, last failure) to support that decision. Moving up is a commit to
the operation or the runbook.

### 7.2 Executor

A separate Deployment from the controller, with its own ServiceAccount. It reads approved operation runs from
Postgres and performs them with a **small fixed set of verbs**:

| Verb | Does | RBAC it needs |
|---|---|---|
| `DeletePods` | Delete pods matching namespace + selector + fieldSelector | `delete` on `pods` in the namespaces listed in its config |
| `RolloutRestart` | Patch a Deployment/StatefulSet's restart annotation | `patch` on named workloads |
| `CreateJobFrom` | `create job --from=cronjob/<name>` (rerun a failed backup) | `create` on `jobs` + `get` on `cronjobs`, named namespaces |
| `RunJob` | Run a vetted Job template from the operation (e.g. a node-local kill script) | `create` on `jobs` in the operator namespace |

The verb set grows by code change. The chart generates the executor's Role/ClusterRole from the mutating operations
in `operations/`, so its RBAC is exactly the union of what the committed operations need. A node-local script such
as "kill stale mount helpers" is a `RunJob` with a pinned image and a fixed command. Only its parameters (from
§4.2's sources) vary.

### 7.3 Permissions summary

| Component | Can |
|---|---|
| Controller | Load runbooks and operations, read the k8s objects read-only operations need, create probe Jobs, read/write its DB, submit gpu-scheduler jobs. No delete/patch on workloads |
| Executor | Only what §7.2 generates. No access to gpu-scheduler |
| Probe Jobs | Read `/proc` on one node. No cluster API token |
| Model | Nothing. It is a remote text-in/JSON-out service |

## 8. Incidents

### 8.1 Lifecycle

`open` → `triaging` → `analysed` → (`awaiting_approval` → `remediating` → `verifying`) → `resolved` | `escalated`

- **Dedup:** one open incident per Alertmanager fingerprint. Re-sends update `last_seen`. A resolved alert marks
  the incident `alert_resolved`. Closing it is separate (a human, or a passing post-check).
- **Grouping:** alerts that resolve to the same target and runbook within a window attach to one incident.
- **Needs runbook:** incidents with no matching runbook appear in their own list, with the default triage's guess
  and a count of how often that alertname has fired. That list is the to-do list for writing runbooks.

### 8.2 Schema (Postgres, sketch)

```
incident(id, fingerprint, alertname, labels jsonb, target_kind, target_name, runbook, runbook_rev, status,
         first_seen, last_seen, resolved_at, summary)
op_result(id, incident_id, operation, op_rev, trigger,          -- trigger: precursor | requested | pre | post
          params jsonb, result jsonb, truncated bool, collected_at, error)
model_turn(id, incident_id, session, turn, model, prompt_version, messages jsonb, output jsonb, raw_text,
           gpu_job_id, input_tokens, output_tokens, started_at, finished_at, error)
op_run(id, incident_id, operation, op_rev, params jsonb, autonomy, proposed_by_turn, approved_by, approved_at,
       status, started_at, finished_at, error)                  -- its pre/post results are op_result rows
```

`runbook_rev` and `op_rev` are the git revisions that were live, so an incident can be read against the exact
runbook it ran with. `model_turn.messages` stores exactly what the model saw, which is what makes replay possible.

## 9. The model

### 9.1 Backend

- **Default: gpu-scheduler** text generation (`POST /v1/jobs` with `{"messages", "json": true, "max_new_tokens"}`),
  with a `deadline_seconds` and a `callback_url`. Greedy decoding, so a replay is deterministic. Triage can wait
  minutes, which suits the queue. The deadline keeps it from starving behind a caption backfill. An investigation
  of N rounds is N sequential jobs.
- **Experiments: any OpenAI-compatible endpoint** (e.g. LM Studio on the desktop) behind the same interface, so a
  model can be tried before gpu-scheduler has an adapter for it.

### 9.2 Model choice (open)

Candidates from the user's tests: Bonsai 27B (Ternary Bonsai 2 recommended: `Q2_0` on stock llama.cpp ≥ b11556),
Gemma 4 12B, Mistral 3 3B. gpu-scheduler already runs Qwen3-VL-4B for text-only jobs. That avoids a model swap,
which takes ~23 s for qwen3vl. Bonsai and other GGUF models would need a llama.cpp-backed adapter in gpu-scheduler,
which doesn't exist yet. The choice is made by replay (§9.3), not up front.

### 9.3 Replay and evaluation

`incident-operator replay --since 2026-11-01 --model X --session investigation` re-runs stored conversations against
another model or prompt. Operation results come from the stored `op_result` rows, never re-run against the live
cluster. It then diffs the structured outputs: which operations were requested, the findings, and the proposed
fix. After a few weeks of real incidents, that gives the same kind of bake-off as the photo-dump search and gyopart
suggester tests: real cases, scored session by session. A replay can only request operations whose results were
stored, which limits how far a different model can explore. Prompts carry a version (`prompt_version`), like
qwen3vl's caption prompts.

## 10. UI and notifications

- **Incidents list:** open, needs-runbook, recently resolved.
- **Incident page:** alert, precursors and requested results (collapsible), the investigation turns, findings, the
  proposed fix, a pre-check preview with an Approve/Reject button, then operation and post-check results.
- **Operations page:** each operation's record (§7.1). **Runbooks page:** loaded runbooks, load errors, and how
  often each has matched.
- **Auth:** authentik OIDC gated by a group, following gpu-scheduler's dashboard pattern (`oidc.py`). LAN-only first.
- **Notifications:** one email per incident when analysis finishes or approval is needed (SMTP as in
  `alerting.md`), in addition to Alertmanager's own email, not instead of it.

## 11. Failure modes of the operator itself

| Failure | Effect | Mitigation |
|---|---|---|
| Operator down | No triage | Alertmanager email is independent. Its webhook receiver retries, and the controller reconciles from Alertmanager's API on start |
| GPU busy / ainode down | Sessions wait for their deadline | Incident shows "analysis unavailable" with the precursor results. Alerts about the ainode itself are expected to hit this |
| Model returns invalid JSON | Turn fails | One retry with the validation error appended, then `escalated` |
| Probe hangs (e.g. NFS) | Operation fails at its deadline | `activeDeadlineSeconds`. Probes never mount the suspect share (§5.3) |
| Bad runbook/operation file pushed | That file doesn't load | Previous version stays live. Load error shown in the UI (§4.3) |
| Fix makes it worse | Post-check fails | `escalate`, no retry. `limits` stop loops |
| Flapping alert | Many incidents | Fingerprint dedup and grouping (§8.1) |

## 12. Worked example: NFS client wedge on a worker

Source: `infra/cluster-config/docs/issues/2026_10_18_unraid_nfs_v41_client_wedge.md` (Recurrence 2026-10-22) and
`docs/postgres_backup.md` → "When the backup share hangs".

**What happened (verified):** on 2026-10-22, k8s-worker1's NFS client to Tower stayed wedged from a diagnostic mount
made on 10-18. kubelet added a `mount.nfs` roughly every 2 minutes, reaching 102 in D state. Two nightly backup jobs
hit their 6h deadline. Deleting worker1's `csi-nfs-node` pod killed all 102 (they were its children, waiting
killably). The share then mounted again with no reboot.

**Monitor:** there is no alert for this today. `node_procs_blocked` does **not** see it: on 2026-10-22 it stayed at
0 on worker1 while `node_load1` rose steadily from ~4 to 105.5 on 8 cores, and CPU stayed 12–27% busy (Prometheus,
queried 2026-10-31). The signature is high load with an idle CPU. Proposed rule (⬜ planned; thresholds to be set
from 14-day baselines):

```yaml
- alert: NodeLoadHighCPUIdle
  expr: |
    (node_load15 / on(instance) count by (instance) (node_cpu_seconds_total{mode="idle"})) > 4
    and on(instance)
    (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[10m]))) < 0.5
  for: 20m
  labels: {severity: warning}
```

**Operations:**

| Operation | Kind | Role in the runbook |
|---|---|---|
| `node-load-cpu-6h` | `promql-range` | precursor |
| `node-failedmount-events` | `k8s-events`, reason `FailedMount` on pods scheduled to the node | precursor |
| `nfs-dstate` | `probe`: counts `mount.nfs` in D state with start times and parents, and whether an NFSv4 state manager (`*-manager`) is in D. Output `{mount_nfs_dstate_total, mount_nfs_dstate_older_than_10m, oldest_start, parents_are_csi_nfs_node, state_manager_dstate}` | precursor, and the `pre`/`post` check of the fix |
| `node-kmsg-nfs` | `probe`: kernel log lines matching `nfs: server` (reads `/dev/kmsg` ⚠ unverified) | on demand |
| `tower-load` | `promql-range` on job `tower-node` | on demand |
| `restart-csi-nfs-node` | mutating, `DeletePods` (§4.2) | the fix, at **`approve`** |

`nfs-dstate` is a precursor even though it touches the node, because without it neither the model nor the fix's
pre-check knows anything useful. It doesn't mount anything, so it is safe to run on a wedged node.

**The runbook** (`files/runbooks/nfs-client-wedge.md`), in full. It is the reference for §4.1's limits:

```markdown
---
name: nfs-client-wedge
description: >-
  A worker's kernel NFS client to Tower is wedged: mount.nfs piles up in D state, load climbs with idle CPU,
  pods mounting the share hang in ContainerCreating.
match: {alertname: NodeLoadHighCPUIdle}
priority: 100
target: {kind: Node, from: instanceToNode}
precursors: [node-load-cpu-6h, node-failedmount-events, nfs-dstate]
operations: [nfs-dstate, node-kmsg-nfs, tower-load, {restart-csi-nfs-node: {autonomy: approve}}]
sourceDocs:
  - infra/cluster-config/docs/issues/2026_10_18_unraid_nfs_v41_client_wedge.md
  - infra/cluster-config/docs/postgres_backup.md
---

## What this looks like
Load rises steadily on one node while its CPU stays mostly idle. Pods on that node that mount the Tower NFS share
sit in ContainerCreating with FailedMount timeouts. Not this runbook: high load with busy CPU.

## Investigation
- nfs-dstate: mount_nfs_dstate_total > 0 confirms a wedged client on this node. state_manager_dstate true means
  the NFSv4 state manager itself is stuck.
- node-load-cpu-6h: a steady climb over hours fits a wedge; a sudden spike fits something else.
- tower-load: if Tower's load is also high, Tower's nfsd may be deadlocked, and the fix below will not hold.
- node-kmsg-nfs: repeated "server not responding" lines point at Tower rather than this node.

## Remediation
- Stuck mount.nfs on this node and Tower looks normal: restart-csi-nfs-node. The stuck mounts are children of the
  CSI pod and die with it.
- Tower load also high, or other nodes affected: do not restart; escalate "check Tower's nfsd for D-state
  threads (root on Tower)".
- Stuck tasks remain after the restart: escalate "reboot the node".
- Never suggest testing with a new NFS mount from a k8s node: it wedges that node too.
```

Measured with the characters ÷ 4 estimate: the body is 192 words, ~290 tokens; `## Remediation` is ~110 tokens; the
`description` is ~40 tokens. All well inside the limits, so the limits leave room for runbooks about three times
this size.

**Suggest only, never operations:**
- "Check Tower's nfsd for D-state threads." It needs root on Tower, which the cluster does not have.
- "Reboot the worker if D-state NFS tasks remain."

**Why the fix starts at `approve`, not `auto`:** if Tower's own nfsd is deadlocked (the 2026-10-18 failure),
deleting the CSI pod clears the node, but the next mount wedges again. The post-check catches that, because D-state
`mount.nfs` reappears. The fix is still a needless cycle, and the incident should go straight to "check Tower".
After several approved runs where the post-check passed, raising it to `auto` is the user's call.

## 13. Placement and deployment

- **Project:** `infra/incident-operator`, a private repo (dwilson2547/incident-operator) and workspace submodule.
  Domain `infra`, by purpose: operating the cluster. Secondary domain `ai`.
- **Chart:** `helm/incident-operator/` in this repo (self-deploying, CONVENTIONS §6). Controller, executor and UI as
  three Deployments from one image. The executor's RBAC is generated from `operations/` (§7.2).
- **Runbooks and operations:** `helm/incident-operator/files/{runbooks,operations}/`, rendered into ConfigMaps
  (§4.3). The repo is private, so node names and cluster specifics can stay here.
- **Alertmanager:** add a `webhook_configs` receiver to the `cluster-alerts` root config
  (`monitoring/manifests/cluster-alerts.yaml`) with `continue: true` next to email, so email is never affected.
- **DB:** `incident_operator` on the plain `postgres` instance, with an init SQL in `infra/cluster-config/postgres/`
  like scrape-job-manager's.
- **gpu-scheduler:** a client token `incident-operator` in `gpu-scheduler-tokens`.

## 14. Phasing

1. **Suggest-only triage.** Webhook intake, incidents, the no-runbook triage (§6.5), needs-runbook list, minimal UI,
   email. Proves intake and model latency on real alerts.
2. **Runbooks, precursors and read-only operations.** File loading and validation, precursors, the investigation
   loop, findings, the remediation session as suggestion only, probes, replay. First runbooks: NFS client wedge
   (§12), the scraper failure classes, `KubeJobFailed` for backups.
3. **Approved fixes.** Mutating operations, the executor with `DeletePods` and `CreateJobFrom`, generated RBAC, pre-
   and post-checks, limits. First: `restart-csi-nfs-node` at `approve`.
4. **Auto** for operations with a record. More verbs as runbooks need them. CRDs if the formats have settled (§4.3).

## 15. Open questions

- **Model:** Qwen3-VL-4B (already loaded, no swap) or a dedicated text model (Bonsai, Gemma 4 12B), via replay
  (§9.3). A llama.cpp adapter in gpu-scheduler would be its own piece of work.
- **Name:** `incident-operator` is a placeholder.
- **Scraper failures:** scrape-job-manager already classifies run failures and emails on state changes. Does this
  operator take those over via alerts (`ScrapeJob*` are currently unmailed), or read scrape-job-manager's history
  DB through a read-only operation?
- **Grafana metrics as input** (from the original prompt): pasting a panel isn't an alert. Is there also a manual
  "triage this" entry point in the UI that takes pasted text, outside the incident flow?
- **Defaults marked ⬜** (`maxRounds`, token budgets, alert thresholds) get set from the first weeks of data.
