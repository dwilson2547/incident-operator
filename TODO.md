# incident-operator backlog

## Next

- [ ] **Shrink scraper logs before the model sees them** (idea 2026-11-02, from the Qwen3.8 27B replay).
  Scraper logs are mostly repeated progress/retry lines with the cause at the bottom; #24's prompt was ~8.7k tokens
  and the 27B spent ~4 of its ~5 min on the first call just reading it (~37 tok/s prompt on the Mac). Code first, in
  the evidence step: collapse repeated lines ("×412 retrying page …"), always keep a traceback and the last N lines
  whole, keep lines matching error/blocked/exit with a little context. A small-model extraction pass only for what
  code can't trim. Helps every model (shorter prompts for the 9B and Gemma too). Measurements:
  `ai/gpu-scheduler/docs/remote-worker-mac.md` "Big-model replay".

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
  1. ~~Alert~~ done 2026-11-02 (cluster-config 8234bf9): plex is skipped 06:00–12:00 UTC (`unless on(name)`), and
     the summary no longer says "for hours". Replayed over 10-26..11-02 it returns nothing. Still `critical`.
  2. Runbook `tower-container-cpu`: a Prometheus op giving the container's CPU over the last 6–24h with the start
     time, the shape (flat or bursty) and same-time-yesterday, so the model can say "daily scheduled-task shape"
     vs "new and steady". A read-only `docker top` from Tower would need an SSH probe (none exists; out of scope
     unless that changes).
