#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STACK_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
STACK_ENV=${STACK_ENV:-$STACK_DIR/.env}
stack_data_root=""
if [[ -f "$STACK_ENV" ]]; then
  stack_data_root=$(grep -E '^DATA_ROOT=' "$STACK_ENV" | tail -n1 | cut -d= -f2- || true)
fi
DATA_ROOT=${DATA_ROOT:-${stack_data_root:-/srv/rieltor}}
BACKUP_ROOT=${BACKUP_ROOT:-/srv/rieltor-backups}
BACKUP_ENV=${BACKUP_ENV:-/etc/rieltor/backup.env}
LOCK_FILE=${LOCK_FILE:-/run/lock/rieltor-backup.lock}

command -v sqlite3 >/dev/null
command -v rsync >/dev/null
command -v zstd >/dev/null

[[ -f "$DATA_ROOT/.rieltor-data-root" ]] || {
  echo "Refusing backup: missing $DATA_ROOT/.rieltor-data-root" >&2
  exit 1
}
[[ -f "$BACKUP_ROOT/.rieltor-backup-root" ]] || {
  echo "Refusing retention cleanup: missing $BACKUP_ROOT/.rieltor-backup-root" >&2
  exit 1
}

install -m 0700 -d "$BACKUP_ROOT"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another backup is running; exiting."; exit 0; }

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
stage="$BACKUP_ROOT/.staging-$timestamp"
archive_tmp="$BACKUP_ROOT/.rieltor-$timestamp.tar.zst.tmp"
archive="$BACKUP_ROOT/rieltor-$timestamp.tar.zst"
trap 'rm -rf -- "$stage" "$archive_tmp"' EXIT

install -m 0700 -d "$stage/data"

# Copy atomically-replaced JSON/CSV/state first. Live SQLite files and their
# WAL/SHM sidecars are handled by the online backup API below.
rsync -a --delete \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='*.tmp' --exclude='*.download' \
  "$DATA_ROOT/" "$stage/data/"

while IFS= read -r -d '' source_db; do
  relative=${source_db#"$DATA_ROOT"/}
  target_db="$stage/data/$relative"
  install -m 0700 -d "$(dirname -- "$target_db")"
  sqlite3 "$source_db" ".timeout 60000" ".backup '$target_db'"
  check=$(sqlite3 "$target_db" 'PRAGMA quick_check;')
  [[ "$check" == "ok" ]] || {
    echo "SQLite quick_check failed for $source_db: $check" >&2
    exit 1
  }
done < <(find "$DATA_ROOT" -type f -name '*.db' -print0)

cat >"$stage/manifest.txt" <<EOF
created_at=$timestamp
hostname=$(hostname --fqdn 2>/dev/null || hostname)
data_root=$DATA_ROOT
EOF
find "$stage/data" -type f -printf '%P\t%s\n' | LC_ALL=C sort >>"$stage/manifest.txt"

if [[ -f "$BACKUP_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$BACKUP_ENV"
  set +a
fi

if [[ -n ${RESTIC_REPOSITORY:-} ]]; then
  : "${RESTIC_PASSWORD_FILE:?Set RESTIC_PASSWORD_FILE in $BACKUP_ENV}"
  restic backup "$stage" --tag rieltor-pokupka
  restic forget --tag rieltor-pokupka \
    --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
else
  echo "RESTIC_REPOSITORY is not set; creating local backup only." >&2
fi

tar --zstd -C "$stage" -cf "$archive_tmp" .
(cd -- "$BACKUP_ROOT" && sha256sum "$(basename -- "$archive_tmp")") \
  >"$archive_tmp.sha256"
mv -- "$archive_tmp" "$archive"
sed "s|$(basename -- "$archive_tmp")|$(basename -- "$archive")|" \
  "$archive_tmp.sha256" >"$archive.sha256"
rm -f -- "$archive_tmp.sha256"

find "$BACKUP_ROOT" -maxdepth 1 -type f \
  \( -name 'rieltor-*.tar.zst' -o -name 'rieltor-*.tar.zst.sha256' \) \
  -mtime +8 -delete

echo "Backup completed: $archive"
