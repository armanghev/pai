#!/usr/bin/env bash
# Run Pai on this machine. Restarts after /update (exit code 3), the way systemd would.
cd "$(dirname "$0")/.."
while true; do
  uv run pai
  code=$?
  [[ $code -eq 3 ]] || exit $code
  echo "restarting after /update"
done
