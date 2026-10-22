# Astana purchase Cleaner on Render

Use this repository's `render.yaml` as a Blueprint. It creates a Frankfurt web
service with its own persistent disk; it never reads the Collector's SQLite
file directly. Create the service only after the Collector reports
`candidate_ready: true` at `/health`.

Set `COLLECTOR_API_KEY` to the Collector's `BASELINE_API_KEY` and provide
`OPENAI_API_KEY` in Render Environment. The public `COLLECTOR_URL` is already
in the Blueprint. Do not copy local `data/` or `env.ps1` into Git.

The initial Blueprint sets `MIN_BATCH_SIZE=999999`. This deliberately ingests
and categorizes source records but does not submit historical AI Batch jobs.
After checking counts, duplicate candidates, and the estimated Batch cost,
lower this value and set a measured `PILOT_LIMIT` before spending money.
Do not set `PILOT_LIMIT=0` with an active Batch threshold until that decision
has been made; zero means unlimited.

The Blueprint generates `CLEANER_API_KEY`. Copy that key into purchase Main as
`RIELTOR_CLEANER_API_KEY`. After Main is live, set `MAINRIELTOR_URL` to its
public URL and `MAINRIELTOR_INTERIOR_API_KEY` to Main's
`INTERIOR_EXCHANGE_API_KEY`, then enable `MAINRIELTOR_PHOTO_SYNC_ENABLED=1`.
The initial Blueprint leaves photo sync disabled so it cannot race a Main
service that has not yet been created.
