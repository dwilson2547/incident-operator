# incident-operator: design

Status: **draft, 2026-10-31.** Nothing is built. Name and placement are provisional (§13).

An in-cluster operator that turns firing alerts into tracked incidents, has a small local LLM triage each one
against a runbook, suggests fixes, and runs narrowly scoped remediation actions you have approved in advance. It
runs in stages: suggestions only at first, then runbooks once a failure recurs, then approved actions, then
automatic ones for fixes that have proven safe.

The rule the whole design follows: **the model picks and explains; code decides what can run.** Matching an alert
to a runbook, collecting evidence, choosing a target, checking preconditions, rate limiting and running an action
are all deterministic code. The model reads, reasons and chooses among options the runbook allows. Nothing the
model writes becomes a command, a target or a permission.

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
- Runbooks: which alerts they match, what evidence to collect, the stage prompts, which actions are allowed.
- A staged LLM pipeline per incident (§6), run as gpu-scheduler text-generation jobs.
- Named remediation actions with preconditions, postconditions, rate limits and per-action autonomy (§7).
- A small UI: incidents, evidence, analysis, approve/reject, "alerts without a runbook".
- Replaying stored incidents against another model or prompt (§9.3).

Not in scope:
- Free-form tool use or shell access for the model. The model can ask for more evidence only from queries its
  runbook names (§6.1).
- Replacing Alertmanager's email. Email keeps working on its own, so an outage of this operator, the GPU or the
  ainode never silences alerts (§11).
- Paging, on-call rotations, hosted LLMs (cost-free tooling only).

## 3. Build vs. buy

**HolmesGPT** (Apache-2.0, CNCF Sandbox) was looked at on 2026-10-31 and rejected for this cluster:
- Its docs call Ollama support "experimental" and recommend hosted models first. Its published evals (the latest run
  is 2026-10-10) cover only proprietary frontier models. In its self-hosted run, open models were served through
  hosted APIs, and two scored 0% and 2%.
- It is an open-ended investigation loop. A sample log in its docs shows a single call sending 45,290 input tokens.
  That does not fit a 12 GB card running a ~7.5 GB model.
- It matches runbooks ("skills") by LLM description, not by alert. Its remediation add-on gets a broad ClusterRole
  plus arbitrary `kubectl` behind an approval step. Both are the opposite of this design.

What we take from it: runbook bodies are markdown with a frontmatter `description`, close to its `SKILL.md`, so the
same runbooks could be pointed at HolmesGPT later if a stronger model ever becomes available.

Building our own also follows the existing operator pattern in this workspace (scrape-job-manager: Python,
`kubernetes` client, a pure reconcile tick, Postgres for history).

## 4. Concepts

| Thing | Where it lives | Why there |
|---|---|---|
| `Runbook` | CRD, YAML in git, synced by Argo | Configuration, reviewed like code |
| `Action` | CRD, YAML in git | Configuration that grants power. Every change goes through a commit |
| `Probe` | CRD, YAML in git | Read-only evidence collector that needs host access (§5.3) |
| Incident, stage runs, action runs, approvals | Postgres (`incident_operator` DB on the plain `postgres` instance) | History that grows without bound and needs querying, which etcd is bad at |

Autonomy levels (`suggest`, `approve`, `auto`) are a field on the `Action` and can be overridden per runbook. A
level change is a git commit, never a button in the UI.

### 4.1 `Runbook`

```yaml
apiVersion: incidents.danwils.com/v1alpha1
kind: Runbook
metadata:
  name: nfs-client-wedge
spec:
  description: >-            # human/LLM-readable; mirrors a SKILL.md description
    A worker's kernel NFS client to Tower is wedged: mount.nfs piles up in D state, load climbs with idle CPU,
    pods mounting the share hang in ContainerCreating.
  match:                     # deterministic; first matching runbook by priority wins
    alertname: NodeLoadHighCPUIdle
    labels: {}               # optional extra label matchers (=, !=, =~)
  priority: 100
  target:                    # how the incident's target is derived. Never from model output
    kind: Node
    from: instanceToNode     # resolver: alert label `instance` (IP:port) -> Node by InternalIP (§5.4)
  evidence: [...]            # §5
  stages: [...]              # §6
  actions:                   # the only actions any stage may propose for this runbook
    - name: restart-csi-nfs-node
      autonomy: approve      # overrides the Action's default for this runbook
  body: |                    # the human runbook text, given to the model as reference
    ...
```

