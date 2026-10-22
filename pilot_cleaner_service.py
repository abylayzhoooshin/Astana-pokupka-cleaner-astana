"""Pilot-only Cleaner API with photo pull, without the full Collector/Batch cycle."""

import asyncio
import logging

import cleaner_db
import main_photo_sync
from verdicts_api import app

log = logging.getLogger("sales_cleaner.pilot")
_task = None


def _pull_once():
    with cleaner_db.connect() as conn:
        return main_photo_sync.sync(conn)


async def _photo_loop():
    while True:
        try:
            result = await asyncio.to_thread(_pull_once)
            if result.get("received") or result.get("acknowledged"):
                log.info("photo sync received=%s acknowledged=%s",
                         result.get("received"), result.get("acknowledged"))
        except (main_photo_sync.PhotoSyncError, ValueError) as exc:
            log.warning("photo sync retry: %s", type(exc).__name__)
        await asyncio.sleep(15)


@app.on_event("startup")
async def start_photo_loop():
    global _task
    _task = asyncio.create_task(_photo_loop())


@app.on_event("shutdown")
async def stop_photo_loop():
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
