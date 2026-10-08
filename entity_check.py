"""Read-only entity-resolution analysis against the current Collector snapshot.

The command never submits OpenAI batches, mutates Collector or touches the
configured Cleaner database. All entity state is built in an in-memory SQLite
database and discarded on exit.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter

import collector_client
import entity_matcher
import entity_store


def main():
    started = time.time()
    version, rows = collector_client.fetch_all_rows()
    fetched_at = time.time()

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    entity_store.create_schema(conn)
    observed = entity_store.observe_listings(
        conn, rows, version, collector_client.content_hash,
    )
    candidates = entity_matcher.refresh_shadow_candidates(conn, rows, version)
    conn.commit()

    priority = Counter()
    reason_counts = Counter()
    active_active = mixed_status = 0
    top = []
    for candidate in conn.execute(
        """SELECT c.*,a.status AS low_status,b.status AS high_status,
                  a.current_price AS low_price,b.current_price AS high_price
           FROM entity_match_candidates c
           JOIN listing_entity_state a ON a.listing_id=c.listing_id_low
           JOIN listing_entity_state b ON b.listing_id=c.listing_id_high
           WHERE c.is_current=1
           ORDER BY c.priority DESC,c.listing_id_low,c.listing_id_high"""
    ):
        reasons = json.loads(candidate["reasons_json"])
        reason_counts.update(reasons)
        priority[str(candidate["priority"])] += 1
        if candidate["low_status"] == candidate["high_status"] == "active":
            active_active += 1
        if candidate["low_status"] != candidate["high_status"]:
            mixed_status += 1
        if len(top) < 20:
            top.append({
                "listing_id_low": candidate["listing_id_low"],
                "listing_id_high": candidate["listing_id_high"],
                "low_status": candidate["low_status"],
                "high_status": candidate["high_status"],
                "low_price": candidate["low_price"],
                "high_price": candidate["high_price"],
                "priority": candidate["priority"],
                "reasons": reasons,
            })

    output = {
        "collector_version": version,
        "rows": len(rows),
        "active": sum(row.get("status") == "active" for row in rows),
        "missing": sum(row.get("status") == "missing" for row in rows),
        "with_price_drops": sum(
            int(row.get("price_drop_count") or 0) > 0 for row in rows
        ),
        "with_reactivations": sum(
            int(row.get("reactivation_count") or 0) > 0 for row in rows
        ),
        "observation": observed,
        "candidate_summary": candidates,
        "candidate_active_active": active_active,
        "candidate_mixed_status": mixed_status,
        "candidate_reasons": dict(sorted(reason_counts.items())),
        "candidate_priorities": dict(sorted(priority.items(), reverse=True)),
        "top_candidates": top,
        "fetch_seconds": round(fetched_at - started, 2),
        "analysis_seconds": round(time.time() - fetched_at, 2),
        "auto_merge_enabled": False,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