Runbook YAML lives in this repo (`runbooks/`), not in `infra/cluster-config/playbooks/`. The existing playbooks and
`docs/issues/` write-ups are the source material. A runbook links to its source with `sourceDocs:`, and its `body` is
a distillation, so a long incident write-up never goes into a prompt whole.

### 4.2 `Action`

```yaml
apiVersion: incidents.danwils.com/v1alpha1
kind: Action
metadata:
  name: restart-csi-nfs-node
spec:
  description: Delete the csi-nfs-node pod on one node; the DaemonSet recreates it.
  autonomy: suggest                 # default; runbooks may raise it, only by commit
  params:
    node: {from: target}            # only sources: target | alertLabel:<name> | evidence:<id>.<jsonpath>
  pre:                              # all must pass right before running; each is a Probe or query
    - probe: nfs-dstate
      expect: "$.mount_nfs_dstate_older_than_10m > 0"
  run:
    kind: DeletePods                # built-in executor verbs (§7.2), no arbitrary commands
    namespace: kube-system
    selector: {app: csi-nfs-node}
    fieldSelector: "spec.nodeName={node}"
  post:                             # checked after `settle`; failure escalates the incident
    settle: 120s
    checks:
      - probe: nfs-dstate
        expect: "$.mount_nfs_dstate_total == 0"
      - k8s: {kind: Pod, namespace: kube-system, selector: {app: csi-nfs-node},
              fieldSelector: "spec.nodeName={node}", expect: Ready}
  limits:
    perTarget: 1/1h                 # second trigger within the window escalates instead of running
    global: 3/24h
  onPostFailure: escalate           # never retries itself
```

### 4.3 `Probe`

A `Probe` is a read-only collector that runs as a short Job on a specific node when evidence needs the host, for
example "which processes are in D state". It is defined, reviewed and permissioned like an `Action`, but it cannot
change anything (§7.3). Its output is JSON on stdout.

## 5. Evidence

### 5.1 Sources

A runbook lists evidence items. The operator collects them **before** any model call, deterministically, and stores
each result on the incident.

| `kind` | What | Notes |
|---|---|---|
| `promql` | Instant or range query against `prometheus-kube-prometheus-prometheus.monitoring:9090` | Range results are downsampled to at most N points (§6.3) |
| `logql` | Loki via `loki-gateway.monitoring` | Line count and byte caps; newest first |
| `k8s.events` | Events for the target (or objects named by alert labels) | Last N, deduplicated by reason |
| `k8s.describe` | A structured summary of one object (status, conditions, recent events). Not raw `kubectl describe` | |
| `k8s.logs` | Last N lines of a container | Byte-capped |
| `probe` | A `Probe` run on the target node | §4.3 |
| `alert` | The alert's labels and annotations | Always included |

Parameters can only reference the target, alert labels and earlier evidence. The model never supplies them.

### 5.2 Size discipline

Every item has a byte cap and a summariser (for example top-k series, last N lines, or a count by reason). The total
evidence block for a stage has a token budget (§6.3). An item that is over its cap is truncated, and the truncation
is noted to the model, never silently dropped.

### 5.3 Probes on nodes

