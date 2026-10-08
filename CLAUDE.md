# Project instructions

## Service

`rieltor-sales-cleaner-astana` is the second service in the Astana apartment
sales pipeline:

```text
pokupka-rieltor-collector -> sales-cleaner -> valuation consumer
```

It decides whether a Collector row contains a comparable asking price for a
whole apartment. It does not detect fraud and must not duplicate Collector's
city/sanity/seller/dedup filters.

The current conservative policy rejects only explicit partial property,
non-apartments, non-sale listings, non-full prices, legal restrictions,
auctions/distress and non-market deal terms. Rough finish, no furniture,
urgency, ordinary mortgage, assignment and construction-in-progress are not
rejection reasons until a labelled sales dataset supports a policy change.

## Commands

```powershell
pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest discover -v
.\run.ps1
```

`prompt_check.py` uses the real configured model and costs money. Run it after
every `SYSTEM_PROMPT` change before deployment. Add a case to
`prompt_cases.json` for every discovered miss.

Set `MIN_BATCH_SIZE=999999` for an integration dry-run that fetches and diffs
Collector data without submitting an OpenAI batch.

Run only the API, without triggering a cycle:

```powershell
.venv\Scripts\python.exe -m uvicorn verdicts_api:app --port 8002
```

## Cycle invariants

The full cycle fetches a consistent Collector version, ingests old batches,
applies rules, diffs by text content, submits the remainder and publishes a
stored snapshot. The ingest tick only checks pending batches and reuses the
last full fetch.

- Keep `in_flight_ids()` exclusion or pending rows will be paid twice.
- A failed LLM result stores an empty hash until `MAX_LLM_ATTEMPTS`; after the
  limit it stores the real hash with `usable=NULL` (unknown, not bad).
- A completed OpenAI batch may have no output file. Mark it failed and requeue.
- Partial batch submission must preserve already recorded batches and leave
  unsent rows for the next cycle.
- Rule verdicts are retroactive. If an old rule stops matching, invalidate its
  hash so the row returns to AI.
- Prompt changes are not retroactive. Use a new `CLEANER_RELABEL_GEN` value.
  Never publish a partial snapshot while relabel is in progress.
- Both verdict producers return `usable`, `reason_code`, `confidence`,
  `reason`, `source`.

## Content identity

Text `content_hash` must cover exactly the listing fields used by text rules or
the model:

```text
title, full_description, rent_renovation, priv_dorm, square_m2, rooms
```

Do not add price, status, technical timestamps, floor or photos unless the
classifier actually starts using them. Price changes must not trigger paid
text relabelling. Validate `photo_urls`, `photo_count` and `photo_set_hash`
together; an inconsistent Collector snapshot must not replace the last clean
snapshot. Photo evaluation has its own identity:
`listing_id + photo_set_hash + evaluator_version`.

## Collector contract

Use `/baseline/table` with `X-API-Key`. Every page includes `version` and
`total`; restart pagination if `version` changes. The Collector baseline
already excludes owners/developers under its current comparison policy and
contains `missing` historical rows. Do not remove a row because
`status=missing`.

The sales index is currently uncalibrated. `measured=false` is expected. Do
not reuse a rental index or invent historical price adjustment.

## Published snapshot

`/baseline/clean` and `.csv` read `clean_baseline` from local SQLite and never
proxy Collector. Publish only rows with a verdict for the current hash and
`usable != 0`. `usable=NULL` is published after retries are exhausted.

Never replace a previous snapshot with an empty result. Before the first
non-empty build the endpoint returns 503. `built_at` is the snapshot cursor;
clients restart pagination when it changes.

Verdict pagination must order by `(processed_at, id)` because timestamps have
second precision. CSV is built atomically on persistent disk when a snapshot
is published (a legacy snapshot is built once on first request), then served as
a file. Never stream a thread-bound live SQLite iterator.

## Physical-apartment entities

`listing_id` is an immutable Krisha advertisement episode; `entity_id` is the
Cleaner identity of a physical apartment. Never delete source listing IDs.
One entity may have any number of simultaneously active members.

Entity matching is shadow-only until a labelled production evaluation accepts
an auto-link threshold. Metadata only creates candidates. Coordinates are
positive evidence and never a hard conflict. Photo downloading and vision stay
outside Cleaner, next to the future purchase evaluation flow; Cleaner stores
only fully versioned evidence submitted through its API.

Canonical selection first requires a verdict for the member's current text
hash. Among ready active members publish the complete row with the minimum
valid price, keeping the current canonical on a tie. Never combine price,
description or photos from different members. Missing members remain in entity
history and price/lifecycle analysis.

Merge, split and manual overrides must stay transactional and idempotent.
Preserve membership, canonical and lineage history. A merge may use an alias;
a split must use lineage because one parent can produce several children.

## Deployment and secrets

The primary deployment target is now a standard European OVH VPS running
Ubuntu 24.04 and Docker Compose. The authoritative deployment files are under
`deploy/ovh/`. Collector and Cleaner use separate bind-mounted data
directories and communicate over the private Compose network. Their HTTP ports
bind to host loopback only. Never let one service open another service's
SQLite file directly.

The first OVH start must keep `COLLECTOR_PAUSED=1` and
`MIN_BATCH_SIZE=999999`; activate scraping and paid OpenAI submission only
after migrated databases pass `PRAGMA quick_check` and counts/versions match
Render. The purchase Main profile stays disabled while the only available Main
checkout is rental-calibrated.

`render.yaml` is retained solely as a rollback reference during migration. Do
not delete Render disks until OVH has completed several full cycles and a
restore from the external backup has been tested.

`env.ps1`, `run.ps1` and `data/` are local and gitignored. Never put live keys
in tracked files. `deploy/ovh/.env`, restic credentials and backup SSH keys are
server-only secrets and must have mode 600.
