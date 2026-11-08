---
name: daemonset-rollout
description: >-
  A DaemonSet's rollout has made no progress for 15 minutes: pods missing, not updated, not ready, or on the wrong
  nodes. Find which node's pod is the holdout and why.
match: {alertname: KubeDaemonSetRolloutStuck}
priority: 10
target: {kind: DaemonSet, name: daemonset, namespace: namespace}
precursors: [daemonset-status, daemonset-pods]
operations: [daemonset-scheduled-6h, namespace-failed-events, namespace-failedmount-events, namespace-unhealthy-events, nodes-ready-now]
---

## What this looks like
A DaemonSet runs one pod per node it selects: on all four nodes for networking, storage and monitoring agents
(calico-node, csi-nfs-node, csi-smb-node, traefik, speaker, node-exporter, otel-agent), and only on daniel-ainode1 for
the gpu-operator-resources ones. The alert fires when current, updated or available pods fall short of desired, or
pods run on nodes they shouldn't, for 15 minutes. The alert's `pod` label is the metrics exporter
(prometheus-kube-state-metrics-*), never the failing workload.

## Investigation
- daemonset-status: desired, current, ready and unavailable counts, and recent events. All equal now means it has
  cleared: say so.
- daemonset-pods: not_ready gives each holdout pod and its node. waiting and last_terminated give the reason, as for any
  pod (ImagePullBackOff, CrashLoopBackOff, ContainerCreating). nodes_not_ready listing that same node means the node
  is down, not the DaemonSet: its pod can't become ready until the node is back.
- daemonset-scheduled-6h: when it began, whether it is flapping, and whether updated is stuck below desired during a
  rollout (the default rolling update replaces one node's pod at a time and waits for it).
- For the message behind it: namespace-failed-events, namespace-failedmount-events, namespace-unhealthy-events.
  Events last about an hour, so none proves nothing.

## Remediation
- Cleared by now: nothing to do now; say when it recovered. It did fail, so never call it a false positive.
- Holdout on a NotReady node: the node is the problem; point at it and nothing else.
- Otherwise everything goes through git: revert or fix the image tag or value in the repo the DaemonSet comes from.
- calico-node or csi-*-node not ready on a node takes out pod networking or volumes there: say the node's pods are
  affected too.
- Never suggest deleting pods as the fix: the replacement fails the same way.
