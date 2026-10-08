#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg git jq openssl restic rsync sqlite3 sudo ufw zstd

install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi

source /etc/os-release
cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update
apt-get install -y --no-install-recommends \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

ADMIN_USER=${ADMIN_USER:-deploy}
[[ "$ADMIN_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
  echo "Invalid ADMIN_USER: $ADMIN_USER" >&2
  exit 1
}
if ! id "$ADMIN_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$ADMIN_USER"
fi
usermod -aG sudo,docker "$ADMIN_USER"
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$ADMIN_USER" \
  >"/etc/sudoers.d/90-$ADMIN_USER"
chmod 0440 "/etc/sudoers.d/90-$ADMIN_USER"

if [[ -f /root/.ssh/authorized_keys ]]; then
  install -o "$ADMIN_USER" -g "$ADMIN_USER" -m 0700 \
    -d "/home/$ADMIN_USER/.ssh"
  install -o "$ADMIN_USER" -g "$ADMIN_USER" -m 0600 \
    /root/.ssh/authorized_keys "/home/$ADMIN_USER/.ssh/authorized_keys"
else
  echo "WARNING: /root/.ssh/authorized_keys is absent; add an admin SSH key manually." >&2
fi

install -m 0750 -d \
  /opt/rieltor/repos \
  /opt/rieltor/deploy \
  /srv/rieltor/collector \
  /srv/rieltor/cleaner \
  /srv/rieltor/main \
  /srv/rieltor-backups \
  /etc/rieltor
touch /srv/rieltor/.rieltor-data-root
touch /srv/rieltor-backups/.rieltor-backup-root
chmod 0600 /srv/rieltor/.rieltor-data-root /srv/rieltor-backups/.rieltor-backup-root

# The purchase Main image runs as uid/gid 10001. Collector and Cleaner still
# run as container root, which can also write these host-owned directories.
chown 10001:10001 /srv/rieltor/main
chown -R "$ADMIN_USER:$ADMIN_USER" /opt/rieltor

echo "Host packages and directories are ready."
echo "Admin user: $ADMIN_USER (re-login is required for docker group membership)."
echo "UFW and SSH hardening were not enabled automatically. Follow README.md."
