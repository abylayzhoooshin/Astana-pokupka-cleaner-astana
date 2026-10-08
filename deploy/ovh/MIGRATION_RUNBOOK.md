# Render to OVH migration runbook

This runbook deliberately separates data copy from production activation. A
failed verification must leave Render data untouched and allow an immediate
rollback.

## Inventory that must be copied

Collector: the entire mounted `DATA_DIR`, including:

- `krisha_astana.db` plus any WAL/SHM present while the service is running;
- `baseline_versions/` and `latest.json`;
- orchestrator, list/detail progress and known-id state;
- first-cycle marker, cookie jar and other files rooted through `paths.py`.

Cleaner: the entire mounted `DATA_DIR`, including:

- `sales_cleaner_astana.db`;
- stored clean CSV artifacts;
- pending OpenAI batch metadata inside SQLite.

Main is migrated only after a sales-safe purchase Main exists. Preserve its
cursor, notification registry and logs; never seed a purchase deployment from
the existing rental state.

## Phase A: prepare OVH without writes

1. Install the stack with `COLLECTOR_PAUSED=1` and
   `MIN_BATCH_SIZE=999999`.
2. Build images but do not start containers.
3. Create empty `/srv/rieltor/{collector,cleaner,main}` directories.
4. Record Render row counts, current baseline version, file sizes and SHA-256
   hashes of exported backup artifacts.

## Phase B: consistent export from Render

1. Prevent a new Collector scan from starting.
2. Wait for or safely stop the current scan.
3. Stop Cleaner's full-cycle process after any already-completed OpenAI batch
   has been ingested. Do not cancel remote pending batches.
4. Use `sqlite3 source.db ".backup '/tmp/name.db'"`; do not copy a live WAL DB
   as a lone `.db` file.
5. Copy the remaining non-DB state and immutable baseline directory.
6. Hash the resulting archives before transfer.

Do not delete the Render disks or services.

## Phase C: import and verification on OVH

Place files in their final directories and run:

```bash
find /srv/rieltor -type f -name '*.db' -print0 | while IFS= read -r -d '' db; do
  printf '%s: ' "$db"
  sqlite3 "$db" 'PRAGMA quick_check;'
done
```

Then compare at minimum:

- Collector total, active and missing rows;
- `MAX(event_id)` and event count;
- current immutable baseline version and total;
- Cleaner verdict, pending batch, relabel and entity counts;
- snapshot `built_at` and row count;
- hashes of immutable baseline files.

Only `ok` from every `quick_check` is acceptable.

## Phase D: safe start

Start Collector and Cleaner with safe defaults. Confirm that Collector returns
the migrated version and Cleaner performs a no-spend fetch/diff. Pending remote
OpenAI batches may be polled and ingested; `MIN_BATCH_SIZE=999999` only blocks
new submissions.

After verification, enable one writer at a time:

1. stop/disable the Render Collector;
2. set OVH `COLLECTOR_PAUSED=0` and recreate Collector;
3. observe one full scan and verify missing counts do not collapse;
4. after prompt regression, set `MIN_BATCH_SIZE=1` and recreate Cleaner.

## Rollback

If OVH fails before it has accepted unique new observations, stop OVH and
resume Render.

If OVH has already collected new data, do not simply restart the old Render
copy: that would fork Master DB history. First export the newer OVH state back
to Render or explicitly accept the lost interval and record it as a history
gap.
