---
name: job-failed
description: >-
  A Job failed (KubeJobFailed): any Job except backups and scrapers, which have their own runbooks. Find why, and
  whether anything will try again.
match: {alertname: KubeJobFailed}
priority: 10
target: {kind: Job, name: job_name, namespace: namespace}
precursors: [job-status]
operations: [job-logs-long, job-failure-kind]
---

## What this looks like
A Job ran out of retries or hit its deadline, and stays failed until deleted, so the alert can be old news. A name
ending in a long number usually comes from a CronJob, whose next run is a fresh try. Other Jobs (a migration, a hook, a
one-off) never run again by themselves.

## Investigation
- job-status: the condition. DeadlineExceeded means it hung or was too slow; BackoffLimitExceeded means the container
  failed, and the error is in the pod's logs. A 404 means the Job is gone and the alert is clearing: say so.
- job-status lists every attempt. With several, the first failed attempt is the cause; later ones can fail on what
  it left behind.
- job-failure-kind: OOMKilled containers and whether the deadline was hit, when the status doesn't settle it.
- If the log tail stops before the error, ask for job-logs-long. No pods left: the logs are gone; say so.
- Errors naming another service (connection refused, timeout, DNS) mean the cause is that service.

## Remediation
- OOMKilled: raise the memory limit in the manifest or values of whatever creates the Job (its CronJob, chart), in git.
  Doubling it is a fair start. Never work out a limit from the amount of data the logs mention.
- DeadlineExceeded: say which step it was on. Raise activeDeadlineSeconds only if the logs show it progressing.
- A clear error (config, missing secret, permissions, code bug): name the log line and the fix. A retry fails the same way.
- A dependency down or a one-off network error, from a CronJob: the next scheduled run will likely succeed; say so. Not
  from a CronJob: it needs running again once the dependency is back.
- Every suggestion ends with this step, whatever the cause: once fixed, `kubectl -n <namespace> delete job <name>`,
  because the alert fires until the failed Job is gone. It is cleanup, never the fix.
