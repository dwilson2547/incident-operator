---
name: scraper-failures
description: >-
  A ScrapeJob's run failed after its retries (Failing), it is stale (no successful run in twice its period), or it
  was auto-suspended after repeated failed runs. Find whether the site, the scraper or the shared services broke.
match:
  alertname: [ScrapeJobFailing, ScrapeJobStale, ScrapeJobSuspended]
priority: 10
target: {kind: ScrapeJob, name: scrape_job, namespace: exported_namespace}
precursors: [scrapejob-status, scrape-last-run, scrape-stack-health]
operations: [scrape-history-7d]
---

## What this looks like
scrape-job-manager runs each ScrapeJob as Kubernetes Jobs, retries transient failures, and suspends a ScrapeJob
after repeated failed runs. Failing means its latest run ended Failed (transient errors are retried first) and
it will run again on its schedule; Stale means no success for twice its period; Suspended means it stopped trying.

## Investigation
- scrapejob-status: lastRun (result, exitCode, error, attempts, items), consecutiveFailures, lastSuccess (null:
  none on record), events (phase changes with their exit codes and errors). The site is the ScrapeJob's own name;
  scraperRef names the scraper code, not a site. scrape-last-run is that run's Job: its pod's exit code and log
  tail. A 404 there means the Job was already cleaned up: use lastRun instead.
- Exit 65 (Broken) is permanent: the scraper decided the data is wrong (layout changed, empty feed, blocked, bad
  data, or a bug). The log line just before the exit says which. Exit 75 is transient (network, 5xx, timeout,
  a service unreachable) and was retried. Exit 1 is unclassified. 137 is out of memory.
- Items far below baselineItems in recent runs: the site changed or is serving less.
- scrape-stack-health lists every ScrapeJob failing right now (consecutive failures) and the scraper code it runs;
  failing_per_scraper is the count per scraper, already counted: quote it, don't count the list yourself. Other jobs
  with the same scraper failing too means the cause is shared, not this site: the scraper's code, or how the requests leave (the VPN proxy's IP blocked or rate-limited, browserless).
  Many different scrapers failing, or webcache, browserless or the VPN proxy with 0 available, points at the shared
  services.
- scrape-history-7d counts finished runs by result: Failed 0 and Succeeded 0 means no runs finished (it was
  suspended), not that they all failed.

## Remediation
- ScrapeJobFailing is one failed run, not an outage: it is still scheduled and a fix takes effect on its next run.
  Say what broke and whether the next run will likely fail the same way (Broken 65, out of memory) or not (a
  one-off transient). Never mention resuming for ScrapeJobFailing: it isn't suspended.
- Other jobs of the same scraper failing too: say so first, name them, treat it as one problem. Blocked (403,
  Cloudflare) on several sites at once points at the egress (the VPN proxy's exit IP), not each site's code: check
  that before changing the scraper. Name the proxy only as nordvpn-proxy-entrypoint.vpn:8118; none of the
  primer's IPs is it.
- ScrapeJobSuspended means retrying has STOPPED, whatever the exit code: it won't run until someone resumes it.
  Never say it fixes itself. Say what to fix first, then to resume it with
  `kubectl -n <namespace> annotate scrapejob <name> scrape.danwils.com/resume=$(date +%s) --overwrite`
  (for transient errors, once the site answers again), with the Target's namespace and name.
- Broken (65) on this site only (layout drift, empty feed, blocking): the scraper needs a code change for this
  site; name the log line. Retrying won't help. If suspended, resume after the fix ships.
- Transient (75) on this site only, not suspended: the site is down or slow; it retries on its own.
- A shared service down: name it; every scraper using it recovers once it's back.
- Out of memory: raise the scraper's memory limit in its values.
