"""Read-only sizing report for the entity photo-analysis queue.

The script never downloads images and opens Cleaner SQLite with ``mode=ro``.
It is intended to be rerun after each materially different candidate snapshot
before selecting a paid or CPU-heavy photo pilot.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter

import entity_matcher


def _raw_photo_urls(payload):
    value = payload.get("photo_urls") or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    return [item for item in value if isinstance(item, str) and item]


def _queue_size(rows, snapshots, predicate, average_source_bytes):
    selected = [item for item in rows if predicate(item)]
    listing_ids = {
        listing_id
        for item in selected
        for listing_id in (item["listing_id_low"], item["listing_id_high"])
    }
    photo_count = sum(
        int(snapshots[listing_id].get("photo_count") or 0)
        for listing_id in listing_ids
    )
    capped_five = sum(
        min(5, int(snapshots[listing_id].get("photo_count") or 0))
        for listing_id in listing_ids
    )
    return {
        "pairs": len(selected),
        "unique_listings": len(listing_ids),
        "all_source_photos": photo_count,
        "first_five_photos": capped_five,
        "estimated_all_source_gib": round(
            photo_count * average_source_bytes / 2**30, 2
        ) if average_source_bytes else None,
        "estimated_first_five_gib": round(
            capped_five * average_source_bytes / 2**30, 2
        ) if average_source_bytes else None,
    }


def build_report(db_path, average_source_bytes=0):
    absolute = os.path.abspath(db_path)
    conn = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        snapshots = {
            row["listing_id"]: json.loads(row["payload_json"])
            for row in conn.execute(
                "SELECT listing_id,payload_json FROM listing_identity_snapshots"
            )
        }
        candidates = list(conn.execute(
            "SELECT listing_id_low,listing_id_high,status,reasons_json "
            "FROM entity_match_candidates WHERE is_current=1"
        ))
        generator_versions = dict(Counter(
            row[0] for row in conn.execute(
                "SELECT generator_version FROM entity_match_candidates "
                "WHERE is_current=1"
            )
        ))
    finally:
        conn.close()

    waiting = [
        item for item in candidates
        if item["status"] == "awaiting_photo_analysis"
    ]

    def facts(item):
        first = snapshots[item["listing_id_low"]]
        second = snapshots[item["listing_id_high"]]
        return (
            first,
            second,
            len(json.loads(item["reasons_json"])),
        )

    raw_memberships = 0
    usable_memberships = 0
    usable_assets = Counter()
    for payload in snapshots.values():
        raw_memberships += len(_raw_photo_urls(payload))
        urls = entity_matcher.usable_photo_urls(payload)
        usable_memberships += len(urls)
        usable_assets.update(urls)

    degree = Counter()
    for item in waiting:
        degree[item["listing_id_low"]] += 1
        degree[item["listing_id_high"]] += 1

    queues = {
        "all_waiting": lambda item: True,
        "both_active": lambda item: all(
            payload.get("status") == "active" for payload in facts(item)[:2]
        ),
        "both_active_3plus_both_eligible": lambda item: (
            facts(item)[2] >= 3
            and all(payload.get("status") == "active" for payload in facts(item)[:2])
            and all(bool(payload.get("baseline_eligible")) for payload in facts(item)[:2])
        ),
        "both_active_2plus_both_eligible": lambda item: (
            facts(item)[2] >= 2
            and all(payload.get("status") == "active" for payload in facts(item)[:2])
            and all(bool(payload.get("baseline_eligible")) for payload in facts(item)[:2])
        ),
        "active_missing_3plus_both_eligible": lambda item: (
            facts(item)[2] >= 3
            and sum(payload.get("status") == "active" for payload in facts(item)[:2]) == 1
            and all(bool(payload.get("baseline_eligible")) for payload in facts(item)[:2])
        ),
    }
    return {
        "database": absolute,
        "current_candidates": len(candidates),
        "candidate_statuses": dict(Counter(item["status"] for item in candidates)),
        "candidate_generator_versions": generator_versions,
        "photo_assets": {
            "raw_url_memberships": raw_memberships,
            "usable_url_memberships": usable_memberships,
            "ignored_or_duplicate_memberships": raw_memberships - usable_memberships,
            "unique_usable_urls": len(usable_assets),
            "max_listings_per_exact_usable_url": max(usable_assets.values(), default=0),
        },
        "waiting_graph": {
            "listings": len(degree),
            "max_pairs_for_one_listing": max(degree.values(), default=0),
            "listings_with_at_least_10_pairs": sum(value >= 10 for value in degree.values()),
        },
        "queues": {
            name: _queue_size(waiting, snapshots, predicate, average_source_bytes)
            for name, predicate in queues.items()
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=os.environ.get("CLEANER_DB", "sales_cleaner_astana.db"),
    )
    parser.add_argument(
        "--average-source-bytes",
        type=int,
        default=0,
        help="Optional measured mean image size used only for transfer estimates.",
    )
    args = parser.parse_args()
    print(json.dumps(
        build_report(args.db, args.average_source_bytes),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
