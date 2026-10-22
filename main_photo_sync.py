"""Pull completed photo evaluations from purchase Main without coupling databases.

ACK follows a committed local write. If the network fails before ACK, the next
cycle sees the same result and the idempotent upsert safely repeats it.
"""

import os
from urllib.parse import urlsplit

import requests

import main_assessments


class PhotoSyncError(RuntimeError):
    pass


def configured():
    if os.environ.get("MAINRIELTOR_PHOTO_SYNC_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    url = os.environ.get("MAINRIELTOR_URL", "").strip().rstrip("/")
    key = os.environ.get("MAINRIELTOR_INTERIOR_API_KEY", "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.query or parsed.fragment or not key:
        raise PhotoSyncError("invalid Main photo exchange configuration")
    return url, key


def _request(method, url, key, **kwargs):
    try:
        response = requests.request(
            method, url, headers={"X-API-Key": key},
            timeout=(10, 60), allow_redirects=False, **kwargs,
        )
        if response.status_code != 200:
            raise PhotoSyncError(f"Main photo exchange HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("response is not an object")
        return data
    except PhotoSyncError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise PhotoSyncError(f"Main photo exchange failed: {type(exc).__name__}") from None


def sync(conn, *, max_pages=20):
    settings = configured()
    if settings is None:
        return {"received": 0, "acknowledged": 0, "disabled": True}
    url, key = settings
    after, received, acknowledged = 0, 0, 0
    for _ in range(max_pages):
        page = _request("GET", url + "/interior/evaluations", key,
                        params={"after_id": after, "limit": 200})
        items = page.get("results")
        if not isinstance(items, list) or len(items) > 200 or page.get("count") != len(items):
            raise PhotoSyncError("invalid Main photo page")
        if not items:
            return {"received": received, "acknowledged": acknowledged}
        ids = []
        for item in items:
            if not isinstance(item, dict) or type(item.get("evaluation_id")) is not int or item["evaluation_id"] <= after:
                raise PhotoSyncError("invalid Main photo evaluation id")
            ids.append(item["evaluation_id"])
            main_assessments.save(conn, {
                "listing_id": item.get("listing_id"),
                "photo": {
                    "photo_set_hash": item.get("photo_set_hash"),
                    "evaluator_version": item.get("evaluator_version"),
                    "result": {"status": item.get("status"), "score": item.get("score"),
                               "coverage": item.get("coverage"), "report": item.get("report")},
                },
            })
        if ids != sorted(set(ids)):
            raise PhotoSyncError("unordered Main photo evaluation ids")
        conn.commit()
        received += len(ids)
        ack = _request("POST", url + "/interior/evaluations/ack", key,
                       json={"evaluation_ids": ids, "baseline_version": "purchase-cleaner-stored"})
        if ack.get("requested") != len(ids) or ack.get("missing") or ack.get("not_ready"):
            raise PhotoSyncError("incomplete Main photo ACK")
        if (ack.get("acknowledged", 0) + ack.get("already_acknowledged", 0)) != len(ids):
            raise PhotoSyncError("Main photo ACK count mismatch")
        acknowledged += len(ids)
        if not page.get("has_more"):
            return {"received": received, "acknowledged": acknowledged}
        if page.get("next_after_id") != ids[-1]:
            raise PhotoSyncError("invalid Main photo cursor")
        after = ids[-1]
    # A large intentional backfill is bounded per tick; acknowledged pages
    # disappear from Main's outbox, so the next tick continues safely.
    return {"received": received, "acknowledged": acknowledged, "truncated": True}
