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

## Investigation
- tower-container-now: cores now, hours since the container started, and the busiest containers. Below 1 core now
  means the load has ended: say so.
- tower-container-cpu, cores: the shape over 24h, hourly, newest last. Flat near a whole number is a pegged thread;
  bursty is real work coming and going. Say how many hours ago it started; the data has no clock times.
- cores_yesterday is the same hours one day earlier. Compare the two position by position, even if the load has ended:
  load at the same positions in both is a daily schedule, and say so in your findings.
- Started right after the container did: startup work, such as a scan or an index.
- tower-load: only if the server as a whole may be affected.

## Remediation
- A daily schedule, or plex's scheduled tasks (they run overnight): expected load, nothing to fix. If it pages too often,
  change the exclusion in the TowerContainerCPUSustained rule (tower-alerts, cluster-config).
- Plex at another time: check Settings > Status > Dashboard for a long transcode.
- Load ended and yesterday's data shows no load at those hours: nothing can be checked now; ask to run `docker top <name>` if it returns.
- New, steady load with no matching yesterday and no known job: escalate. A low, steady load is also what a throttled
  miner looks like. On Tower, run `docker top <name>` several times, since short-lived unknown processes may show only for
  seconds, and `docker logs --tail 100 <name>`. Say this can't be told from CPU data: never call it a miner or call it safe.
- The rule mails as critical, but one busy container is not an outage: say so unless the load is new or Tower's is high.
