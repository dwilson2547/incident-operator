---
name: scraper-failures
description: >-
  A ScrapeJob is stale (no successful run in twice its period) or was auto-suspended after repeated failed runs.
  Find whether the site, the scraper or the shared services broke.
match:
  alertname: [ScrapeJobStale, ScrapeJobSuspended]
priority: 10
target: {kind: ScrapeJob, name: scrape_job, namespace: exported_namespace}
precursors: [scrapejob-status, scrape-last-run]
operations: [scrape-history-7d, scrape-stack-health]
---

## What this looks like
scrape-job-manager runs each ScrapeJob as Kubernetes Jobs, retries transient failures, and suspends a ScrapeJob
after repeated failed runs. Stale means no success for twice its period; Suspended means it stopped trying.

## Investigation
- scrapejob-status: lastRun.result, attempts and items; consecutiveFailures; lastSuccess. scrape-last-run is that
  run's Job: its pod's exit code and log tail.
- Exit 65 (Broken) is permanent: the scraper decided the data is wrong (layout changed, empty feed, blocked, bad
  data, or a bug). The log line just before the exit says which. Exit 75 is transient (network, 5xx, timeout,
  a service unreachable) and was retried. Exit 1 is unclassified. 137 is out of memory.
- Items far below baselineItems in recent runs: the site changed or is serving less.
- scrape-stack-health: many ScrapeJobs failing at once, or webcache, browserless or the VPN proxy down, points at
  the shared services, not this site.
- scrape-history-7d: one bad run after steady success, or failing all week.

## Remediation
- Broken (65) from layout drift, an empty feed or blocking: the scraper needs a code change for this site; name
  the log line. Retrying won't help. Resume the ScrapeJob after the fix ships.
- Transient (75) on this site only: the site is down or slow; it retries on its own. Suggest checking the site.
- A shared service down: name it; every scraper using it recovers once it's back.
- Out of memory: raise the scraper's memory limit in its values.