The NFS write-up's first rule: **never probe a suspect NFS server with a new mount from a k8s node.** A new mount
reuses the wedged client and wedges that node too. Probes therefore:
- mount no volumes beyond `/proc` (via `hostPID: true`) and a tmpfs;
- run with a read-only root filesystem, no host network, and drop every capability except the ones the probe
  declares (reading other processes' `/proc/<pid>/stack` needs `CAP_SYS_PTRACE` ⚠ unverified for this kernel);
- have a hard `activeDeadlineSeconds`. A probe that hangs fails the stage instead of blocking it.

### 5.4 Target resolution

node-exporter alerts carry `instance="192.168.0.36:9100"`, with no node name (verified 2026-10-31 on
`node_procs_blocked`). The `instanceToNode` resolver maps the IP to the Node whose `InternalIP` matches. Tower
(`instance="tower"`, job `tower-node`) resolves to no Node, so node-targeted actions are impossible for it by
construction.

## 6. The stage pipeline

Each stage is **one fresh model call**: its own system prompt, only the inputs it declares, and a JSON output with a
fixed shape that is validated before the next stage sees it. Code runs between stages. A runbook declares its stages.
An incident with no runbook gets the default single stage (§6.4).

```
alert ─▶ match runbook ─▶ collect evidence ─▶ [investigate ⇄ more evidence]* ─▶ findings ─▶ remediate ─▶ report
                                               (bounded rounds)                   (structured)  (no raw logs)
```

### 6.1 `investigate`

- **In:** runbook `description` + `body`, the alert, the evidence block, and the menu of extra evidence the runbook
  offers (`evidence.optional[]`, by id and description).
- **Out:**
  ```json
  {"need_more": ["dmesg-nfs"], "notes": "...", "done": false}
  ```
  or `{"done": true, "notes": "..."}`.
- Code collects any requested optional items and calls `investigate` again, up to `maxRounds` (default 2 ⬜). Ids
  that aren't on the menu are ignored and recorded.

### 6.2 `findings`

- **In:** the investigate notes, plus the evidence.
- **Out:**
  ```json
  {"what_failed": "...", "where": {"kind": "Node", "name": "..."}, "evidence_refs": ["nfs-dstate", "load"],
   "confidence": "low|medium|high", "matches_runbook": true, "summary": "..."}
  ```
- `where` is checked against the derived target. A mismatch is recorded and shown, and the action stage still uses
  the derived target.
- This is the hand-off point. Everything after it sees only these fields, not the raw evidence.

### 6.3 `remediate`

- **In:** the findings, the runbook's remediation section, and the allowed actions with their descriptions and
  effective autonomy. **No raw evidence, no log lines.**
- **Out:**
  ```json
  {"suggestion": "...", "action": "restart-csi-nfs-node" | null, "why": "...",
   "escalate_to_human": "e.g. check Tower's nfsd for D-state threads (needs root on Tower)"}
  ```
- `action` must be on the runbook's list. Anything else becomes null and is recorded. Parameters are not part of
  the output, because they come from the `Action`'s `params` (§4.2).

Budgets: each stage has `maxInputTokens` (default 6k ⬜) and `maxNewTokens` (default 512, gpu-scheduler's
`DEFAULT_MAX_NEW_TOKENS`). gpu-scheduler's text path rejects inputs over 60,000 characters (`MAX_INPUT_CHARS` in
`models/textgen.py`), which is the hard ceiling.

### 6.4 Default stage (no runbook)

A single call with the user's tested system prompt ("You are a triage agent, users will provide you with stack
traces, error logs, or grafana metrics and your job is to identify where the failure happened and advise the user"),
the alert, and generic evidence: events and logs for the objects named in the alert's labels. The output is a
suggestion only. The incident is flagged **needs runbook** (§8).

### 6.5 Untrusted text

Log lines, events and alert annotations can contain text that someone else controls. Scraper errors include
content from third-party pages. Defences, in order:
1. Only `investigate` sees raw evidence, and it can only request menu items (§6.1).
2. `findings` has a fixed shape. `remediate` never sees raw text.
3. Actions come only from the runbook's list. Parameters come only from the target and alert labels. Preconditions
   run in code against fresh probes.

So a model fooled by injected text can at most suggest an action the runbook already allows, on the host that is
alerting, and only if that host passes the precondition at that moment.

## 7. Actions

### 7.1 Autonomy

| Level | What happens when `remediate` proposes it |
|---|---|
| `suggest` | Shown on the incident. A human runs the fix (the UI shows the exact `kubectl` equivalent) |
| `approve` | One click in the UI runs it through the executor. Pre-checks run at click time, not proposal time |
| `auto` | The executor runs it when proposed, if pre-checks pass and limits allow |

How an action moves up a level (a convention, not enforced): `suggest` → `approve` once the fix is confirmed
correct, then `approve` → `auto` after several approved runs with passing post-checks. The UI shows each action's
record (runs, post-check pass rate, last failure) to support that decision. Moving up is a commit to the
`Action`/`Runbook`.

### 7.2 Executor

A separate Deployment from the controller, with its own ServiceAccount. It reads approved action runs from Postgres
and performs them with a **small fixed set of verbs**:

| Verb | Does | RBAC it needs |
|---|---|---|
| `DeletePods` | Delete pods matching namespace + selector + fieldSelector | `delete` on `pods` in the namespaces listed in its config |
| `RolloutRestart` | Patch a Deployment/StatefulSet's restart annotation | `patch` on named workloads |
| `CreateJobFrom` | `create job --from=cronjob/<name>` (rerun a failed backup) | `create` on `jobs` + `get` on `cronjobs`, named namespaces |
| `RunJob` | Run a vetted Job template from the `Action` (e.g. a node-local kill script) | `create` on `jobs` in the operator namespace |

The verb set grows by code change. The executor's Role/ClusterRole is generated from the `Action`s in git, so its
RBAC is exactly the union of what the committed actions need. A node-local script such as "kill stale mount
helpers" is a `RunJob` with a pinned image and a fixed command. Only its parameters (from §4.2's sources) vary.

### 7.3 Permissions summary

