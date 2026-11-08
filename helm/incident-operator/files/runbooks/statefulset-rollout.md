---
name: statefulset-rollout
description: >-
  A StatefulSet has had fewer ready replicas than desired for 15 minutes with no rollout progress. Find which pod
  isn't coming up and why.
match: {alertname: KubeStatefulSetReplicasMismatch}
priority: 10
target: {kind: StatefulSet, name: statefulset, namespace: namespace}
precursors: [statefulset-status, statefulset-pods]
operations: [statefulset-replicas-6h, namespace-failed-events, namespace-failedmount-events, namespace-failedscheduling-events, namespace-unhealthy-events, nodes-ready-now]
---

## What this looks like
Ready replicas stayed below desired for 15 minutes while no pod was updated in the last 10. Every StatefulSet here
runs one replica (postgres, the monitoring stores, argocd's controller), so this usually means the one pod is down and
the service with it. The alert's `pod` label is the metrics exporter (prometheus-kube-state-metrics-*), never the
failing workload: don't blame it or investigate it.

## Investigation
- statefulset-status: replicas, ready and updated, and recent events. All ready now means it has cleared: say so.
  A 404 means the StatefulSet is gone.
- statefulset-pods: what the pod is doing. waiting ImagePullBackOff or ErrImagePull: the image or tag can't be pulled.
  waiting CrashLoopBackOff: the app crashes; last_terminated gives the reason. waiting ContainerCreating: its volume
  isn't mounting. pending with no waiting rows: unscheduled. not_ready with no waiting rows: running but failing its
  readiness probe. current_revision and update_revision differing means a rollout is under way and stuck on this pod.
- For the message behind it, ask for the matching check: namespace-failed-events, namespace-failedmount-events,
  namespace-failedscheduling-events, namespace-unhealthy-events. Events last about an hour, so none proves nothing.
- nodes-ready-now: a StatefulSet pod on a NotReady node stays Terminating and is not replaced until the node returns.
- statefulset-replicas-6h: when it began, and whether it is flapping or steady.

## Remediation
- Cleared by now: nothing to do now; say when it recovered if the data shows it. It did fail, so never call it a false
  positive.
- Everything goes through git: Argo reverts kubectl edits, so no manual scaling. Revert the image tag or value in the
  app's repo. A StatefulSet stuck on a broken pod doesn't pick up the reverted spec until that pod is deleted: say
  that the pod may need deleting after the revert lands.
- Image can't be pulled, crash loop, missing secret, failing probe: as for a Deployment; name the image, the crash
  reason from `kubectl -n <namespace> logs <pod> --previous`, the secret, or the probe.
- FailedMount on an NFS volume: suspect the node's NFS client, and never suggest testing with a new mount.
- Pod stuck Terminating on a NotReady node: the node is the problem; force-deleting the pod is only safe once that
  machine is certainly off, because two copies would share the volume. Say that, never advise it outright.
- Never suggest deleting the claim or its data.
