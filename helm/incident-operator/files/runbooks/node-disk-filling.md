---
name: node-disk-filling
description: >-
  A filesystem on a cluster node is nearly full, or predicted to fill within a day. Say how soon, whether it is a
  steady fill or a sudden one, and what the kubelet will do when it gets there.
match:
  alertname: [NodeFilesystemSpaceFillingUp, NodeFilesystemAlmostOutOfSpace]
priority: 10
target: {kind: Node, from: instanceToNode}
precursors: [node-filesystem-usage, node-filesystem-2d]
operations: [node-status]
---

## What this looks like
SpaceFillingUp: under 15% free and predicted full within 24 hours (warning), or under 10% and full within 4 hours
(critical). AlmostOutOfSpace: under 5% free (warning) or 3% (critical) for 30 minutes. The alert's `instance` is the
node's IP:9100 and `mountpoint` the filesystem. These alerts cover the four cluster nodes only, not Tower or the
TrueNAS. On these nodes everything but /boot and /boot/efi is on the root filesystem /, container images and logs
included.

## Investigation
- node-filesystem-usage: free_fraction and free_gib for the alert's mountpoint now. free_gib_in_24h below 0 means
  full within a day at the last 6 hours' rate. Other mountpoints show whether more than one is filling.
- node-filesystem-2d: a steady fall over days is growth (logs, images, a local volume); a sudden drop is one big write
  or download. A rise means space was freed: say when.
- node-status: DiskPressure=True means the kubelet has started evicting pods from this node.
- The kubelet on these nodes deletes unused container images once the disk is 85% used, and evicts pods when under
  1 GiB is left. So a full / that image cleanup didn't help is filled by something else: logs or data.

## Remediation
- Free space back above the threshold: nothing to do now; say when it recovered.
- Nothing here can look inside the node's filesystem. Escalate "on the node, find what grew:
  `sudo du -xh --max-depth=3 / | sort -h | tail -20`", naming the node and how many hours are left.
- If the systemd journal is the culprit: `sudo journalctl --vacuum-size=1G` on the node.
- /boot filling: old kernels; escalate "`sudo apt autoremove` on the node".
- DiskPressure already set: pods on this node are being evicted; say which matter and that it clears once space is freed.
- Never suggest deleting files under /var/snap/microk8s or a pod's volume by hand.