| Component | Can |
|---|---|
| Controller | Read CRDs, read the k8s objects evidence needs, create Probe Jobs, read/write its DB, submit gpu-scheduler jobs. No delete/patch on workloads |
| Executor | Only what §7.2 generates. No access to gpu-scheduler |
| Probe Jobs | Read `/proc` on one node. No cluster API token (`automountServiceAccountToken: false`) |
| Model | Nothing. It is a remote text-in/JSON-out service |

## 8. Incidents

### 8.1 Lifecycle

`open` → `triaging` → `analysed` → (`awaiting_approval` → `remediating` → `verifying`) → `resolved` | `escalated`

- **Dedup:** one open incident per Alertmanager fingerprint. Re-sends update `last_seen`. A resolved alert marks
  the incident `alert_resolved`. Closing it is separate (a human, or a passing post-check).
- **Grouping:** alerts that resolve to the same target and runbook within a window attach to one incident.
- **Needs runbook:** incidents with no matching runbook appear in their own list, with the default stage's guess
  and a count of how often that alertname has fired. That list is the to-do list for writing runbooks.

### 8.2 Schema (Postgres, sketch)

```
incident(id, fingerprint, alertname, labels jsonb, target_kind, target_name, runbook, status,
         first_seen, last_seen, resolved_at, summary)
stage_run(id, incident_id, stage, round, model, prompt_version, input jsonb, output jsonb, raw_text,
          gpu_job_id, input_tokens, output_tokens, started_at, finished_at, error)
evidence(id, incident_id, item_id, kind, query, result jsonb, truncated bool, collected_at)
action_run(id, incident_id, action, params jsonb, autonomy, proposed_by_stage, approved_by, approved_at,
           pre_result jsonb, run_result jsonb, post_result jsonb, status, started_at, finished_at)
```

`stage_run.input` stores exactly what the model saw, which is what makes replay possible (§9.3).

## 9. The model

### 9.1 Backend

- **Default: gpu-scheduler** text generation (`POST /v1/jobs` with `{"messages", "json": true, "max_new_tokens"}`),
  with a `deadline_seconds` and a `callback_url`. Greedy decoding, so a replay is deterministic. Triage can wait
  minutes, which suits the queue. The deadline keeps it from starving behind a caption backfill.
- **Experiments: any OpenAI-compatible endpoint** (e.g. LM Studio on the desktop) behind the same interface, so a
  model can be tried before gpu-scheduler has an adapter for it.

### 9.2 Model choice (open)

Candidates from the user's tests: Bonsai 27B (Ternary Bonsai 2 recommended: `Q2_0` on stock llama.cpp ≥ b11556),
Gemma 4 12B, Mistral 3 3B. gpu-scheduler already runs Qwen3-VL-4B for text-only jobs. That avoids a model swap,
which takes ~23 s for qwen3vl. Bonsai and other GGUF models would need a llama.cpp-backed adapter in gpu-scheduler,
which doesn't exist yet. The choice is made by replay (§9.3), not up front.

### 9.3 Replay and evaluation

`incident-operator replay --since 2026-11-01 --model X --stage findings` re-runs stored stage inputs against another
model or prompt and diffs the structured outputs. After a few weeks of real incidents, that gives the same kind of
bake-off as the photo-dump search and gyopart suggester tests: real cases, scored stage by stage. Prompts carry a
version (`prompt_version`), like qwen3vl's caption prompts.

## 10. UI and notifications

- **Incidents list:** open, needs-runbook, recently resolved.
- **Incident page:** alert, evidence (collapsible), each stage's output, the proposed action, its pre-check preview
  and an Approve/Reject button, then action and post-check results.
- **Actions page:** each action's record (§7.1).
- **Auth:** authentik OIDC gated by a group, following gpu-scheduler's dashboard pattern (`oidc.py`). LAN-only first.
- **Notifications:** one email per incident when analysis finishes or approval is needed (SMTP as in
  `alerting.md`), in addition to Alertmanager's own email, not instead of it.

## 11. Failure modes of the operator itself

| Failure | Effect | Mitigation |
|---|---|---|
| Operator down | No triage | Alertmanager email is independent. Its webhook receiver retries, and the controller reconciles from Alertmanager's API on start |
| GPU busy / ainode down | Stages wait for their deadline | Incident shows "analysis unavailable" with the collected evidence. Alerts about the ainode itself are expected to hit this |
| Model returns invalid JSON | Stage fails | One retry with the validation error appended, then `escalated` |
| Probe hangs (e.g. NFS) | Stage blocks | `activeDeadlineSeconds`. Probes never mount the suspect share (§5.3) |
| Action makes it worse | Post-check fails | `escalate`, no retry. `limits` stop loops |
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

