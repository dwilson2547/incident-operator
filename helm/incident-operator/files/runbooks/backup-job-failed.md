---
name: backup-job-failed
description: >-
  A backup or export Job from a CronJob failed (Postgres dumps, volume and object backups, restore tests, the
  offsite copy, the secrets export).
match:
  alertname: [PostgresBackupJobFailed, OffsiteBackupJobFailed, SecretsBackupJobFailed]
priority: 10
target: {kind: Job, name: job_name, namespace: namespace}
precursors: [job-status, cronjob-history]
operations: [job-logs-long, tower-load]
---

## What this looks like
One run of a backup CronJob failed. The next scheduled run is a fresh try; the separate "Stale" alert fires only if
there is no success for a day or more. So the question is whether the next run will fail the same way.

## Investigation
- job-status: the Job's condition. DeadlineExceeded means the run hung or was too slow, most often on the NFS
  backup share; BackoffLimitExceeded means the container failed with an error, which is in the pod's logs.
- Exit code 137 or OOMKilled: the copy tool ran out of memory (rclone and pg_dump on large data; rclone listing a
  big bucket counts). rclone's "Config file not found - using defaults" is normal here: its remotes come from
  environment variables. It is never the cause.
- Log lines with "permission denied", "no space left", authentication or connection errors name the cause directly.
- If the log tail stops before the error, ask for job-logs-long.
- cronjob-history: several failures in 7 days point at a lasting cause; a first failure after many successes at a
  passing one. A long time since the last success makes it urgent.
- A run that hung on the backup share: ask for tower-load.

## Remediation
- OOMKilled: raise the Job's memory limit in its manifest or values in git.
- Hung on the NFS share with Tower loaded: escalate "check Tower and the NFS client on the node"; the next run will
  hang too. Never suggest testing with a new NFS mount from a cluster node.
- A one-off network or upstream error with a good history: the next scheduled run will likely succeed; say when to
  check.
- Credentials, permissions or a full disk: name the secret or volume to fix. Retrying won't help.
- The failed Job keeps its alert firing until it is deleted; say to delete it once the cause is understood.
