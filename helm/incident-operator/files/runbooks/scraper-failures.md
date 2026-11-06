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
operations: [scrape-history-7d, {run-scrapejob-now: {autonomy: approve}}, {resume-scrapejob: {autonomy: approve}}]
---

## What this looks like
Failing: the latest run ended Failed after retries; it runs again on schedule. Stale: no success for twice its
period. Suspended: repeated failed runs stopped it until someone resumes it.

## Investigation
- scrapejob-status: lastRun (result, exitCode, error, attempts, items), consecutiveFailures, lastSuccess (null: none
  on record), events. The site is the ScrapeJob's name; scraperRef is the code. scrape-last-run is that run's Job
  and log tail; a 404 means it was cleaned up, so use lastRun.
- The log line before the exit says why. Items far below baselineItems: the site changed or serves less.
- scrape-stack-health: failing_per_scraper is already counted, so quote it. Above 1 for this scraper points at a
  shared cause: state the count and the other jobs in what_failed and summary, since the remediation step sees only
  your findings. Many scrapers failing, or webcache, browserless or the VPN proxy at 0 available: shared services.
- scrape-history-7d: Failed 0 and Succeeded 0 means no runs finished (suspended), not that all failed.

## Remediation
Use the first that fits, then add the Suspended or Failing line.
- Findings say the same scraper fails on other sites: say that first, name them, treat it as one problem. Never call
  for a per-site code change. Blocked (403, Cloudflare): the VPN proxy's exit IP, nordvpn-proxy-entrypoint.vpn:8118
  (none of the primer's IPs). It changes exit every 20 minutes. One burst: a later run gets a new exit and may pass,
  so nothing needs doing yet. Only if evidence shows blocks on several exits or across runs: tell the operator
  Cloudflare may be blocking the VPN's ranges, so use another exit or provider. Cloudflare itself can't help.
  Not blocked: the shared code; name the log line.
- A shared service at 0 available: name it and fix that; its scrapers recover once it's back.
- Broken (65) on this site only: needs a code change for this site; name the log line. Retrying won't help.
- Transient (75) on this site only: the site is down or slow; the operator retries it.
- Out of memory (137): raise the ScrapeJob's memory limit in its values.
- Failing: one failed run, still scheduled. Never mention resuming. Say if the next run will likely fail the same way.
  If a new run could pass (blocked, transient, or to test a fix), propose run-scrapejob-now.
- Suspended: retrying has STOPPED, whatever the exit code; never say it fixes itself. Propose resume-scrapejob unless a
  code or memory change must come first. That is one probe run: success recovers it, failure suspends it again.
