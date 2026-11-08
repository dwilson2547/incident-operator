---
name: pvc-filling
description: >-
  A PersistentVolumeClaim is nearly full, or filling fast enough to be full within days. Tell one app filling its
  own volume from the NFS share they all live on filling up.
match: {alertname: KubePersistentVolumeFillingUp}
priority: 10
precursors: [pvc-usage, pvc-free-4d]
operations: []
---

## What this looks like
Critical: under 3% free. Warning: under 15% free and predicted full within 4 days. The alert names the claim in
`namespace` and `persistentvolumeclaim`, and `node` is where the volume is mounted, not where its data lives.
Most claims are storage class nfs-dataset: every one of them is a directory on the same TrueNAS share, their sizes are
not enforced, and each reports the share's free space. So when one fills, they all fill together and the alert fires
for many claims at once. tower-share (storage class tower-smb) is an SMB share on Tower and fills on its own.

## Investigation
- pvc-usage: free_fraction is what the alert saw. storage_class says which case this is. free_fraction_by_storage_class
  showing nfs-dataset at the same value as this claim confirms the shared TrueNAS share is filling, not this claim.
  inodes_free_fraction near 0 with space left means too many small files instead. mounted_by names the pods writing to it.
- pvc-free-4d: a steady fall over days is growth; a sudden drop points at one job or one write; a rise means space was
  freed. Use the fractions: the absolute byte counts the kubelet reports for NFS claims don't match the share's size.
- Nothing here sees inside TrueNAS or which directory grew: say that a person needs to look.

## Remediation
- Free fraction back above the threshold: nothing to do now; say when it recovered.
- nfs-dataset filling: escalate "find what grew on the TrueNAS share /mnt/ssd_pool/nfs_dataset (one directory per
  claim, named after its volume) and free space or grow the pool". Name the claim the alert fired for and the pods
  mounting it, but say it may not be the one that grew.
- tower-share filling: escalate "free space on Tower's share".
- Raising a claim's size in values does nothing for nfs-dataset claims: their size is not enforced.
- Never suggest deleting a claim or its pod to get space back: a claim's data goes with it.
