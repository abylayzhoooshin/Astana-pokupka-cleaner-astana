#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STACK_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${ENV_FILE:-$STACK_DIR/.env}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

command -v docker >/dev/null || fail "docker is not installed"
docker compose version >/dev/null || fail "Docker Compose v2 is unavailable"
[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE (copy stack.env.example)"

for repo_var in COLLECTOR_REPO_PATH CLEANER_REPO_PATH; do
  value=$(grep -E "^${repo_var}=" "$ENV_FILE" | tail -n1 | cut -d= -f2-)
  [[ -n "$value" && -d "$value" ]] || fail "$repo_var does not point to a directory"
  [[ -f "$value/Dockerfile" ]] || fail "$value/Dockerfile is missing"
done

if grep -Eq '^(COLLECTOR_API_KEY|CLEANER_API_KEY|OPENAI_API_KEY)=(|CHANGE_ME)$' "$ENV_FILE"; then
  fail "replace all CHANGE_ME/empty secrets in $ENV_FILE"
fi

chmod 600 "$ENV_FILE"
docker compose --env-file "$ENV_FILE" -f "$STACK_DIR/compose.yaml" config --quiet

data_root=$(grep -E '^DATA_ROOT=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)
data_root=${data_root:-/srv/rieltor}
[[ -f "$data_root/.rieltor-data-root" ]] || fail "missing data-root safety marker"
for part in collector cleaner main; do
  [[ -d "$data_root/$part" ]] || fail "missing $data_root/$part"
done

free_kb=$(df -Pk "$data_root" | awk 'NR==2 {print $4}')
(( free_kb >= 20 * 1024 * 1024 )) || fail "less than 20 GiB free on data filesystem"

echo "Preflight passed. Safe defaults still need to be checked manually:"
grep -E '^(COLLECTOR_PAUSED|MIN_BATCH_SIZE|ENTITY_MANUAL_WRITES_ENABLED)=' "$ENV_FILE"
