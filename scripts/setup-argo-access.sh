#!/bin/bash
# One-time: let Argo CD read this repo over SSH, the way it reads gpu-scheduler: a read-only SSH deploy key, public
# half on GitHub, private half in Argo's repository Secret repo-incident-operator. The key pair lives in a temp dir
# for the length of this script and is deleted after.
#   bash infra/incident-operator/scripts/setup-argo-access.sh
set -euo pipefail
REPO=dwilson2547/incident-operator
if kubectl -n argocd get secret repo-incident-operator >/dev/null 2>&1; then
  echo "repo-incident-operator exists: nothing to do"; exit 0
fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
ssh-keygen -q -t ed25519 -N "" -C "argocd@home-cluster ($REPO, read-only)" -f "$TMP/key"
# REST rather than `gh repo deploy-key` (older gh releases lack it).
gh api "repos/$REPO/keys" -f title="argocd (read-only)" -f key="$(cat "$TMP/key.pub")" -F read_only=true --silent
kubectl -n argocd create secret generic repo-incident-operator \
  --from-literal=type=git --from-literal=url="git@github.com:$REPO.git" --from-file=sshPrivateKey="$TMP/key" >/dev/null
kubectl -n argocd label secret repo-incident-operator argocd.argoproj.io/secret-type=repository >/dev/null
echo "deploy key added to $REPO (read-only); Argo repository Secret repo-incident-operator created"
