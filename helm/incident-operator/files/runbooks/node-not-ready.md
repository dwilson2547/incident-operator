---
name: node-not-ready
description: >-
  A cluster node has been NotReady or unreachable for 15 minutes, or its readiness keeps flipping. Tell a machine
  that is down from a kubelet that is failing on a running machine, and say what the outage takes out.
match:
  alertname: [KubeNodeNotReady, KubeNodeUnreachable, KubeNodeReadinessFlapping]
priority: 10
target: {kind: Node, name: node}
precursors: [node-status, node-ready-6h, nodes-ready-now]
operations: [node-pods-not-ready, nodes-load-now, node-failedmount-events]
---

## What this looks like
NotReady: the node's Ready condition has been false or unknown for 15 minutes; cordoned nodes are left out.
Unreachable: the node controller has heard nothing from the node's kubelet and tainted it unreachable.
ReadinessFlapping: Ready changed more than 2 times in 15 minutes. On these nodes the kubelet runs inside microk8s'
kubelite service, which also restarts on every node when a microk8s addon or setting changes; a short dip on all nodes
at once is that, not a failing node.

## Investigation
- node-status: Ready=Unknown with "Kubelet stopped posting node status" means the kubelet or the whole machine has
  gone quiet. Ready=False with a message means the kubelet runs but reports a problem (container runtime, network
  plugin): quote it. MemoryPressure, DiskPressure or PIDPressure true names the cause. A 404 means the node was removed.
- node-ready-6h: when it went NotReady and whether it came back. node_exporter_up is the machine itself: 0 alongside
  ready 0 means the machine or its network is down; 1 while ready is 0 means the machine is up and only the kubelet is
  failing. hours_since_boot dropping means it rebooted; no data for it while the machine is down is expected.
- nodes-ready-now: other nodes NotReady too means a shared cause (microk8s itself, the network), not this machine.
- node-pods-not-ready: what is down with it. daniel-ainode1 is the only GPU node, so GPU work stops while it is out.
- nodes-load-now: this node's load per core far above the others' suggests it is overloaded rather than broken.
- node-failedmount-events: FailedMount timeouts here point at a wedged NFS client, which can make the kubelet stall.
- If Prometheus data is missing altogether, Prometheus or kube-state-metrics may run on the failed node: say so.

## Remediation
- Ready again: nothing to do now; say when it recovered and whether it rebooted. It did fail, so never call it a
  false positive.
- Machine down (node_exporter_up 0): escalate "check the machine's power and network; it needs hands on it".
- Machine up, kubelet failing: escalate "on the node, `sudo journalctl -u snap.microk8s.daemon-kubelite` for why, then
  `sudo snap restart microk8s.daemon-kubelite`".
- Pressure condition: say which; DiskPressure is the disk filling, MemoryPressure is a workload's memory.
- Several nodes at once: suspect microk8s or the network as a whole; don't advise restarting nodes one by one.
- Pods are evicted on their own once the node has been NotReady or unreachable for 5 minutes, and Deployments
  reschedule them elsewhere. StatefulSet pods are not replaced: they stay Terminating until the node returns, so
  postgres and the monitoring StatefulSets on that node stay down. Force-deleting one is only safe once the machine is
  certainly off, because two copies would share the volume: say that, never advise it outright.
- Never suggest draining or deleting the node as the fix.
