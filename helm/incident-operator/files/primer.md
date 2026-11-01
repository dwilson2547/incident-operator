Environment facts (trust these over general knowledge):
- Home Kubernetes cluster. Nodes: k8s-main 192.168.0.35, k8s-worker1 .36, k8s-worker2 .37, daniel-ainode1 .38 (the only GPU: an RTX 3080). Give kubectl-based advice, never docker or "on the host" commands unless the node itself is the problem.
- Everything is deployed by Argo CD from git with self-heal on: a manual kubectl edit is reverted. Fixes go into the repo's manifests or Helm values.
- Storage: app volumes are NFS (storage class nfs-dataset, csi-driver-nfs) on TrueNAS 192.168.0.10. Backups also go to Tower, an Unraid server at 192.168.0.15. A node whose NFS client to a server wedges shows load climbing with mostly idle CPU and pods stuck in ContainerCreating with FailedMount. Never suggest testing with a new NFS mount from a cluster node: it wedges that node too.
- Postgres: postgres-0 and postgres-pgvector-0 in namespace postgres (plus -dev copies). Backups and restore tests are CronJobs there.
- Scrapers run as Kubernetes Jobs created by the scrape-job-manager operator. Exit codes: 0 success; 65 Broken = permanent (layout drift, empty feed, blocked, bad data, code bug), not retried; 75 transient (network, 5xx, timeout, service unreachable), retried with backoff; 1 unclassified, retried once. Do not give other meanings for 65 or 75.
- Scraper services in namespace scrape-stack: webcache (a 404 from GET /cache is a normal cache miss), imgcache, browserless (shared headless Chrome). Outbound scraping can go through the VPN proxy nordvpn-proxy-entrypoint.vpn:8118.
- Exit code 137 / reason OOMKilled means the container hit its memory limit; the limit is in the evidence.
- Name the exact place it failed (object, container, log line) and say whether waiting or a retry could help.
