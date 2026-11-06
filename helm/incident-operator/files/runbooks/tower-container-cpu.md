---
name: tower-container-cpu
description: >-
  A Docker container on Tower (the Unraid server) has averaged over 1 core for hours. Tell a scheduled or expected
  job from new steady load.
match: {alertname: TowerContainerCPUSustained}
priority: 10
precursors: [tower-container-cpu, tower-container-now]
operations: [tower-load]
---

## What this looks like
The rule needs a 1-hour average above 1 core, held for 2 hours, so it fires about 3 hours after the load starts, often
as the load ends. Tower has 12 threads, so 1 core is one pegged thread and little load on the server.
Tower isn't a cluster node: nothing here can see inside the container, and docker commands on Tower are correct advice.

Two known jobs, measured 2026-10-27 to 11-05:
- plex's Scheduled Tasks: daily at 02:07 Eastern (06:07 UTC in summer time, 07:07 UTC in winter), a flat 1 core for 3 to
  5 hours, sometimes minutes near 6 cores. The rule skips plex from 06:00 to 12:00 UTC, so these tasks on their usual
  schedule don't fire it. Plex firing means load outside that window, or held hours past it.
- The monthly parity check: on the 1st from about local midnight, about 16 hours (2026-11-01: 04:13 to 20:03 UTC). It
  runs in the host, so it adds nothing to any container's CPU, and host CPU stayed low during it.

## Investigation
- tower-container-now: cores now, hours since the container started, and the busiest containers. Below 1 core now
  means the load has ended: say so. disks_reading_fast of 5 or more means the parity check is running: say so.
- tower-container-cpu, cores: the shape over 24h, hourly, newest last. Flat near a whole number is a pegged thread;
  bursty is real work coming and going. Say how many hours ago it started; the data has no clock times.
- cores_yesterday is the same hours one day earlier. Compare the two position by position, even if the load has ended:
  load at the same positions in both is a daily schedule, and say so in your findings.
- Started right after the container did: startup work, such as a scan or an index.
- tower-load: only if the server as a whole may be affected.

## Remediation
- Plex load that began at its tasks' start and ran past the skipped window: the tasks ran long. Say so; a parity check
  running may be why, since it slows disk work. Nothing to fix unless it repeats.
- A daily schedule on another container: expected load. If it pages too often, add an exclusion to the
  TowerContainerCPUSustained rule (tower-alerts, cluster-config).
- Plex at another time: check Settings > Status > Dashboard for a long transcode.
- Parity running: never give it as the cause of a container's CPU. It may only stretch work that was already scheduled.
- Load ended and yesterday's data shows no load at those hours: nothing can be checked now; ask to run `docker top <name>` if it returns.
- New, steady load with no matching yesterday and no known job: escalate. A low, steady load is also what a throttled
  miner looks like. On Tower, run `docker top <name>` several times, since short-lived unknown processes may show only for
  seconds, and `docker logs --tail 100 <name>`. Say this can't be told from CPU data: never call it a miner or call it safe.
- The rule mails as critical, but one busy container is not an outage: say so unless the load is new or Tower's is high.
