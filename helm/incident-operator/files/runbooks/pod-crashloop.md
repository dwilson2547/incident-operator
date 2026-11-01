---
name: pod-crashloop
description: >-
  A container keeps crashing and restarting (KubePodCrashLooping). Find which container, how it exits and why.
match: {alertname: KubePodCrashLooping}
priority: 10
target: {kind: Pod, name: pod, namespace: namespace}
precursors: [pod-status, pod-previous-logs]
operations: [pod-previous-logs-long, pod-restarts-6h, pod-memory-6h]
---

## What this looks like
One container restarts again and again; the pod sits in CrashLoopBackOff between tries. The useful evidence is the
previous container's exit code and its last log lines, not the current (often empty) run.

## Investigation
- pod-status: the crashed container's last state. Exit code 137 or reason OOMKilled: it hit its memory limit.
  Exit code 1 or another non-zero code with an error in the previous logs: the program failed on its own.
  Exit code 0 with restarts: the process exits when it shouldn't (wrong command, or a job run as a Deployment).
- pod-previous-logs is the crashed run. If it ends before the error, ask for pod-previous-logs-long.
- Check when the last restart was (pod-status, lastState finishedAt): restarts that stopped days ago are history,
  not an ongoing crash loop.
- pod-memory-6h: usage climbing to the limit before each restart is a leak or an undersized limit; flat usage far
  below the limit rules memory out.
- pod-restarts-6h: started at one moment (a deploy, a config change, a dependency going down) or always flapping.
- Errors naming another service (connection refused, timeout, DNS) mean the cause is that service, not this pod.

## Remediation
- OOMKilled with usage rising steadily to the limit: raise the memory limit in the app's Helm values, in git
  (Argo reverts kubectl edits); if it keeps rising after that, it's a leak to fix in code.
- A clear application error (config, missing secret, bad migration, code bug): name the fix in the repo's values or
  code. Restarting won't help.
- A dependency down: say which one; the pod recovers on its own once it's back.
- Unclear from the evidence: say what to check next. Never suggest deleting the pod as a fix for a crash.
