#!/usr/bin/env bash
# Stop what start.sh started. The Postgres container is kept (`docker rm -f io-pg` to drop it).
cd "$(dirname "$0")"
if [ -f .pids/io.pid ]; then
  kill "$(cat .pids/io.pid)" 2>/dev/null && echo "stopped incident-operator" || echo "not running"
  rm -f .pids/io.pid
fi
