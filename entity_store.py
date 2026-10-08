"""Durable physical-apartment entities built from immutable listing episodes.

This module deliberately does not decide whether two listings match.  It owns
the lossless part of entity resolution: stable IDs, membership history,
presence transitions, transactional merge/split and canonical selection.
Matchers may propose operations later; original listing IDs are never deleted.
"""
import hashlib
import json
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone


ENTITY_SCHEMA_VERSION = "entity-store-v3"
ENTITY_GRACE_HOURS = float(os.environ.get("ENTITY_GRACE_HOURS", "24"))
IDENTITY_SNAPSHOT_FIELDS = (
    "id", "url", "title", "status", "price", "initial_price",
    "price_drop_count", "rooms", "square_m2", "floor", "floor_total",
    "street", "house_num", "district", "complex_id", "complex_alias",
    "complex_name", "latitude", "longitude", "seller_type", "owner_name",
    "first_seen_at", "last_seen_at", "missing_detected_at",
    "reactivation_count", "photo_urls", "photo_count", "photo_set_hash",
)


class StalePhotoEvidenceError(ValueError):
    """Main returned evidence for a candidate revision that is no longer current."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_schema(conn):
    """Create entity tables idempotently on both new and existing databases."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS property_entities (
            entity_id TEXT PRIMARY KEY,
            lifecycle_status TEXT NOT NULL,
            canonical_listing_id TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            relist_count INTEGER NOT NULL DEFAULT 0,
            merged_into_entity_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entity_members (
            listing_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            attached_at TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            FOREIGN KEY(entity_id) REFERENCES property_entities(entity_id)
        );
        CREATE INDEX IF NOT EXISTS idx_entity_members_entity
            ON entity_members(entity_id, listing_id);

        CREATE TABLE IF NOT EXISTS entity_membership_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            operation_id TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_open_membership_listing
            ON entity_membership_history(listing_id) WHERE valid_to IS NULL;

        CREATE TABLE IF NOT EXISTS listing_entity_state (
            listing_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            first_seen_at TEXT,
            last_seen_at TEXT,
            missing_detected_at TEXT,
            reactivation_count INTEGER NOT NULL DEFAULT 0,
            initial_price REAL,
            current_price REAL,
            price_drop_count INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT,
            photo_set_hash TEXT,
            source_version TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS listing_identity_snapshots (
            listing_id TEXT PRIMARY KEY,
            source_version TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS listing_price_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            price REAL NOT NULL,
            observation_type TEXT NOT NULL,
            history_complete INTEGER NOT NULL,
            source_version TEXT NOT NULL,
            UNIQUE(listing_id, source_version, observation_type, price)
        );
        CREATE INDEX IF NOT EXISTS idx_price_observations_listing_time
            ON listing_price_observations(listing_id, observed_at, observation_id);

        CREATE TABLE IF NOT EXISTS listing_presence_events (
            local_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            status_after TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            history_complete INTEGER NOT NULL,
            source_version TEXT NOT NULL,
            UNIQUE(listing_id, event_type, status_after, observed_at)
        );
        CREATE INDEX IF NOT EXISTS idx_presence_listing_time
            ON listing_presence_events(listing_id, observed_at, local_event_id);

        CREATE TABLE IF NOT EXISTS entity_operations (
            operation_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS entity_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL,
            event_seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            entity_id TEXT,
            listing_id TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(operation_id, event_seq)
        );

        CREATE TABLE IF NOT EXISTS canonical_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id TEXT NOT NULL,
            listing_id TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            operation_id TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_open_canonical_entity
            ON canonical_history(entity_id) WHERE valid_to IS NULL;

        CREATE TABLE IF NOT EXISTS entity_aliases (
            old_entity_id TEXT PRIMARY KEY,
            surviving_entity_id TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entity_lineage (
            operation_id TEXT NOT NULL,
            parent_entity_id TEXT NOT NULL,
            child_entity_id TEXT NOT NULL,
            moved_listing_ids_hash TEXT NOT NULL,
            PRIMARY KEY(operation_id, child_entity_id)
        );

        CREATE TABLE IF NOT EXISTS entity_overrides (
            listing_id_low TEXT NOT NULL,
            listing_id_high TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            override_version TEXT NOT NULL,
            actor TEXT,
            operation_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(listing_id_low < listing_id_high),
            PRIMARY KEY(listing_id_low, listing_id_high)
        );

        CREATE TABLE IF NOT EXISTS entity_match_candidates (
            listing_id_low TEXT NOT NULL,
            listing_id_high TEXT NOT NULL,
            low_identity_hash TEXT NOT NULL,
            high_identity_hash TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            candidate_revision_hash TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            hard_conflicts_json TEXT NOT NULL,
            positive_signals_json TEXT NOT NULL,
            source_version TEXT NOT NULL,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            CHECK(listing_id_low < listing_id_high),
            PRIMARY KEY(listing_id_low, listing_id_high, candidate_revision_hash)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_current_candidate_pair
            ON entity_match_candidates(listing_id_low, listing_id_high)
            WHERE is_current = 1;

        CREATE TABLE IF NOT EXISTS entity_match_edges (
            listing_id_low TEXT NOT NULL,
            listing_id_high TEXT NOT NULL,
            low_identity_hash TEXT NOT NULL,
            high_identity_hash TEXT NOT NULL,
            low_photo_set_hash TEXT,
            high_photo_set_hash TEXT,
            low_photo_content_set_hash TEXT,
            high_photo_content_set_hash TEXT,
            low_photo_evidence_set_hash TEXT,
            high_photo_evidence_set_hash TEXT,
            normalization_version TEXT NOT NULL,
            fingerprint_version TEXT NOT NULL,
            intra_listing_dedupe_version TEXT NOT NULL,
            photo_role_model_version TEXT NOT NULL,
            embedding_version TEXT,
            photo_asset_stats_version TEXT NOT NULL,
            matcher_version TEXT NOT NULL,
            candidate_revision_hash TEXT,
            source_version TEXT,
            match_revision_hash TEXT NOT NULL,
            score REAL,
            decision TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            CHECK(listing_id_low < listing_id_high),
            PRIMARY KEY(listing_id_low, listing_id_high, match_revision_hash)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_current_match_pair
            ON entity_match_edges(listing_id_low, listing_id_high)
            WHERE is_current = 1;

        """
    )
    state_columns = {
        r[1] for r in conn.execute("PRAGMA table_info(listing_entity_state)")
    }
    for name, declaration in (
        ("initial_price", "REAL"),
        ("current_price", "REAL"),
        ("price_drop_count", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in state_columns:
            conn.execute(
                f"ALTER TABLE listing_entity_state ADD COLUMN {name} {declaration}"
            )
    candidate_columns = {
        r[1] for r in conn.execute("PRAGMA table_info(entity_match_candidates)")
    }
    if "priority" not in candidate_columns:
        conn.execute(
            "ALTER TABLE entity_match_candidates "
            "ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
        )
    edge_columns = {
        r[1] for r in conn.execute("PRAGMA table_info(entity_match_edges)")
    }
    for name in ("candidate_revision_hash", "source_version"):
        if name not in edge_columns:
            conn.execute(f"ALTER TABLE entity_match_edges ADD COLUMN {name} TEXT")

    # Avoid DROP/CREATE DDL on every API connection. Rebuild only when an old
    # database actually has the pre-priority index definition.
    expected_index_columns = (
        "status,is_current,priority,updated_at,listing_id_low,listing_id_high"
    )
    existing_index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_candidates_review'"
    ).fetchone()
    normalized_sql = "".join((existing_index[0] if existing_index else "").split())
    if expected_index_columns not in normalized_sql:
        conn.execute("DROP INDEX IF EXISTS idx_candidates_review")
        conn.execute(
            "CREATE INDEX idx_candidates_review ON entity_match_candidates"
            "(status,is_current,priority,updated_at,listing_id_low,listing_id_high)"
        )


def _operation(conn, operation_type, idempotency_key, payload):
    existing = conn.execute(
        "SELECT operation_id FROM entity_operations WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        return existing[0], False
    operation_id = "op_" + uuid.uuid4().hex
    now = _now()
    conn.execute(
        "INSERT INTO entity_operations "
        "(operation_id,idempotency_key,operation_type,status,payload_json,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (operation_id, idempotency_key, operation_type, "processing", _json(payload), now),
    )
    return operation_id, True


def _complete_operation(conn, operation_id):
    conn.execute(
        "UPDATE entity_operations SET status='completed', completed_at=? "
        "WHERE operation_id=?",
        (_now(), operation_id),
    )


def _event(conn, operation_id, seq, event_type, entity_id=None,
           listing_id=None, payload=None):
    conn.execute(
        "INSERT OR IGNORE INTO entity_events "
        "(operation_id,event_seq,event_type,entity_id,listing_id,payload_json,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (operation_id, seq, event_type, entity_id, listing_id, _json(payload or {}), _now()),
    )


def _new_entity_id():
    return "ent_" + uuid.uuid4().hex


def _attach_new_listing(conn, listing_id):
    entity_id = _new_entity_id()
    now = _now()
    operation_id, created = _operation(
        conn, "create_entity", f"create_entity:{listing_id}", {"listing_id": listing_id},
    )
    if not created:
        row = conn.execute(
            "SELECT entity_id FROM entity_members WHERE listing_id=?", (listing_id,)
        ).fetchone()
        return row[0] if row else None
    conn.execute(
        "INSERT INTO property_entities "
        "(entity_id,lifecycle_status,created_at,updated_at) VALUES (?,?,?,?)",
        (entity_id, "active", now, now),
    )
    conn.execute(
        "INSERT INTO entity_members(listing_id,entity_id,attached_at,operation_id) "
        "VALUES (?,?,?,?)",
        (listing_id, entity_id, now, operation_id),
    )
    conn.execute(
        "INSERT INTO entity_membership_history "
        "(listing_id,entity_id,valid_from,operation_id,reason) VALUES (?,?,?,?,?)",
        (listing_id, entity_id, now, operation_id, "first_observed"),
    )
    _event(conn, operation_id, 1, "new_entity", entity_id, listing_id)
    _complete_operation(conn, operation_id)
    return entity_id


def _presence_time(row, event_type, now):
    if event_type == "missing":
        return row.get("missing_detected_at") or row.get("last_seen_at") or now
    if event_type == "bootstrap_state":
        return row.get("first_seen_at") or row.get("last_seen_at") or now
    return row.get("last_seen_at") or now


def _price(value):
    """Return a usable positive asking price without inventing market bounds."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _price_observed_at(row, now):
    return row.get("scraped_at") or row.get("last_seen_at") or now


def _observe_price(conn, row, previous, source_version, now):
    """Append only facts available in this snapshot.

    Collector currently exposes the initial and current price, but not every
    intermediate price observation.  Bootstrap rows are therefore marked
    incomplete.  Changes observed by Cleaner after bootstrap are complete from
    that point forward and are never overwritten.
    """
    listing_id = str(row["id"])
    initial = _price(row.get("initial_price"))
    current = _price(row.get("price"))
    if previous is None:
        if initial is not None:
            conn.execute(
                "INSERT OR IGNORE INTO listing_price_observations "
                "(listing_id,observed_at,price,observation_type,history_complete,"
                "source_version) VALUES (?,?,?,?,?,?)",
                (listing_id, row.get("first_seen_at") or now, initial,
                 "bootstrap_initial", 0, str(source_version)),
            )
        if current is not None and current != initial:
            conn.execute(
                "INSERT OR IGNORE INTO listing_price_observations "
                "(listing_id,observed_at,price,observation_type,history_complete,"
                "source_version) VALUES (?,?,?,?,?,?)",
                (listing_id, _price_observed_at(row, now), current,
                 "bootstrap_current", 0, str(source_version)),
            )
        return

    previous_price = _price(previous["current_price"])
    if current is not None and current != previous_price:
        conn.execute(
            "INSERT OR IGNORE INTO listing_price_observations "
            "(listing_id,observed_at,price,observation_type,history_complete,"
            "source_version) VALUES (?,?,?,?,?,?)",
            (listing_id, _price_observed_at(row, now), current,
             "observed_change", 1, str(source_version)),
        )


def observe_listings(conn, rows, source_version, content_hash_fn=None):
    """Persist listing episodes and locally observable presence transitions.

    Existing Collector history cannot be reconstructed: the first event for
    every previously unknown listing is explicitly an incomplete bootstrap.
    Later status transitions observed by Cleaner are complete from that point.
    """
    now = _now()
    members = {r[0]: r[1] for r in conn.execute(
        "SELECT listing_id,entity_id FROM entity_members"
    )}
    states = {r["listing_id"]: r for r in conn.execute(
        "SELECT * FROM listing_entity_state"
    )}
    affected = set()
    created_entities = transitions = 0

    for row in rows:
        listing_id = str(row["id"])
        entity_id = members.get(listing_id)
        if entity_id is None:
            entity_id = _attach_new_listing(conn, listing_id)
            members[listing_id] = entity_id
            created_entities += 1
        affected.add(entity_id)

        status = str(row.get("status") or "active")
        previous = states.get(listing_id)
        if previous is None:
            event_type = "bootstrap_state"
            history_complete = 0
        elif previous["status"] != status:
            event_type = "reactivated" if status == "active" else "missing"
            history_complete = 1
            transitions += 1
        else:
            event_type = None
            history_complete = None

        if event_type:
            conn.execute(
                "INSERT OR IGNORE INTO listing_presence_events "
                "(listing_id,event_type,status_after,observed_at,history_complete,source_version) "
                "VALUES (?,?,?,?,?,?)",
                (listing_id, event_type, status,
                 _presence_time(row, event_type, now), history_complete, str(source_version)),
            )

        _observe_price(conn, row, previous, source_version, now)

        identity_snapshot = {
            field: row.get(field) for field in IDENTITY_SNAPSHOT_FIELDS
        }
        conn.execute(
            """INSERT INTO listing_identity_snapshots
               (listing_id,source_version,payload_json,updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(listing_id) DO UPDATE SET
                 source_version=excluded.source_version,
                 payload_json=excluded.payload_json,
                 updated_at=excluded.updated_at""",
            (listing_id, str(source_version), _json(identity_snapshot), now),
        )

        content_hash = content_hash_fn(row) if content_hash_fn else None
        conn.execute(
            """
            INSERT INTO listing_entity_state
                (listing_id,status,first_seen_at,last_seen_at,missing_detected_at,
                 reactivation_count,initial_price,current_price,price_drop_count,
                 content_hash,photo_set_hash,source_version,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(listing_id) DO UPDATE SET
                status=excluded.status,
                first_seen_at=COALESCE(listing_entity_state.first_seen_at,excluded.first_seen_at),
                last_seen_at=excluded.last_seen_at,
                missing_detected_at=excluded.missing_detected_at,
                reactivation_count=excluded.reactivation_count,
                initial_price=COALESCE(listing_entity_state.initial_price,excluded.initial_price),
                current_price=excluded.current_price,
                price_drop_count=excluded.price_drop_count,
                content_hash=excluded.content_hash,
                photo_set_hash=excluded.photo_set_hash,
                source_version=excluded.source_version,
                updated_at=excluded.updated_at
            """,
            (listing_id, status, row.get("first_seen_at"), row.get("last_seen_at"),
             row.get("missing_detected_at"), int(row.get("reactivation_count") or 0),
             _price(row.get("initial_price")), _price(row.get("price")),
             int(row.get("price_drop_count") or 0), content_hash,
             row.get("photo_set_hash"), str(source_version), now),
        )

    for entity_id in affected:
        recompute_entity(conn, entity_id, now=now)
    return {
        "observed": len(rows),
        "created_entities": created_entities,
        "presence_transitions": transitions,
        "affected_entities": len(affected),
    }


def recompute_entity(conn, entity_id, now=None):
    rows = list(conn.execute(
        """SELECT s.* FROM entity_members m
           JOIN listing_entity_state s ON s.listing_id=m.listing_id
           WHERE m.entity_id=?""",
        (entity_id,),
    ))
    if not rows:
        return
    active = any(r["status"] == "active" for r in rows)
    if active:
        lifecycle = "active"
    else:
        now_dt = _parse_iso(now or _now())
        missing_times = [_parse_iso(r["missing_detected_at"] or r["last_seen_at"])
                         for r in rows]
        latest_missing = max((t for t in missing_times if t), default=None)
        age_h = ((now_dt - latest_missing).total_seconds() / 3600
                 if now_dt and latest_missing else ENTITY_GRACE_HOURS)
        lifecycle = "uncertain" if age_h < ENTITY_GRACE_HOURS else "off_market"

    first_seen = min((r["first_seen_at"] for r in rows if r["first_seen_at"]), default=None)
    last_seen = max((r["last_seen_at"] for r in rows if r["last_seen_at"]), default=None)
    # Несколько одновременных объявлений разных агентов — один рыночный
    # период, а не несколько перевыкладок. Считаем relist по разрывам в
    # объединённых active-интервалах всех members. Старые агрегаты Collector
    # без дат намеренно не превращаем в выдуманную точную историю.
    relists = max(0, _market_period_count(conn, entity_id, rows) - 1)
    conn.execute(
        "UPDATE property_entities SET lifecycle_status=?,first_seen_at=?,last_seen_at=?,"
        "relist_count=?,updated_at=? WHERE entity_id=?",
        (lifecycle, first_seen, last_seen, relists, now or _now(), entity_id),
    )


def _market_period_count(conn, entity_id, states):
    """Count disjoint known on-market periods across all entity members."""
    events = defaultdict(list)
    for event in conn.execute(
        """SELECT p.listing_id,p.event_type,p.observed_at
             FROM listing_presence_events p
             JOIN entity_members m ON m.listing_id=p.listing_id
            WHERE m.entity_id=? AND p.event_type!='bootstrap_state'
            ORDER BY p.observed_at,p.local_event_id""",
        (entity_id,),
    ):
        events[event["listing_id"]].append(event)

    intervals = []
    for state in states:
        start = _parse_iso(state["first_seen_at"])
        current_start = start
        for event in events.get(state["listing_id"], ()):
            at = _parse_iso(event["observed_at"])
            if at is None:
                continue
            if event["event_type"] == "missing":
                if current_start is not None and at >= current_start:
                    intervals.append((current_start, at))
                current_start = None
            elif event["event_type"] == "reactivated" and current_start is None:
                current_start = at

        if state["status"] == "active":
            if current_start is None:
                current_start = _parse_iso(state["last_seen_at"])
            if current_start is not None:
                intervals.append((current_start, None))
        elif current_start is not None:
            end = _parse_iso(
                state["missing_detected_at"] or state["last_seen_at"]
            )
            if end is not None and end >= current_start:
                intervals.append((current_start, end))

    if not intervals:
        return 0
    intervals.sort(key=lambda value: value[0])
    periods = 0
    merged_end = None
    have_period = False
    for start, end in intervals:
        if not have_period:
            periods = 1
            merged_end = end
            have_period = True
            continue
        if merged_end is None or start <= merged_end:
            if end is None or (merged_end is not None and end > merged_end):
                merged_end = end
        else:
            periods += 1
            merged_end = end
    return periods


def _row_quality(row):
    complete = sum(bool(row.get(k)) for k in (
        "title", "full_description", "rooms", "square_m2", "street", "house_num",
        "complex_id", "complex_name", "floor", "floor_total",
    ))
    try:
        photo_count = int(row.get("photo_count") or 0)
    except (TypeError, ValueError):
        photo_count = 0
    return (
        str(row.get("last_seen_at") or ""),
        complete,
        photo_count,
        str(row.get("id")),
    )


def _choose_canonical(candidates, current_listing_id=None):
    """Choose one complete source row, preferring the cheapest ready active ad.

    Price is never copied between rows: the selected listing supplies the
    entire published payload.  On an equal minimum price the current canonical
    remains stable; otherwise freshness and completeness break the tie.
    """
    active = [row for row in candidates if row.get("status") == "active"]
    if active:
        priced = [(row, _price(row.get("price"))) for row in active]
        valid_prices = [price for _, price in priced if price is not None]
        if valid_prices:
            minimum = min(valid_prices)
            pool = [row for row, price in priced if price == minimum]
        else:
            pool = active
        if current_listing_id:
            current = next(
                (row for row in pool if str(row["id"]) == str(current_listing_id)),
                None,
            )
            if current is not None:
                return current
        return max(pool, key=_row_quality)
    return max(candidates, key=_row_quality)


def _set_canonical(conn, entity_id, listing_id, source_version):
    current = conn.execute(
        "SELECT canonical_listing_id FROM property_entities WHERE entity_id=?",
        (entity_id,),
    ).fetchone()
    if current and current[0] == listing_id:
        return False
    key = f"canonical:{entity_id}:{listing_id}:{source_version}"
    operation_id, created = _operation(
        conn, "canonical_switch", key,
        {"entity_id": entity_id, "listing_id": listing_id,
         "source_version": str(source_version)},
    )
    if not created:
        return False
    now = _now()
    conn.execute(
        "UPDATE canonical_history SET valid_to=? WHERE entity_id=? AND valid_to IS NULL",
        (now, entity_id),
    )
    conn.execute(
        "INSERT INTO canonical_history "
        "(entity_id,listing_id,valid_from,operation_id,reason) VALUES (?,?,?,?,?)",
        (entity_id, listing_id, now, operation_id, "publication_ready_rank"),
    )
    conn.execute(
        "UPDATE property_entities SET canonical_listing_id=?,updated_at=? WHERE entity_id=?",
        (listing_id, now, entity_id),
    )
    _event(conn, operation_id, 1, "canonical_switched", entity_id, listing_id)
    _complete_operation(conn, operation_id)
    return True


def canonicalize_ready_rows(conn, rows, source_version):
    """Collapse publication-ready rows to one whole canonical row per entity."""
    if not rows:
        return [], {"entities": 0, "duplicates_removed": 0, "canonical_switches": 0}
    listing_ids = [str(r["id"]) for r in rows]
    member_map = {}
    for start in range(0, len(listing_ids), 500):
        chunk = listing_ids[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        member_map.update({r[0]: r[1] for r in conn.execute(
            f"SELECT listing_id,entity_id FROM entity_members "
            f"WHERE listing_id IN ({placeholders})", chunk
        )})

    grouped = defaultdict(list)
    for row in rows:
        entity_id = member_map.get(str(row["id"]))
        if entity_id:
            grouped[entity_id].append(row)

    output = []
    switches = 0
    for entity_id, candidates in grouped.items():
        current = conn.execute(
            "SELECT canonical_listing_id FROM property_entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        current_listing_id = current[0] if current else None
        chosen = _choose_canonical(candidates, current_listing_id)
        switches += int(_set_canonical(conn, entity_id, str(chosen["id"]), source_version))
        entity = conn.execute(
            "SELECT first_seen_at,last_seen_at,lifecycle_status,relist_count "
            "FROM property_entities WHERE entity_id=?", (entity_id,)
        ).fetchone()
        member_stats = conn.execute(
            """SELECT COUNT(*) AS member_count,
                      SUM(CASE WHEN s.status='active' THEN 1 ELSE 0 END)
                          AS active_count
               FROM entity_members m
               JOIN listing_entity_state s ON s.listing_id=m.listing_id
               WHERE m.entity_id=?""",
            (entity_id,),
        ).fetchone()
        ready_active = [row for row in candidates if row.get("status") == "active"]
        ready_active_prices = [
            value for value in (_price(row.get("price")) for row in ready_active)
            if value is not None
        ]
        published = dict(chosen)
        published["entity_id"] = entity_id
        published["source_listing_id"] = str(chosen["id"])
        published["entity_first_seen_at"] = entity["first_seen_at"]
        published["entity_last_seen_at"] = entity["last_seen_at"]
        published["entity_status"] = entity["lifecycle_status"]
        published["entity_relist_count"] = entity["relist_count"]
        published["entity_member_count"] = int(member_stats["member_count"] or 0)
        published["entity_active_listing_count"] = int(
            member_stats["active_count"] or 0
        )
        published["entity_ready_active_listing_count"] = len(ready_active)
        # Price analytics must never be influenced by an unreviewed or rejected
        # listing. Every row in candidates already has a current eligible verdict.
        published["entity_active_min_price"] = (
            min(ready_active_prices) if ready_active_prices else None
        )
        published["entity_active_max_price"] = (
            max(ready_active_prices) if ready_active_prices else None
        )
        output.append(published)

    return output, {
        "entities": len(output),
        "duplicates_removed": len(rows) - len(output),
        "canonical_switches": switches,
    }


def resolve_alias(conn, entity_id):
    seen = set()
    current = entity_id
    while current and current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT surviving_entity_id FROM entity_aliases WHERE old_entity_id=?",
            (current,),
        ).fetchone()
        if not row:
            return current
        current = row[0]
    raise ValueError("entity alias cycle detected")


def merge_listings(conn, listing_a, listing_b, reason,
                   decision_revision="manual-v1"):
    """Transaction-friendly, idempotent merge. Caller owns commit/rollback."""
    pair = [str(listing_a), str(listing_b)]
    rows = list(conn.execute(
        "SELECT listing_id,entity_id FROM entity_members WHERE listing_id IN (?,?)",
        pair,
    ))
    if len(rows) != 2:
        raise ValueError("both listings must already belong to entities")
    entities = {r[1] for r in rows}
    if len(entities) == 1:
        return next(iter(entities)), False
    details = list(conn.execute(
        "SELECT entity_id,created_at FROM property_entities WHERE entity_id IN (?,?)",
        tuple(entities),
    ))
    details.sort(key=lambda r: (r["created_at"], r["entity_id"]))
    survivor, absorbed = details[0]["entity_id"], details[1]["entity_id"]
    key = f"merge:{survivor}:{absorbed}:{decision_revision}"
    operation_id, created = _operation(
        conn, "merge", key,
        {"survivor": survivor, "absorbed": absorbed, "reason": reason},
    )
    if not created:
        return survivor, False
    now = _now()
    moved = [r[0] for r in conn.execute(
        "SELECT listing_id FROM entity_members WHERE entity_id=? ORDER BY listing_id",
        (absorbed,),
    )]
    for listing_id in moved:
        conn.execute(
            "UPDATE entity_membership_history SET valid_to=? "
            "WHERE listing_id=? AND valid_to IS NULL", (now, listing_id)
        )
        conn.execute(
            "UPDATE entity_members SET entity_id=?,attached_at=?,operation_id=? "
            "WHERE listing_id=?", (survivor, now, operation_id, listing_id)
        )
        conn.execute(
            "INSERT INTO entity_membership_history "
            "(listing_id,entity_id,valid_from,operation_id,reason) VALUES (?,?,?,?,?)",
            (listing_id, survivor, now, operation_id, reason),
        )
    conn.execute(
        "INSERT INTO entity_aliases(old_entity_id,surviving_entity_id,operation_id,created_at) "
        "VALUES (?,?,?,?)", (absorbed, survivor, operation_id, now)
    )
    conn.execute(
        "UPDATE property_entities SET lifecycle_status='merged',merged_into_entity_id=?,"
        "canonical_listing_id=NULL,updated_at=? WHERE entity_id=?",
        (survivor, now, absorbed),
    )
    conn.execute(
        "UPDATE property_entities SET canonical_listing_id=NULL,updated_at=? WHERE entity_id=?",
        (now, survivor),
    )
    conn.execute(
        "UPDATE canonical_history SET valid_to=? WHERE entity_id IN (?,?) AND valid_to IS NULL",
        (now, survivor, absorbed),
    )
    _event(conn, operation_id, 1, "entities_merged", survivor, payload={
        "absorbed_entity_id": absorbed, "moved_listing_ids": moved, "reason": reason,
    })
    _event(conn, operation_id, 2, "memberships_changed", survivor,
           payload={"moved_listing_ids": moved})
    recompute_entity(conn, survivor, now=now)
    _complete_operation(conn, operation_id)
    return survivor, True


def split_entity(conn, parent_entity_id, listing_ids, reason,
                 decision_revision="manual-v1"):
    """Move a strict subset of members to a new child entity."""
    parent_entity_id = resolve_alias(conn, parent_entity_id)
    requested = sorted({str(x) for x in listing_ids})
    moved_hash = hashlib.sha256(_json(requested).encode("utf-8")).hexdigest()
    key = f"split:{parent_entity_id}:{moved_hash}:{decision_revision}"
    existing = conn.execute(
        "SELECT operation_id FROM entity_operations WHERE idempotency_key=?", (key,)
    ).fetchone()
    if existing:
        row = conn.execute(
            "SELECT child_entity_id FROM entity_lineage WHERE operation_id=?",
            (existing[0],),
        ).fetchone()
        if not row:
            raise RuntimeError("completed split operation has no lineage")
        return row[0], False
    current = [r[0] for r in conn.execute(
        "SELECT listing_id FROM entity_members WHERE entity_id=? ORDER BY listing_id",
        (parent_entity_id,),
    )]
    if not requested or not set(requested) < set(current):
        raise ValueError("split must move a non-empty strict subset of members")
    if not set(requested).issubset(current):
        raise ValueError("split contains a listing outside the parent entity")
    operation_id, created = _operation(
        conn, "split", key,
        {"parent_entity_id": parent_entity_id, "listing_ids": requested,
         "reason": reason},
    )
    if not created:  # covered above; retained as a defensive race guard
        raise RuntimeError("split idempotency race")
    now = _now()
    child = _new_entity_id()
    conn.execute(
        "INSERT INTO property_entities "
        "(entity_id,lifecycle_status,created_at,updated_at) VALUES (?,?,?,?)",
        (child, "active", now, now),
    )
    for listing_id in requested:
        conn.execute(
            "UPDATE entity_membership_history SET valid_to=? "
            "WHERE listing_id=? AND valid_to IS NULL", (now, listing_id)
        )
        conn.execute(
            "UPDATE entity_members SET entity_id=?,attached_at=?,operation_id=? "
            "WHERE listing_id=?", (child, now, operation_id, listing_id)
        )
        conn.execute(
            "INSERT INTO entity_membership_history "
            "(listing_id,entity_id,valid_from,operation_id,reason) VALUES (?,?,?,?,?)",
            (listing_id, child, now, operation_id, reason),
        )
    conn.execute(
        "INSERT INTO entity_lineage "
        "(operation_id,parent_entity_id,child_entity_id,moved_listing_ids_hash) "
        "VALUES (?,?,?,?)", (operation_id, parent_entity_id, child, moved_hash)
    )
    conn.execute(
        "UPDATE canonical_history SET valid_to=? WHERE entity_id=? AND valid_to IS NULL",
        (now, parent_entity_id),
    )
    conn.execute(
        "UPDATE property_entities SET canonical_listing_id=NULL,updated_at=? "
        "WHERE entity_id=?", (now, parent_entity_id)
    )
    _event(conn, operation_id, 1, "entity_split", parent_entity_id, payload={
        "child_entity_id": child, "moved_listing_ids": requested, "reason": reason,
    })
    _event(conn, operation_id, 2, "split_child_created", child,
           payload={"parent_entity_id": parent_entity_id,
                    "moved_listing_ids": requested})

    # A merge may have created aliases from absorbed entity IDs to the parent.
    # After an emergency split those aliases must not silently resolve to the
    # wrong apartment. If all historical members of an alias moved, redirect it
    # to the child. If only some moved, invalidate the ambiguous alias.
    affected_aliases = []
    for alias in conn.execute(
        "SELECT old_entity_id FROM entity_aliases ORDER BY old_entity_id"
    ):
        old_entity_id = alias[0]
        if resolve_alias(conn, old_entity_id) != parent_entity_id:
            continue
        historical_members = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT listing_id FROM entity_membership_history "
                "WHERE entity_id=?", (old_entity_id,)
            )
        }
        moved_from_alias = historical_members.intersection(requested)
        if moved_from_alias:
            affected_aliases.append(
                (old_entity_id, historical_members, moved_from_alias)
            )
    for old_entity_id, historical_members, moved_from_alias in affected_aliases:
        if moved_from_alias == historical_members:
            conn.execute(
                "UPDATE entity_aliases SET surviving_entity_id=?,operation_id=?,"
                "created_at=? WHERE old_entity_id=?",
                (child, operation_id, now, old_entity_id),
            )
            conn.execute(
                "UPDATE property_entities SET merged_into_entity_id=?,updated_at=? "
                "WHERE entity_id=?",
                (child, now, old_entity_id),
            )
        else:
            conn.execute(
                "DELETE FROM entity_aliases WHERE old_entity_id=?",
                (old_entity_id,),
            )
            conn.execute(
                "UPDATE property_entities SET lifecycle_status='split_ambiguous',"
                "merged_into_entity_id=NULL,updated_at=? WHERE entity_id=?",
                (now, old_entity_id),
            )
    recompute_entity(conn, parent_entity_id, now=now)
    recompute_entity(conn, child, now=now)
    _complete_operation(conn, operation_id)
    return child, True


def entity_detail(conn, entity_id):
    """Return one entity with lossless member, presence and price history."""
    requested = str(entity_id)
    resolved = resolve_alias(conn, requested)
    entity = conn.execute(
        "SELECT * FROM property_entities WHERE entity_id=?", (resolved,)
    ).fetchone()
    if entity is None:
        return None
    members = []
    for state in conn.execute(
        """SELECT s.* FROM entity_members m
           JOIN listing_entity_state s ON s.listing_id=m.listing_id
           WHERE m.entity_id=? ORDER BY s.first_seen_at,s.listing_id""",
        (resolved,),
    ):
        listing_id = state["listing_id"]
        snapshot = conn.execute(
            "SELECT payload_json FROM listing_identity_snapshots WHERE listing_id=?",
            (listing_id,),
        ).fetchone()
        presence = [dict(row) for row in conn.execute(
            "SELECT event_type,status_after,observed_at,history_complete,source_version "
            "FROM listing_presence_events WHERE listing_id=? "
            "ORDER BY observed_at,local_event_id",
            (listing_id,),
        )]
        prices = [dict(row) for row in conn.execute(
            "SELECT observed_at,price,observation_type,history_complete,source_version "
            "FROM listing_price_observations WHERE listing_id=? "
            "ORDER BY observed_at,observation_id",
            (listing_id,),
        )]
        members.append({
            "state": dict(state),
            "listing": json.loads(snapshot[0]) if snapshot else None,
            "presence_events": presence,
            "price_observations": prices,
        })
    canonical_history = [dict(row) for row in conn.execute(
        "SELECT listing_id,valid_from,valid_to,operation_id,reason "
        "FROM canonical_history WHERE entity_id=? ORDER BY history_id",
        (resolved,),
    )]
    return {
        "requested_entity_id": requested,
        "resolved_entity_id": resolved,
        "entity": dict(entity),
        "members": members,
        "canonical_history": canonical_history,
    }


def match_candidates_page(conn, status=None, limit=50, offset=0):
    """Return current shadow candidates with compact source listing snapshots."""
    where = "WHERE c.is_current=1"
    params = []
    if status:
        where += " AND c.status=?"
        params.append(str(status))
    total = conn.execute(
        f"SELECT COUNT(*) FROM entity_match_candidates c {where}", params,
    ).fetchone()[0]
    rows = []
    for candidate in conn.execute(
        f"""SELECT c.*,
                    low.payload_json AS low_payload_json,
                    high.payload_json AS high_payload_json
             FROM entity_match_candidates c
             LEFT JOIN listing_identity_snapshots low
               ON low.listing_id=c.listing_id_low
             LEFT JOIN listing_identity_snapshots high
               ON high.listing_id=c.listing_id_high
             {where}
             ORDER BY c.priority DESC,c.updated_at,c.listing_id_low,c.listing_id_high
             LIMIT ? OFFSET ?""",
        params + [int(limit), int(offset)],
    ):
        item = dict(candidate)
        for field in (
            "reasons_json", "hard_conflicts_json", "positive_signals_json",
        ):
            item[field.removesuffix("_json")] = json.loads(item.pop(field))
        item["listing_low"] = (
            json.loads(item.pop("low_payload_json"))
            if item["low_payload_json"] else None
        )
        item["listing_high"] = (
            json.loads(item.pop("high_payload_json"))
            if item["high_payload_json"] else None
        )
        rows.append(item)
    return total, rows


def _canonical_pair(listing_a, listing_b):
    first, second = str(listing_a), str(listing_b)
    if not first or not second or first == second:
        raise ValueError("two different listing IDs are required")
    return (first, second, False) if first < second else (second, first, True)


def record_photo_evidence(conn, listing_a, listing_b, evidence):
    """Store a fully versioned pair result produced by purchase Main.

    This is deliberately evidence-only. A high score never mutates entity
    membership while auto-merge is disabled.
    """
    low, high, swapped = _canonical_pair(listing_a, listing_b)
    known = conn.execute(
        "SELECT COUNT(*) FROM entity_members WHERE listing_id IN (?,?)",
        (low, high),
    ).fetchone()[0]
    if known != 2:
        raise ValueError("both listings must be known to Cleaner")

    value = dict(evidence)
    candidate = conn.execute(
        "SELECT candidate_revision_hash,low_identity_hash,high_identity_hash,"
        "source_version FROM entity_match_candidates "
        "WHERE listing_id_low=? AND listing_id_high=? AND is_current=1",
        (low, high),
    ).fetchone()
    if candidate is None:
        raise StalePhotoEvidenceError("candidate is no longer current")
    submitted_candidate_revision = str(
        value.get("candidate_revision_hash") or ""
    )
    if submitted_candidate_revision != candidate["candidate_revision_hash"]:
        raise StalePhotoEvidenceError(
            "candidate revision changed; fetch a fresh comparison job"
        )

    required_versions = (
        "normalization_version", "fingerprint_version",
        "intra_listing_dedupe_version", "photo_role_model_version",
        "photo_asset_stats_version", "matcher_version",
    )
    missing = [name for name in required_versions if not value.get(name)]
    if missing:
        raise ValueError("missing evidence versions: " + ", ".join(missing))
    decision = str(value.get("decision") or "")
    if decision not in {"likely_same", "likely_different", "uncertain"}:
        raise ValueError("unsupported photo evidence decision")
    try:
        score = float(value.get("score"))
    except (TypeError, ValueError) as exc:
        raise ValueError("photo evidence score must be numeric") from exc
    if not 0 <= score <= 1:
        raise ValueError("photo evidence score must be between 0 and 1")

    low_state = conn.execute(
        "SELECT photo_set_hash FROM listing_entity_state "
        "WHERE listing_id=?", (low,),
    ).fetchone()
    high_state = conn.execute(
        "SELECT photo_set_hash FROM listing_entity_state "
        "WHERE listing_id=?", (high,),
    ).fetchone()
    side_a = dict(value.get("listing_a") or {})
    side_b = dict(value.get("listing_b") or {})
    low_side, high_side = (side_b, side_a) if swapped else (side_a, side_b)
    for label, side, expected_identity, expected_photo_set in (
        ("low", low_side, candidate["low_identity_hash"],
         low_state["photo_set_hash"]),
        ("high", high_side, candidate["high_identity_hash"],
         high_state["photo_set_hash"]),
    ):
        if str(side.get("identity_hash") or "") != str(expected_identity or ""):
            raise StalePhotoEvidenceError(
                f"{label} listing identity changed; fetch a fresh comparison job"
            )
        submitted_photo_set = side.get("photo_set_hash")
        if (submitted_photo_set or None) != (expected_photo_set or None):
            raise StalePhotoEvidenceError(
                f"{label} photo set changed; fetch a fresh comparison job"
            )

    revision_payload = {
        "listing_id_low": low,
        "listing_id_high": high,
        "candidate_revision_hash": candidate["candidate_revision_hash"],
        "low_identity_hash": candidate["low_identity_hash"],
        "high_identity_hash": candidate["high_identity_hash"],
        "low_photo_set_hash": low_state["photo_set_hash"],
        "high_photo_set_hash": high_state["photo_set_hash"],
        "low_photo_content_set_hash": low_side.get("photo_content_set_hash"),
        "high_photo_content_set_hash": high_side.get("photo_content_set_hash"),
        "low_photo_evidence_set_hash": low_side.get("photo_evidence_set_hash"),
        "high_photo_evidence_set_hash": high_side.get("photo_evidence_set_hash"),
        **{name: value.get(name) for name in required_versions},
        "embedding_version": value.get("embedding_version"),
    }
    revision = hashlib.sha256(
        _json(revision_payload).encode("utf-8")
    ).hexdigest()
    conn.execute(
        "UPDATE entity_match_edges SET is_current=0 "
        "WHERE listing_id_low=? AND listing_id_high=? AND is_current=1 "
        "AND match_revision_hash!=?",
        (low, high, revision),
    )
    now = _now()
    conn.execute(
        """INSERT INTO entity_match_edges
           (listing_id_low,listing_id_high,low_identity_hash,high_identity_hash,
            low_photo_set_hash,high_photo_set_hash,
            low_photo_content_set_hash,high_photo_content_set_hash,
            low_photo_evidence_set_hash,high_photo_evidence_set_hash,
            normalization_version,fingerprint_version,intra_listing_dedupe_version,
            photo_role_model_version,embedding_version,photo_asset_stats_version,
            matcher_version,candidate_revision_hash,source_version,
            match_revision_hash,score,decision,evidence_json,
            processed_at,is_current)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
           ON CONFLICT(listing_id_low,listing_id_high,match_revision_hash)
           DO UPDATE SET score=excluded.score,decision=excluded.decision,
             evidence_json=excluded.evidence_json,processed_at=excluded.processed_at,
             is_current=1""",
        (low, high, candidate["low_identity_hash"], candidate["high_identity_hash"],
         low_state["photo_set_hash"], high_state["photo_set_hash"],
         low_side.get("photo_content_set_hash"),
         high_side.get("photo_content_set_hash"),
         low_side.get("photo_evidence_set_hash"),
         high_side.get("photo_evidence_set_hash"),
         value["normalization_version"], value["fingerprint_version"],
         value["intra_listing_dedupe_version"],
         value["photo_role_model_version"], value.get("embedding_version"),
         value["photo_asset_stats_version"], value["matcher_version"],
         candidate["candidate_revision_hash"], candidate["source_version"], revision,
         score, decision, _json(value), now),
    )
    conn.execute(
        "UPDATE entity_match_candidates SET status='photo_evidence_ready',updated_at=? "
        "WHERE listing_id_low=? AND listing_id_high=? AND is_current=1",
        (now, low, high),
    )
    return {
        "listing_id_low": low,
        "listing_id_high": high,
        "match_revision_hash": revision,
        "candidate_revision_hash": candidate["candidate_revision_hash"],
        "decision": decision,
        "score": score,
        "auto_merged": False,
    }


def apply_manual_match_decision(conn, listing_a, listing_b, decision, reason,
                                actor, decision_revision):
    """Apply an explicit human decision transactionally and idempotently."""
    low, high, _ = _canonical_pair(listing_a, listing_b)
    decision = str(decision)
    if decision not in {"same_entity", "different_entity"}:
        raise ValueError("decision must be same_entity or different_entity")
    if not str(reason or "").strip():
        raise ValueError("manual decision requires a reason")
    if not str(decision_revision or "").strip():
        raise ValueError("manual decision requires decision_revision")
    memberships = {
        row["listing_id"]: row["entity_id"] for row in conn.execute(
            "SELECT listing_id,entity_id FROM entity_members WHERE listing_id IN (?,?)",
            (low, high),
        )
    }
    if len(memberships) != 2:
        raise ValueError("both listings must be known to Cleaner")
    if decision == "different_entity" and memberships[low] == memberships[high]:
        raise ValueError("pair is already merged; use an explicit entity split")

    key = f"manual_override:{low}:{high}:{decision}:{decision_revision}"
    operation_id, created = _operation(
        conn, "manual_override", key,
        {"listing_id_low": low, "listing_id_high": high,
         "decision": decision, "reason": reason, "actor": actor,
         "decision_revision": decision_revision},
    )
    if not created:
        current = conn.execute(
            "SELECT entity_id FROM entity_members WHERE listing_id=?", (low,)
        ).fetchone()
        return {
            "operation_id": operation_id,
            "decision": decision,
            "entity_id": current[0] if current else None,
            "changed": False,
        }

    entity_id = memberships[low]
    if decision == "same_entity":
        entity_id, _ = merge_listings(
            conn, low, high, str(reason),
            decision_revision=f"manual:{decision_revision}",
        )
    now = _now()
    conn.execute(
        """INSERT INTO entity_overrides
           (listing_id_low,listing_id_high,decision,reason,override_version,
            actor,operation_id,updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(listing_id_low,listing_id_high) DO UPDATE SET
             decision=excluded.decision,reason=excluded.reason,
             override_version=excluded.override_version,actor=excluded.actor,
             operation_id=excluded.operation_id,updated_at=excluded.updated_at""",
        (low, high, decision, str(reason), str(decision_revision),
         str(actor or ""), operation_id, now),
    )
    candidate_status = (
        "manual_linked" if decision == "same_entity" else "manual_not_link"
    )
    conn.execute(
        "UPDATE entity_match_candidates SET status=?,updated_at=? "
        "WHERE listing_id_low=? AND listing_id_high=? AND is_current=1",
        (candidate_status, now, low, high),
    )
    _event(
        conn, operation_id, 1, "manual_match_decision", entity_id,
        payload={"listing_id_low": low, "listing_id_high": high,
                 "decision": decision, "reason": reason, "actor": actor},
    )
    _complete_operation(conn, operation_id)
    return {
        "operation_id": operation_id,
        "decision": decision,
        "entity_id": entity_id,
        "changed": True,
    }
