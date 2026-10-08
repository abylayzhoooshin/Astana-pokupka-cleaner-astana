#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STACK_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${ENV_FILE:-$STACK_DIR/.env}
stack_data_root=""
if [[ -f "$ENV_FILE" ]]; then
  stack_data_root=$(grep -E '^DATA_ROOT=' "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)
fi
DATA_ROOT=${DATA_ROOT:-${stack_data_root:-/srv/rieltor}}
BACKUP_ROOT=${BACKUP_ROOT:-/srv/rieltor-backups}

[[ -f "$DATA_ROOT/.rieltor-data-root" ]] || {
  echo "Refusing restore: missing $DATA_ROOT/.rieltor-data-root" >&2
  exit 1
}
[[ -f "$BACKUP_ROOT/.rieltor-backup-root" ]] || {
  echo "Refusing restore: missing $BACKUP_ROOT/.rieltor-backup-root" >&2
  exit 1
}

[[ $# -eq 1 ]] || { echo "Usage: $0 /srv/rieltor-backups/rieltor-*.tar.zst" >&2; exit 2; }
archive=$(realpath -e -- "$1")
backup_root=$(realpath -e -- "$BACKUP_ROOT")
[[ "$archive" == "$backup_root"/rieltor-*.tar.zst ]] || {
  echo "Refusing archive outside $backup_root" >&2
  exit 1
}

sha_file="$archive.sha256"
[[ -f "$sha_file" ]] || { echo "Missing $sha_file" >&2; exit 1; }
(cd -- "$(dirname -- "$archive")" && sha256sum -c "$(basename -- "$sha_file")")

restore_tmp=$(mktemp -d "$BACKUP_ROOT/.restore.XXXXXX")
trap 'rm -rf -- "$restore_tmp"' EXIT
tar --zstd -xf "$archive" -C "$restore_tmp"
[[ -d "$restore_tmp/data/collector" && -d "$restore_tmp/data/cleaner" ]] || {
  echo "Archive does not contain the expected data tree" >&2
  exit 1
}
[[ -f "$restore_tmp/data/.rieltor-data-root" ]] || {
  echo "Archive is missing the data-root safety marker" >&2
  exit 1
}

while IFS= read -r -d '' db; do
  check=$(sqlite3 "$db" 'PRAGMA quick_check;')
  [[ "$check" == "ok" ]] || { echo "Invalid database $db: $check" >&2; exit 1; }
done < <(find "$restore_tmp/data" -type f -name '*.db' -print0)

echo "This stops the stack and replaces $DATA_ROOT with the selected backup."
read -r -p "Type RESTORE to continue: " confirmation
[[ "$confirmation" == "RESTORE" ]] || { echo "Cancelled."; exit 1; }

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
rollback="${DATA_ROOT}.before-restore-$timestamp"
[[ ! -e "$rollback" ]] || { echo "$rollback already exists" >&2; exit 1; }

cd -- "$STACK_DIR"
docker compose --env-file "$ENV_FILE" down
mv -- "$DATA_ROOT" "$rollback"
mv -- "$restore_tmp/data" "$DATA_ROOT"

if ! docker compose --env-file "$ENV_FILE" up -d collector cleaner; then
  echo "Start failed; restored data remains at $DATA_ROOT." >&2
  echo "Previous data is recoverable at $rollback." >&2
  exit 1
fi

echo "Restore started. Previous data was preserved at $rollback."
