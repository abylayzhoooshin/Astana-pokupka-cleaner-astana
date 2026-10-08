# OVH production deployment

Target: a standard (not Local Zone) OVH VPS-3 with Ubuntu 24.04. Collector,
Cleaner and the future purchase Main remain separate containers and separate
SQLite owners. They communicate through the private Docker network; no
container opens another service's database.

Render configuration remains in the repositories only as a rollback path.

## Safety defaults

The first start is intentionally non-destructive and non-paid:

- `COLLECTOR_PAUSED=1`: Collector serves migrated data but does not scrape;
- `MIN_BATCH_SIZE=999999`: Cleaner fetches/diffs without creating OpenAI jobs;
- Main is behind the `purchase-main` profile and is not started;
- ports 8001/8002 bind to `127.0.0.1`, not the public interface;
- entity matching remains shadow-only and manual writes remain disabled.

Do not change these values until migrated SQLite files pass `quick_check`, row
counts match Render, and both APIs return the expected versions.

## Server layout

```text
/opt/rieltor/
  repos/collector/       collector git checkout
  repos/cleaner/         cleaner git checkout
  repos/main/            purchase Main checkout, later
  deploy/ovh/            this directory

/srv/rieltor/
  collector/             Master DB, immutable baselines, scraper state
  cleaner/               verdict/entity DB and clean CSV artifacts
  main/                  notification registry, cursor, baseline cache

/srv/rieltor-backups/    short-lived local backup archives
```

## 1. Create the host

Choose Ubuntu 24.04 on a regular European OVH datacenter VPS, add an SSH public
key, then log in as root once. Do not expose 8001 or 8002 in the OVH firewall.

Run `bash scripts/bootstrap_host.sh`. It installs Docker, SQLite, restic and the
backup tools, creates the fixed directories, enables Docker on boot and creates
the `deploy` administrator. If root has an OVH-provisioned SSH key, that public
key is copied to `deploy`. The script does not disable root SSH or enable UFW,
so it cannot lock out the active session.

After confirming key-based SSH in a second terminal:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable
```

If SSH uses another port, allow that port before enabling UFW. Disable root
password login only after the non-root admin key has been tested.

## 2. Install repositories and environment

Clone the three repositories into `/opt/rieltor/repos`. Copy this deployment
directory to `/opt/rieltor/deploy/ovh`, then:

```bash
cd /opt/rieltor/deploy/ovh
cp stack.env.example .env
chmod 600 .env
openssl rand -base64 48
```

Generate separate Collector and Cleaner API keys and edit `.env`. Store the
OpenAI and Telegram secrets in a password manager as well; `.env` is not a
backup of secrets.

Run:

```bash
bash scripts/preflight.sh
docker compose build collector cleaner
```

Do not enable the `purchase-main` profile: the currently available local Main
is calibrated for rental listings and is not a valid sales consumer.

## 3. Migrate data

Follow `MIGRATION_RUNBOOK.md`. In short: keep Render serving, pause writes,
create consistent SQLite backups, transfer every state file, verify hashes and
row counts, then start OVH in paused/dry-run mode.

```bash
docker compose up -d collector cleaner
docker compose ps
curl -fsS http://127.0.0.1:8001/health
curl -fsS http://127.0.0.1:8002/health
```

Remote access is through an SSH tunnel, not a public application port:

```bash
ssh -L 8001:127.0.0.1:8001 -L 8002:127.0.0.1:8002 deploy@SERVER_IP
```

## 4. Turn production on in stages

1. Keep `COLLECTOR_PAUSED=1`, `MIN_BATCH_SIZE=999999`; validate APIs and data.
2. Set `COLLECTOR_PAUSED=0`, recreate only Collector, observe one fast pass and
   one complete nightly scan.
3. Run the paid Luna prompt regression outside production.
4. Set `MIN_BATCH_SIZE=1`, recreate Cleaner and observe batch submission and
   ingestion.
5. Adapt and audit the purchase Main. Only then start it with
   `docker compose --profile purchase-main up -d main`.
6. Keep Render intact but stopped/read-only until several full cycles pass.

Environment changes require container recreation:

```bash
docker compose up -d --force-recreate collector
docker compose up -d --force-recreate cleaner
```

## 5. Backups and recovery

`scripts/backup.sh` uses SQLite's online backup API, checks every copied DB,
copies non-database state and creates a compressed local archive. If restic is
configured, the uncompressed staging tree is encrypted and uploaded before it
is removed.

The restricted Aeza-side SFTP setup is documented in
`AEZA_BACKUP_TARGET.md`.

Create `/etc/rieltor/backup.env` with mode `600`:

```bash
RESTIC_REPOSITORY=sftp:rieltorbackup@AEZA_IP:/data/rieltor-pokupka
RESTIC_PASSWORD_FILE=/etc/rieltor/restic-password
```

Use a dedicated restricted SSH key for the backup account. Initialize once:

```bash
set -a
source /etc/rieltor/backup.env
set +a
restic --repository "$RESTIC_REPOSITORY" \
  --password-file "$RESTIC_PASSWORD_FILE" init
```

If `DATA_ROOT` or `BACKUP_ROOT` is changed from the documented `/srv` paths,
create the corresponding `.rieltor-data-root` or `.rieltor-backup-root` marker
manually. Backup retention and restore refuse to run without these markers so
a typo cannot turn cleanup or replacement into a broad filesystem operation.

Install the timers:

```bash
bash scripts/install_systemd_units.sh
systemctl list-timers 'rieltor-*'
```

The policy is 7 daily, 4 weekly and 6 monthly remote snapshots. Local archives
older than 8 days are removed. Test `restore.sh` before Render is deleted.

The built-in OVH daily backup is an additional recovery layer, not the only
copy. The Aeza/restic repository protects against accidental deletion,
corruption of the VPS and loss of the OVH account.

## Operations

```bash
docker compose ps
docker compose logs --tail=200 collector
docker compose logs --tail=200 cleaner
bash scripts/healthcheck.sh
bash scripts/backup.sh
```

Never run two independent active Collectors against separate copies of Master
DB. During cutover only one host may scrape; otherwise states and event cursors
diverge.
