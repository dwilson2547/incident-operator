"""Probes (docs/design.md §5.3): small read-only programs that run *on a node*, as a short Job the operator creates,
and print one JSON object.

`python -m incident_operator.probes <name>` is the Job's command. The pod gets `hostPID` and nothing else: no volumes,
no service-account token, non-root, no capabilities, read-only root filesystem. That is enough to read every host
task's /proc/<pid>/stat and cmdline (checked on the cluster's nodes 2026-11-01); kernel stacks are not readable and not
needed. Probes never mount anything, so a probe can't wedge a node the way a test NFS mount does.

Probes are code in the image, chosen by name from an operation file (`run: {kind: probe, probe: nfs-dstate}`): an
operation can't supply a command.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

PROBES = {}


def probe(fn):
    PROBES[fn.__name__.replace("_", "-")] = fn
    return fn


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tasks(proc: str):
    """(pid, tid, comm, state, ppid, start_seconds_after_boot) for every thread of every process."""
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        try:
            tids = os.listdir(f"{proc}/{pid}/task")
        except OSError:
            tids = [pid]
        for tid in tids:
            try:
                raw = open(f"{proc}/{pid}/task/{tid}/stat").read()
            except OSError:
                try:
                    raw = open(f"{proc}/{pid}/stat").read() if tid == pid else None
                except OSError:
                    raw = None
                if raw is None:
                    continue                       # exited while we looked
            comm = raw[raw.index("(") + 1:raw.rindex(")")]
            f = raw[raw.rindex(")") + 2:].split()
            yield int(pid), int(tid), comm, f[0], int(f[1]), int(f[19]) / hz


@probe
def nfs_dstate(proc: str = "/proc", now: float | None = None) -> dict:
    """Tasks stuck in uninterruptible sleep (D), with the NFS client's signs picked out (design §12, and the
    2026-10-18/22 wedges): `mount.nfs` in D from kubelet's retries, their parent (`nfsplugin` = the csi-nfs-node pod,
    whose deletion kills them), `nfsplugin` threads in D, and the NFSv4 state manager kernel thread
    (`<server>-manager`). On 2026-10-22 there was no manager thread, so it can't be the only sign."""
    now = time.time() if now is None else now
    btime = next(int(line.split()[1]) for line in open(f"{proc}/stat") if line.startswith("btime"))
    comm_of: dict[int, str] = {}
    tasks = list(_tasks(proc))
    for pid, tid, comm, *_ in tasks:
        if pid == tid:
            comm_of[pid] = comm
    d = [t for t in tasks if t[3] == "D"]
    mounts = [t for t in d if t[2] == "mount.nfs" and t[0] == t[1]]
    starts = sorted(btime + t[5] for t in mounts)
    parents = Counter(comm_of.get(t[4], "?") for t in mounts)
    managers = [{"thread": t[2], "state": t[3]} for t in tasks if t[2].endswith("-manager") and t[0] == t[1]
                and any(c.isdigit() for c in t[2])]      # [192.168.0.15-manager], not e.g. charger_manager
    return {
        "dstate_total": len(d),
        "dstate_by_command": dict(Counter(t[2] for t in d).most_common(8)),
        "mount_nfs_dstate_total": len(mounts),
        "mount_nfs_dstate_older_than_10m": sum(1 for s in starts if now - s > 600),
        "mount_nfs_oldest_start": _iso(starts[0]) if starts else None,
        "mount_nfs_parents": dict(parents),
        "parents_are_csi_nfs_node": bool(mounts) and set(parents) == {"nfsplugin"},
        "nfsplugin_threads_dstate": sum(1 for t in d if comm_of.get(t[0]) == "nfsplugin"),
        "state_managers": managers,
        "state_manager_dstate": any(m["state"] == "D" for m in managers),
        "node_booted": _iso(btime),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in PROBES:
        print(json.dumps({"error": f"usage: probes <{'|'.join(PROBES)}>"}))
        return 2
    print(json.dumps(PROBES[argv[0]]()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
