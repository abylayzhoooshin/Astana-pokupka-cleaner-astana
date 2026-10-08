#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STACK_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
STACK_ENV=${STACK_ENV:-$STACK_DIR/.env}
stack_data_root=""
collector_host_port=""
cleaner_host_port=""
if [[ -f "$STACK_ENV" ]]; then
  stack_data_root=$(grep -E '^DATA_ROOT=' "$STACK_ENV" | tail -n1 | cut -d= -f2- || true)
  collector_host_port=$(grep -E '^COLLECTOR_HOST_PORT=' "$STACK_ENV" | tail -n1 | cut -d= -f2- || true)
  cleaner_host_port=$(grep -E '^CLEANER_HOST_PORT=' "$STACK_ENV" | tail -n1 | cut -d= -f2- || true)
fi
DATA_ROOT=${DATA_ROOT:-${stack_data_root:-/srv/rieltor}}
COLLECTOR_HOST_PORT=${COLLECTOR_HOST_PORT:-${collector_host_port:-8001}}
CLEANER_HOST_PORT=${CLEANER_HOST_PORT:-${cleaner_host_port:-8002}}
MIN_FREE_GIB=${MIN_FREE_GIB:-10}
failed=0

if [[ ! -f "$DATA_ROOT/.rieltor-data-root" ]]; then
  echo "Missing data-root safety marker under $DATA_ROOT" >&2
  exit 1
fi

for endpoint in \
  "http://127.0.0.1:${COLLECTOR_HOST_PORT}/health" \
  "http://127.0.0.1:${CLEANER_HOST_PORT}/health"; do
  if ! curl --fail --silent --show-error --max-time 15 "$endpoint" >/dev/null; then
    echo "Health endpoint failed: $endpoint" >&2
    failed=1
  fi
done

available_kb=$(df -Pk "$DATA_ROOT" | awk 'NR==2 {print $4}')
required_kb=$((MIN_FREE_GIB * 1024 * 1024))
if (( available_kb < required_kb )); then
  echo "Low disk space under $DATA_ROOT: less than ${MIN_FREE_GIB} GiB free" >&2
  failed=1
fi

exit "$failed"
