# Aeza as a restricted restic target

The existing Aeza SWE-2 is suitable as an encrypted off-site backup target. It
must not run a second active Collector against a divergent Master DB.

Run these steps on Aeza from an existing administrator session. Keep that
session open until `sshd -t` and a second login test succeed.

## 1. Create a storage-only account

```bash
sudo useradd --home-dir /data --shell /usr/sbin/nologin rieltorbackup
sudo install -o root -g root -m 0755 -d /srv/restic-sftp
sudo install -o rieltorbackup -g rieltorbackup -m 0700 -d /srv/restic-sftp/data
sudo install -o root -g root -m 0755 -d /etc/ssh/authorized_keys
sudo install -o root -g root -m 0600 /dev/null /etc/ssh/authorized_keys/rieltorbackup
sudo usermod --password "$(openssl passwd -6 "$(openssl rand -base64 48)")" rieltorbackup
```

On OVH generate a dedicated key with no other purpose:

```bash
sudo ssh-keygen -t ed25519 -f /etc/rieltor/aeza-backup-key -N ''
```

Append only the resulting `.pub` line to
`/etc/ssh/authorized_keys/rieltorbackup` on Aeza.

## 2. Restrict the account to SFTP

Create `/etc/ssh/sshd_config.d/rieltor-backup.conf` on Aeza:

```text
Match User rieltorbackup
    ChrootDirectory /srv/restic-sftp
    ForceCommand internal-sftp
    AuthorizedKeysFile /etc/ssh/authorized_keys/%u
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowAgentForwarding no
    AllowTcpForwarding no
    X11Forwarding no
    PermitTunnel no
```

Validate before reload:

```bash
sudo sshd -t
sudo systemctl reload ssh
```

If validation fails, do not reload SSH. Correct the configuration from the
still-open administrator session.

## 3. Configure OVH

`/etc/rieltor/backup.env`:

```bash
RESTIC_REPOSITORY=sftp:rieltorbackup@AEZA_IP:/data/rieltor-pokupka
RESTIC_PASSWORD_FILE=/etc/rieltor/restic-password
RESTIC_SFTP_COMMAND="ssh -i /etc/rieltor/aeza-backup-key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes rieltorbackup@AEZA_IP -s sftp"
```

Add the Aeza host key to root's `known_hosts` interactively before the first
automated run. Compare its fingerprint in the Aeza control panel/console; do
not use `StrictHostKeyChecking=no`.

Create a long random restic password, store a second copy in a password
manager, set file mode `600`, source `backup.env`, then run `restic init`.
Without that password the encrypted off-site backups cannot be restored.

Finally run `scripts/backup.sh`, list `restic snapshots`, restore one snapshot
to a temporary directory and run `PRAGMA quick_check` on every restored DB.
