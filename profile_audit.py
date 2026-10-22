"""Read-only coverage audit of sales text claims on a Collector master DB."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3

import sale_listing_profile


def audit(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    counts = Counter()
    examples = defaultdict(list)
    total = tagged = contradictions = unknown_finish_with_photos = 0
    try:
        for source in conn.execute(
                "SELECT id,title,full_description,rent_renovation,furniture,photo_count "
                "FROM listings"):
            total += 1
            row = dict(source)
            signals = sale_listing_profile.classify(row)["signals"]
            codes = {item["code"] for item in signals}
            tagged += bool(codes)
            counts.update(codes)
            for code in codes:
                if len(examples[code]) < 3:
                    examples[code].append(row["id"])
            contradictory_finish = (
                {"finish_rough", "finish_fresh"}.issubset(codes)
                or {"finish_needs_repair", "finish_fresh"}.issubset(codes)
            )
            contradictions += contradictory_finish
            if (not any(code.startswith("finish_") for code in codes)
                    and int(row.get("photo_count") or 0) > 0):
                unknown_finish_with_photos += 1
    finally:
        conn.close()
    return {"rows": total, "tagged_rows": tagged,
            "contradictory_finish_claims": contradictions,
            "unknown_finish_with_photos": unknown_finish_with_photos,
            "counts": dict(counts.most_common()), "sample_ids": dict(examples)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master_db")
    args = parser.parse_args()
    print(json.dumps(audit(args.master_db), ensure_ascii=False, indent=2))
