---
name: deployment-rollout
description: >-
  A Deployment's rollout stopped progressing, or it has had fewer available replicas than desired for 15 minutes.
  Find which pods aren't coming up and why.
match:
  alertname: [KubeDeploymentRolloutStuck, KubeDeploymentReplicasMismatch]
priority: 10
target: {kind: Deployment, name: deployment, namespace: namespace}
precursors: [deployment-status, deployment-pods]
operations: [deployment-replicas-6h, namespace-failed-events, namespace-failedmount-events, namespace-failedscheduling-events, namespace-unhealthy-events]
---

## What this looks like
New or replacement pods of one Deployment aren't becoming ready. RolloutStuck means the Progressing condition went
false (ProgressDeadlineExceeded); ReplicasMismatch means available stayed below desired with no rollout moving.
The alert's `pod` label is the metrics exporter (prometheus-kube-state-metrics-*), never the failing workload: don't
blame it or investigate it.

## Investigation
- deployment-status: replicas, updated and available, and the conditions. All available and Progressing true now
  means it has cleared: say so. A 404 means the Deployment is gone.
- deployment-pods: what each of this Deployment's pods is doing. images lists two images during a rollout: the new one
  is the one failing, and old pods still serving means there is no outage yet.
  waiting ImagePullBackOff or ErrImagePull: the image or tag can't be pulled. waiting CrashLoopBackOff: the app crashes;
  last_terminated gives the reason. waiting ContainerCreating: a volume or secret isn't mounting.
  waiting CreateContainerConfigError: a secret or configmap key is missing. pending with no waiting rows: unscheduled.
  not_ready with no waiting rows: running but failing its readiness probe.
- For the message behind it, ask for the matching check: namespace-failed-events (pull errors, missing secret),
  namespace-failedmount-events, namespace-failedscheduling-events, namespace-unhealthy-events. Events last about an
  hour, so none proves nothing.
- deployment-replicas-6h: when it began, and whether it is flapping or steady.

## Remediation
- Cleared by now: say nothing is needed, and when it recovered if the data shows it.
- Everything goes through git: Argo reverts kubectl edits, so no `kubectl rollout undo` or manual scaling. To get back
  fast, revert the image tag or value in the app's repo.
- Image can't be pulled: name the image; fix the tag in values, or publish the image if CI never pushed it.
- CrashLoopBackOff: read the crashed pod's logs: `kubectl -n <namespace> logs <pod> --previous`; fix the error or the
  memory limit (OOMKilled) in code or values.
- Missing secret or configmap: name it. Secrets are created by hand, not by Argo; create it and the pod starts itself.
- FailedMount on an NFS volume: suspect the node's NFS client, and never suggest testing with a new mount.
- FailedScheduling: quote the message. A GPU request only fits daniel-ainode1. Lower the request or free the node.
- Failing readiness probe: name the probe failure; fix the app, its dependency or the probe in values.
- Say whether old pods still serve traffic. Never suggest deleting pods as the fix: replacements fail the same way.
