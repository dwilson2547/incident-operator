---
name: target-down
description: >-
  Prometheus has failed to scrape more than 10% of a scrape job's targets for 10 minutes. Name which targets are
  down, and tell a dead exporter or host from a pod that isn't ready.
match: {alertname: TargetDown}
priority: 10
precursors: [target-down-instances, target-up-6h]
operations: [nodes-ready-now]
---

## What this looks like
The alert names the scrape job in `job`, plus `namespace` and `service` for in-cluster jobs. Jobs tower-node and
tower-cadvisor scrape Tower (192.168.0.15) and mac-node scrapes the Mac mini (192.168.0.30): those targets are outside
the cluster and have no pod. The only metrics lost are the job's own; the service itself may be fine.

## Investigation
- target-down-instances: down lists each failing target's instance and pod. targets is how many the job has, so
  1 of 1 down is the whole job. down_scrape_seconds near the scrape timeout means the target hangs rather than
  refusing. pod_ready 0 for a down target's pod means the pod itself isn't ready: then the cause is the pod, not
  scraping.
- target-up-6h: when it began, and whether it is flapping or steady. Back to all targets up means it has cleared.
- No pod for the target and the job is tower-* or mac-node: the host or its exporter is down or unreachable.
- nodes-ready-now: a down node takes its node-exporter and every pod on it with it; say so if one is NotReady.

## Remediation
- All targets up again: nothing to do now; say when it recovered. A single short dip is a missed scrape, not an
  outage, but it did fire, so never call it a false positive.
- Pod not ready: name it and say its own state is the problem; Kubernetes restarts it, nothing to do here.
- Pod ready but not scraped: the metrics port or path is wrong or the app stopped serving metrics; escalate with the
  instance and pod.
- tower-node or tower-cadvisor down: escalate "check Tower is up and node_exporter or cAdvisor runs on it".
- mac-node down: escalate "check the Mac mini is awake and logged in; node_exporter there starts at login".
- Never suggest restarting Prometheus: one job down means the target, not Prometheus.
