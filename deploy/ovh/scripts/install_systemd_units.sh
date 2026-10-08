#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STACK_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)

chmod 0755 "$STACK_DIR"/scripts/*.sh

install -m 0644 "$STACK_DIR/systemd/rieltor-backup.service" /etc/systemd/system/
install -m 0644 "$STACK_DIR/systemd/rieltor-backup.timer" /etc/systemd/system/
install -m 0644 "$STACK_DIR/systemd/rieltor-health.service" /etc/systemd/system/
install -m 0644 "$STACK_DIR/systemd/rieltor-health.timer" /etc/systemd/system/

sed -i "s|__STACK_DIR__|$STACK_DIR|g" \
  /etc/systemd/system/rieltor-backup.service \
  /etc/systemd/system/rieltor-health.service

systemctl daemon-reload
systemctl enable --now rieltor-backup.timer rieltor-health.timer
echo "Backup and health timers installed."