**Probe `nfs-dstate`** (on the target node, `/proc` only, no mounts): counts tasks in D state whose `comm` is
`mount.nfs`, with their start times and parent PIDs. It also reports whether any NFSv4 state manager thread
(`*-manager`) is in D, which is the runbook's step-1 signal. Output:
`{mount_nfs_dstate_total, mount_nfs_dstate_older_than_10m, oldest_start, parents_are_csi_nfs_node, state_manager_dstate}`.

**Evidence:** the alert; `node_load1`, `node_load15` and CPU busy for the node over 6h; events on the node and on pods
scheduled there with reason `FailedMount`; the probe; Tower's `node_load1` (job `tower-node`) as context.
Optional: kernel log lines matching `nfs: server` from the node, which needs a probe reading `/dev/kmsg` ⚠ unverified.

**Stages:** `investigate` (one round), `findings`, `remediate`.

**Actions:**
- `restart-csi-nfs-node` (§4.2): **`approve`** to start. Pre-check: the probe shows D-state `mount.nfs` older than 10
  minutes. Post-check: no D-state `mount.nfs` after 120s, and the new CSI pod Ready.
- **Suggest only, never actions:**
  - "Check Tower's nfsd for D-state threads." It needs root on Tower, which the cluster does not have.
  - "Reboot the worker if D-state NFS tasks remain."

**Why it starts at `approve`, not `auto`:** if Tower's own nfsd is deadlocked (the 2026-10-18 failure), deleting the
CSI pod clears the node, but the next mount wedges again. The post-check catches that, because D-state `mount.nfs`
reappears. The action is still a needless cycle, and the incident should go straight to "check Tower". After
several approved runs where the post-check passed, raising this action to `auto` is the user's call.

## 13. Placement and deployment

- **Project:** `infra/incident-operator` (domain `infra`, by purpose: operating the cluster; secondary domain `ai`).
  A submodule with its own repo once one exists.
- **Chart:** `helm/incident-operator/` in this repo (self-deploying, CONVENTIONS §6). CRDs in `crds/`. Controller,
  executor and UI as three Deployments from one image.
- **Runbooks, Actions and Probes:** YAML in this repo under `runbooks/`, `actions/` and `probes/`, synced by the same
  Argo app. They contain node names and cluster specifics, so if this repo goes public, they move to a private repo
  ⬜ (decide with the repo's visibility).
- **Alertmanager:** add a `webhook_configs` receiver to the `cluster-alerts` root config
  (`monitoring/manifests/cluster-alerts.yaml`) with `continue: true` next to email, so email is never affected.
- **DB:** `incident_operator` on the plain `postgres` instance, with an init SQL in `infra/cluster-config/postgres/`
  like scrape-job-manager's.
- **gpu-scheduler:** a client token `incident-operator` in `gpu-scheduler-tokens`.

## 14. Phasing

1. **Suggest-only triage.** Webhook intake, incidents, the default stage, needs-runbook list, minimal UI, email.
   No CRDs yet: one hard-coded generic stage. Proves intake and model latency on real alerts.
2. **Runbooks and evidence.** `Runbook` and `Probe` CRDs, evidence collection, the three-stage pipeline, replay.
   First runbooks: NFS client wedge (§12), the scraper failure classes, `KubeJobFailed` for backups.
3. **Approved actions.** `Action` CRD, executor with `DeletePods` and `CreateJobFrom`, generated RBAC, pre- and
   post-checks, limits. First action: `restart-csi-nfs-node` at `approve`.
4. **Auto** for actions with a record. More verbs as runbooks need them.

## 15. Open questions

- **Model:** Qwen3-VL-4B (already loaded, no swap) or a dedicated text model (Bonsai, Gemma 4 12B), via replay
  (§9.3). A llama.cpp adapter in gpu-scheduler would be its own piece of work.
- **Repo visibility** and where runbooks with cluster specifics live (§13).
- **Name:** `incident-operator` is a placeholder.
- **Scraper failures:** scrape-job-manager already classifies run failures and emails on state changes. Does this
  operator take those over via alerts (`ScrapeJob*` are currently unmailed), or read scrape-job-manager's history
  DB directly as an evidence source?
- **Grafana metrics as input** (from the original prompt): pasting a panel isn't an alert. Is there also a manual
  "triage this" entry point in the UI that takes pasted text, outside the incident flow?
- **Defaults marked ⬜** (`maxRounds`, token budgets, alert thresholds) get set from the first weeks of data.
