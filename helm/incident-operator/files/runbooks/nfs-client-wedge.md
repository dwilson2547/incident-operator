---
name: nfs-client-wedge
description: >-
  A node's NFS client is wedged: load climbs while the CPU stays idle and pods mounting the share hang in
  ContainerCreating.
match: {alertname: NodeLoadHighCPUIdle}
priority: 100
target: {kind: Node, from: instanceToNode}
precursors: [node-load-cpu-6h, node-failedmount-events, nfs-dstate]
operations: [tower-load, nodes-load-now, node-status, {restart-csi-nfs-node: {autonomy: approve}}]
sourceDocs:
  - infra/cluster-config/docs/issues/2026_10_18_unraid_nfs_v41_client_wedge.md
  - infra/cluster-config/docs/postgres_backup.md
---

## What this looks like
Load rises steadily on one node while its CPU stays mostly idle: processes are stuck waiting on I/O, usually
mount.nfs calls to an NFS server that never answer. Pods on that node that mount NFS volumes sit in
ContainerCreating with FailedMount timeouts. Not this runbook: high load with busy CPU.

## Investigation
- nfs-dstate (read on the node): mount_nfs_dstate_total above 0 confirms a wedged NFS client here; older than 10
  minutes rules out a slow mount. parents_are_csi_nfs_node true means restarting the CSI pod kills them.
  state_manager_dstate true means the client's state manager itself is stuck. If the probe failed or timed out,
  say so: that alone suggests the node is in trouble.
- The wedge signature: load15_per_core above 2 with cpu_busy below 0.5. Below 1 per core is normal load; cpu_busy
  above 0.5 means the load is real work, not a wedge.
- node-load-cpu-6h: a steady climb over hours with low CPU busy fits a wedge; a sudden spike fits something else.
  If the node is below the signature now, the condition has cleared: say so.
- node-failedmount-events: FailedMount timeouts on this node confirm pods are stuck on a volume. The event names
  the server and share.
- nodes-load-now: other nodes also above the signature means the NFS server, not this node's client.
- tower-load: if Tower's load is also high, Tower's nfsd may be deadlocked, and fixing the node will not hold.

## Remediation
- Stuck mount.nfs on this node, parents_are_csi_nfs_node true, only this node affected and Tower looks normal:
  propose restart-csi-nfs-node. The stuck mounts are the CSI pod's children and die with it.
- mount_nfs_dstate_total 0 and load normal: the condition has cleared; nothing to do.
- Tower load also high, or several nodes affected: do not restart (operation none); escalate "check Tower's nfsd for stuck threads
  (needs root on Tower)".
- Stuck mounts come back after the restart: escalate "reboot the node".
- Never suggest testing with a new NFS mount from a cluster node: it wedges that node too.
