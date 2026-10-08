"""Metadata candidate generation for future photo matching in Main.

Cleaner owns entities and the durable review queue, but it does not download or
score photographs. This module only narrows the full listing set to plausible
pairs. Every result remains shadow-only: no function merges entities.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone


CANDIDATE_GENERATOR_VERSION = "sales-metadata-candidates-v3"
IDENTITY_FIELDS = (
    "rooms", "square_m2", "floor", "floor_total", "street", "house_num",
    "complex_id", "complex_alias", "complex_name", "latitude", "longitude",
    "owner_name", "photo_set_hash",
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def _norm(value):
    return " ".join(str(value or "").strip().casefold().split())


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def identity_hash(row):
    payload = {field: row.get(field) for field in IDENTITY_FIELDS}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def coordinate_distance_meters(first, second):
    lat1, lon1 = _number(first.get("latitude")), _number(first.get("longitude"))
    lat2, lon2 = _number(second.get("latitude")), _number(second.get("longitude"))
    if None in (lat1, lon1, lat2, lon2):
        return None
    radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    value = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * radius * math.asin(min(1, math.sqrt(value)))


def hard_conflicts(first, second):
    """Strong metadata contradictions; coordinates deliberately never block."""
    conflicts = []
    rooms_a, rooms_b = _number(first.get("rooms")), _number(second.get("rooms"))
    if rooms_a is not None and rooms_b is not None and rooms_a != rooms_b:
        conflicts.append("rooms")
    area_a = _number(first.get("square_m2"))
    area_b = _number(second.get("square_m2"))
    if area_a and area_b and abs(area_a - area_b) / max(area_a, area_b) > 0.10:
        conflicts.append("area")
    floor_a, floor_b = _number(first.get("floor")), _number(second.get("floor"))
    if floor_a is not None and floor_b is not None and floor_a != floor_b:
        conflicts.append("floor")
    street_a, street_b = _norm(first.get("street")), _norm(second.get("street"))
    house_a, house_b = _norm(first.get("house_num")), _norm(second.get("house_num"))
    if street_a and street_b and house_a and house_b:
        if (street_a, house_a) != (street_b, house_b):
            conflicts.append("address")
    return sorted(set(conflicts))


def positive_signals(first, second):
    signals = []
    photo_set_a = str(first.get("photo_set_hash") or "")
    photo_set_b = str(second.get("photo_set_hash") or "")
    if photo_set_a and photo_set_a == photo_set_b:
        signals.append({"signal": "exact_photo_set"})
    distance = coordinate_distance_meters(first, second)
    if distance is not None and distance <= 500:
        signals.append({
            "signal": "near_coordinates",
            "distance_m": round(distance, 1),
        })
    complex_a = _norm(
        first.get("complex_id") or first.get("complex_alias")
        or first.get("complex_name")
    )
    complex_b = _norm(
        second.get("complex_id") or second.get("complex_alias")
        or second.get("complex_name")
    )
    if complex_a and complex_a == complex_b:
        signals.append({"signal": "same_complex"})
    street_a, street_b = _norm(first.get("street")), _norm(second.get("street"))
    house_a, house_b = _norm(first.get("house_num")), _norm(second.get("house_num"))
    if street_a and house_a and (street_a, house_a) == (street_b, house_b):
        signals.append({"signal": "same_address"})
    return signals


def _area_bucket(row):
    area = _number(row.get("square_m2"))
    return str(round(area)) if area is not None else ""


def _integerish(value):
    parsed = _number(value)
    if parsed is None:
        return ""
    return str(int(parsed)) if parsed.is_integer() else str(parsed)


def candidate_pairs_with_reasons(rows, max_block=100):
    """Return precise candidate pairs without an O(N^2) citywide comparison."""
    blocks = defaultdict(list)
    for row in rows:
        listing_id = str(row["id"])
        photo_set_hash = str(row.get("photo_set_hash") or "")
        if photo_set_hash:
            blocks[("exact_photo_set", photo_set_hash)].append(listing_id)
        rooms, area, floor = (
            _integerish(row.get("rooms")), _area_bucket(row),
            _integerish(row.get("floor")),
        )
        if not rooms or not area:
            continue
        street, house = _norm(row.get("street")), _norm(row.get("house_num"))
        complex_key = _norm(
            row.get("complex_id") or row.get("complex_alias")
            or row.get("complex_name")
        )
        if street and house:
            blocks[("address", street, house, rooms, area, floor)].append(listing_id)
        if complex_key and floor:
            blocks[("complex", complex_key, rooms, area, floor)].append(listing_id)
        lat, lon = _number(row.get("latitude")), _number(row.get("longitude"))
        if lat is not None and lon is not None and floor:
            # Roughly a 100 m cell in Astana. This only adds candidates; a far
            # or inaccurate point is never a contradiction.
            blocks[("coordinates", round(lat, 3), round(lon, 3), rooms, area, floor)].append(
                listing_id
            )

    reasons_by_pair = defaultdict(set)
    for key, ids in blocks.items():
        unique_ids = sorted(set(ids))
        if not 2 <= len(unique_ids) <= max_block:
            continue
        for pair in itertools.combinations(unique_ids, 2):
            reasons_by_pair[pair].add(key[0])
    return {
        pair: sorted(reasons) for pair, reasons in sorted(reasons_by_pair.items())
    }


def _priority(reasons, signals, first, second):
    if "exact_photo_set" in reasons:
        value = 100
    elif len(reasons) >= 3:
        value = 80
    elif len(reasons) == 2:
        value = 60
    else:
        value = 30
    if first.get("status") == "active" and second.get("status") == "active":
        value += 5
    if any(item.get("signal") == "near_coordinates" for item in signals):
        value += 2
    return value


def refresh_shadow_candidates(conn, rows, source_version):
    """Persist the current metadata shortlist for later photo analysis in Main."""
    by_id = {str(row["id"]): row for row in rows}
    pairs = candidate_pairs_with_reasons(rows)
    now = _now()
    overrides = {
        (item["listing_id_low"], item["listing_id_high"]): item["decision"]
        for item in conn.execute(
            "SELECT listing_id_low,listing_id_high,decision FROM entity_overrides"
        )
    }
    evidence_by_pair = defaultdict(list)
    for item in conn.execute(
        "SELECT listing_id_low,listing_id_high,low_identity_hash,"
        "high_identity_hash,candidate_revision_hash,match_revision_hash "
        "FROM entity_match_edges WHERE is_current=1"
    ):
        evidence_by_pair[(item["listing_id_low"], item["listing_id_high"])].append(
            dict(item)
        )
    conn.execute("UPDATE entity_match_candidates SET is_current=0 WHERE is_current=1")
    conn.execute("UPDATE entity_match_edges SET is_current=0 WHERE is_current=1")
    written = 0
    status_counts = Counter()
    for (low, high), reasons in pairs.items():
        first, second = by_id[low], by_id[high]
        low_hash, high_hash = identity_hash(first), identity_hash(second)
        conflicts = hard_conflicts(first, second)
        signals = positive_signals(first, second)
        priority = _priority(reasons, signals, first, second)
        revision_payload = {
            "listing_id_low": low,
            "listing_id_high": high,
            "low_identity_hash": low_hash,
            "high_identity_hash": high_hash,
            "generator_version": CANDIDATE_GENERATOR_VERSION,
            "reasons": reasons,
            "hard_conflicts": conflicts,
            "positive_signals": signals,
        }
        revision = hashlib.sha256(
            _canonical_json(revision_payload).encode("utf-8")
        ).hexdigest()
        override = overrides.get((low, high))
        matching_evidence = next((
            item for item in evidence_by_pair.get((low, high), [])
            if item.get("low_identity_hash") == low_hash
            and item.get("high_identity_hash") == high_hash
            and item.get("candidate_revision_hash") == revision
        ), None)
        if override == "same_entity":
            status = "manual_linked"
        elif override == "different_entity":
            status = "manual_not_link"
        elif matching_evidence is not None:
            status = "photo_evidence_ready"
        else:
            status = "metadata_conflict" if conflicts else "awaiting_photo_analysis"
        status_counts[status] += 1
        conn.execute(
            """INSERT INTO entity_match_candidates
               (listing_id_low,listing_id_high,low_identity_hash,high_identity_hash,
                generator_version,candidate_revision_hash,reasons_json,
                hard_conflicts_json,positive_signals_json,source_version,status,
                priority,created_at,updated_at,is_current)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT(listing_id_low,listing_id_high,candidate_revision_hash)
               DO UPDATE SET reasons_json=excluded.reasons_json,
                 hard_conflicts_json=excluded.hard_conflicts_json,
                 positive_signals_json=excluded.positive_signals_json,
                 source_version=excluded.source_version,status=excluded.status,
                 priority=excluded.priority,updated_at=excluded.updated_at,
                 is_current=1""",
            (low, high, low_hash, high_hash, CANDIDATE_GENERATOR_VERSION,
             revision, _canonical_json(reasons), _canonical_json(conflicts),
             _canonical_json(signals), str(source_version), status, priority,
             now, now),
        )
        if matching_evidence is not None:
            conn.execute(
                "UPDATE entity_match_edges SET is_current=1 "
                "WHERE listing_id_low=? AND listing_id_high=? "
                "AND match_revision_hash=?",
                (low, high, matching_evidence["match_revision_hash"]),
            )
        written += 1
    return {
        "current_candidates": len(pairs),
        "metadata_conflicts": status_counts["metadata_conflict"],
        "awaiting_photo_analysis": status_counts["awaiting_photo_analysis"],
        "photo_evidence_ready": status_counts["photo_evidence_ready"],
        "manual_linked": status_counts["manual_linked"],
        "manual_not_link": status_counts["manual_not_link"],
        "written": written,
        "generator_version": CANDIDATE_GENERATOR_VERSION,
        "auto_merged": 0,
    }
