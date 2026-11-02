# incident-operator backlog

## Next

- [ ] **TowerContainerCPUSustained needs alert work and a runbook** (flagged 2026-11-02, incident #28).
  It fires for `plex` about once a day, and by the time anyone looks there's nothing to see.
  Measured 2026-10-27 → 11-02 (Prometheus `ALERTS` + `container_cpu_usage_seconds_total{job="tower-cadvisor"}`):
  - Plex goes from 0.00 to a flat **1.02 cores** (one thread pegged, of Tower's 12) from about 07:00 to 10:05 UTC,
    then back to 0.00. On 11-02 it ran 07:02–10:05. It goes pending every day at about 07:00–09:00 UTC and fires on
    4 of 7 days. It's Plex's Scheduled Tasks, which run daily between 2:00 and 6:00 AM by default. That's expected
    load, not a problem (user, 2026-11-02).
  - Why nothing shows when you check: a 1h average plus `for: 2h` means the alert can only fire about 3 h after the
    load starts. That's about when the job ends, so on 11-02 it fired at 10:03 and the load had stopped by 10:05.
    It also mails as `critical`.
  - The summary says "for hours", but the expression is a 1h average.
  - Triage had no evidence (`target: null`; Tower isn't a cluster object), so the model just restated the alert.

  Work:
  1. Alert (cluster-config `monitoring/manifests/tower-monitoring.yaml`): decide what it's for (miner detection)
     and exclude or explain a known Plex maintenance window instead of paging on it. Compare with
     `TowerCPUElevated`. Reconsider `critical`.
  2. Runbook `tower-container-cpu`: a Prometheus op giving the container's CPU over the last 6–24h with the start
     time, the shape (flat or bursty) and same-time-yesterday, so the model can say "daily scheduled-task shape"
     vs "new and steady". A read-only `docker top` from Tower would need an SSH probe (none exists; out of scope
     unless that changes).
