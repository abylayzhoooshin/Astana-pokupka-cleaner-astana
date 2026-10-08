import copy
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import requests
from fastapi import HTTPException

import cleaner_db
import collector_client
import entity_matcher
import entity_store
import heuristics
import openai_batch
import pipeline
import service
import verdicts_api


def row(listing_id="1", **changes):
    value = {
        "id": listing_id,
        "url": f"https://krisha.kz/a/show/{listing_id}",
        "title": "2-комнатная квартира · 60 м²",
        "full_description": "Продам полноценную квартиру",
        "rent_renovation": "хорошее",
        "priv_dorm": None,
        "square_m2": 60.0,
        "rooms": 2,
        "floor": 5,
        "floor_total": 12,
        "price": 42_000_000,
        "status": "active",
        "last_seen_at": "2026-10-05T00:00:00+00:00",
        "photo_urls": "[]",
        "photo_count": 0,
        "photo_set_hash": None,
    }
    value.update(changes)
    return value


def collector_stub(listing_id):
    return {
        "id": str(listing_id),
        "photo_urls": "[]",
        "photo_count": 0,
        "photo_set_hash": None,
    }


class SalesRulesTests(unittest.TestCase):
    def test_obvious_partial_property_is_free_rule(self):
        verdict = heuristics.classify(row(title="Продам долю в квартире"))
        self.assertEqual("partial_property", verdict["reason_code"])

    def test_one_room_apartment_is_not_a_room(self):
        self.assertIsNone(heuristics.classify(row(title="1-комнатная квартира · 40 м²")))

    def test_non_residential_title_is_free_rule(self):
        verdict = heuristics.classify(row(title="Продам офис 60 м²"))
        self.assertEqual("not_apartment", verdict["reason_code"])

    def test_condition_and_dorm_flag_are_not_automatic_rejections(self):
        value = row(rent_renovation="без ремонта", priv_dorm="да", square_m2=18)
        self.assertIsNone(heuristics.classify(value))


class ContentIdentityTests(unittest.TestCase):
    def test_technical_and_price_changes_do_not_change_text_hash(self):
        original = row()
        changed = copy.deepcopy(original)
        changed.update(
            price=30_000_000,
            status="missing",
            floor=9,
            last_seen_at="2026-11-01T00:00:00+00:00",
            photo_set_hash="b" * 64,
        )
        self.assertEqual(
            collector_client.content_hash(original),
            collector_client.content_hash(changed),
        )

    def test_model_input_changes_change_hash(self):
        original = row()
        for field, value in {
            "title": "другой заголовок",
            "full_description": "другое описание",
            "rent_renovation": "черновая",
            "priv_dorm": "да",
            "square_m2": 61.0,
            "rooms": 3,
        }.items():
            changed = copy.deepcopy(original)
            changed[field] = value
            self.assertNotEqual(
                collector_client.content_hash(original),
                collector_client.content_hash(changed),
                field,
            )

    def test_formatted_model_input_excludes_price_and_floor(self):
        text = openai_batch._format_listing("1", row())
        self.assertNotIn("42000000", text)
        self.assertNotIn("этаж 5/12", text)
        self.assertIn("Комнат: 2", text)

    def test_photo_identity_matches_collector_algorithm(self):
        urls = ["https://img/2.jpg", "https://img/1.jpg"]
        value = collector_stub("photos")
        value.update(
            photo_urls=json.dumps(urls),
            photo_count=2,
            photo_set_hash=collector_client.photo_set_hash(urls),
        )
        self.assertTrue(collector_client.photo_identity_valid(value))
        value["photo_set_hash"] = "wrong"
        self.assertFalse(collector_client.photo_identity_valid(value))


class VerdictContractTests(unittest.TestCase):
    def test_sales_codes_are_accepted(self):
        for code in openai_batch._VALID_CODES - {"ok"}:
            verdict = openai_batch._validate_verdict({
                "usable": False,
                "reason_code": code,
                "confidence": "high",
                "reason": "явный тестовый признак",
            })
            self.assertEqual("llm", verdict["source"], code)

    def test_rental_code_and_contradiction_are_rejected(self):
        for payload in (
            {"usable": False, "reason_code": "daily_rental", "confidence": "high"},
            {"usable": True, "reason_code": "partial_property", "confidence": "high"},
        ):
            self.assertEqual("llm_failed", openai_batch._validate_verdict(payload)["source"])

    def test_reason_shape_is_enforced(self):
        for payload in (
            {"usable": True, "reason_code": "ok", "confidence": "high", "reason": "лишнее"},
            {"usable": False, "reason_code": "not_sale", "confidence": "high", "reason": ""},
        ):
            self.assertEqual("llm_failed", openai_batch._validate_verdict(payload)["source"])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = cleaner_db.DB_PATH
        cleaner_db.DB_PATH = os.path.join(self.temp.name, "test.db")

    def tearDown(self):
        cleaner_db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_missing_and_unknown_are_published_but_rejected_is_not(self):
        rows = [
            row("ok", status="missing"),
            row("bad"),
            row("unknown"),
        ]
        with cleaner_db.connect() as conn:
            cleaner_db.upsert_verdict(
                conn, "ok", collector_client.content_hash(rows[0]),
                {"usable": True, "reason_code": "ok", "confidence": "high",
                 "reason": "", "source": "llm"}, "v1",
            )
            cleaner_db.upsert_verdict(
                conn, "bad", collector_client.content_hash(rows[1]),
                {"usable": False, "reason_code": "partial_property", "confidence": "high",
                 "reason": "доля", "source": "llm"}, "v1",
            )
            cleaner_db.upsert_verdict(
                conn, "unknown", collector_client.content_hash(rows[2]),
                {"usable": None, "reason_code": "llm_failed", "confidence": "low",
                 "reason": "ошибка", "source": "llm_failed"}, "v1",
            )
            conn.commit()
            result = pipeline.publish_clean_baseline(conn, rows, "v1")
            published = list(cleaner_db.iter_clean_baseline(conn))

        self.assertEqual(2, result["published"])
        self.assertEqual({"ok", "unknown"}, {item["id"] for item in published})
        self.assertEqual("missing", next(item for item in published if item["id"] == "ok")["status"])

    def test_empty_rebuild_keeps_previous_snapshot(self):
        original = row("old")
        with cleaner_db.connect() as conn:
            cleaner_db.upsert_verdict(
                conn, "old", collector_client.content_hash(original),
                {"usable": True, "reason_code": "ok", "confidence": "high",
                 "reason": "", "source": "llm"}, "v1",
            )
            conn.commit()
            pipeline.publish_clean_baseline(conn, [original], "v1")
            result = pipeline.publish_clean_baseline(conn, [row("new")], "v2")
            info = cleaner_db.clean_baseline_info(conn)
            published = list(cleaner_db.iter_clean_baseline(conn))

        self.assertEqual("empty_result", result["publish_skipped"])
        self.assertEqual("v1", info[1])
        self.assertEqual(["old"], [item["id"] for item in published])

    def test_csv_is_prebuilt_on_disk_and_served_without_memory_copy(self):
        listing = row("csv-ready", photo_urls=["https://img/1.jpg"])
        with cleaner_db.connect() as conn:
            cleaner_db.upsert_verdict(
                conn, listing["id"], collector_client.content_hash(listing),
                {"usable": True, "reason_code": "ok", "confidence": "high",
                 "reason": "", "source": "llm"}, "v1",
            )
            conn.commit()
            pipeline.publish_clean_baseline(conn, [listing], "v1")
            csv_path = cleaner_db.ensure_clean_baseline_csv(conn)

        self.assertTrue(os.path.isfile(csv_path))
        with open(csv_path, encoding="utf-8") as handle:
            csv_text = handle.read()
        self.assertIn("csv-ready", csv_text)
        self.assertIn("https://img/1.jpg", csv_text)
        response = verdicts_api.baseline_clean_csv()
        self.assertEqual(os.path.abspath(csv_path), os.path.abspath(response.path))


class ApiAuthorizationTests(unittest.TestCase):
    def test_write_api_fails_closed_when_key_is_not_configured(self):
        with mock.patch.object(verdicts_api, "API_KEY", ""):
            with self.assertRaises(HTTPException) as raised:
                verdicts_api.require_write_api_key("anything")
        self.assertEqual(503, raised.exception.status_code)

    def test_write_api_rejects_wrong_key_and_accepts_correct_key(self):
        with mock.patch.object(verdicts_api, "API_KEY", "secret"):
            with self.assertRaises(HTTPException) as raised:
                verdicts_api.require_write_api_key("wrong")
            self.assertEqual(401, raised.exception.status_code)
            self.assertIsNone(verdicts_api.require_write_api_key("secret"))

    def test_manual_entity_changes_are_disabled_by_default(self):
        payload = verdicts_api.ManualMatchDecisionRequest(
            listing_id_a="a", listing_id_b="b", decision="same_entity",
            reason="test", decision_revision="v1",
        )
        with mock.patch.object(
            verdicts_api, "MANUAL_MATCH_WRITES_ENABLED", False,
        ):
            with self.assertRaises(HTTPException) as raised:
                verdicts_api.entity_match_manual_decision(payload)
        self.assertEqual(403, raised.exception.status_code)


class DurableStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = cleaner_db.DB_PATH
        cleaner_db.DB_PATH = os.path.join(self.temp.name, "durable.db")

    def tearDown(self):
        cleaner_db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_pending_batch_survives_restart_and_blocks_duplicate(self):
        with cleaner_db.connect() as conn:
            cleaner_db.create_batch(conn, "batch-1", {"listing-1": "hash-1"})
            conn.commit()
        with cleaner_db.connect() as conn:
            self.assertEqual({"listing-1"}, cleaner_db.in_flight_ids(conn))

    def test_schema_enforces_foreign_keys_and_persists_batch_configuration(self):
        with cleaner_db.connect() as conn:
            self.assertEqual(cleaner_db.SCHEMA_VERSION, conn.execute(
                "PRAGMA user_version"
            ).fetchone()[0])
            self.assertEqual(1, conn.execute("PRAGMA foreign_keys").fetchone()[0])
            cleaner_db.create_batch(
                conn, "configured-batch", {"listing-1": "hash-1"},
                group_size=17, model="model-at-submit",
                policy_version="policy-at-submit",
            )
            conn.commit()
            sqlite_schema_version = conn.execute(
                "PRAGMA schema_version"
            ).fetchone()[0]
        with cleaner_db.connect() as conn:
            stored = conn.execute(
                "SELECT group_size,model,policy_version FROM batches "
                "WHERE batch_id='configured-batch'"
            ).fetchone()
            schema_version_after_reopen = conn.execute(
                "PRAGMA schema_version"
            ).fetchone()[0]
        self.assertEqual((17, "model-at-submit", "policy-at-submit"), tuple(stored))
        self.assertEqual(sqlite_schema_version, schema_version_after_reopen)

    def test_legacy_batch_table_is_migrated_without_losing_pending_work(self):
        legacy = sqlite3.connect(cleaner_db.DB_PATH)
        legacy.execute(
            "CREATE TABLE batches (batch_id TEXT PRIMARY KEY,status TEXT NOT NULL,"
            "listing_ids TEXT NOT NULL,submitted_at TEXT NOT NULL,completed_at TEXT,"
            "output_file_id TEXT,error TEXT)"
        )
        legacy.execute(
            "INSERT INTO batches(batch_id,status,listing_ids,submitted_at) "
            "VALUES ('legacy','pending','{\"old-id\":\"old-hash\"}','2026-01-01')"
        )
        legacy.commit()
        legacy.close()

        with cleaner_db.connect() as conn:
            columns = {
                item[1] for item in conn.execute("PRAGMA table_info(batches)")
            }
            pending = cleaner_db.in_flight_ids(conn)

        self.assertTrue({"group_size", "model", "policy_version"} <= columns)
        self.assertEqual({"old-id"}, pending)

    def test_policy_version_is_persisted_and_exposed(self):
        with cleaner_db.connect() as conn:
            cleaner_db.upsert_verdict(
                conn, "1", "hash", {"usable": True, "reason_code": "ok",
                "confidence": "high", "reason": "", "source": "llm"}, "v1",
                policy_version=openai_batch.POLICY_VERSION,
            )
            conn.commit()
            stored = conn.execute(
                "SELECT policy_version FROM verdicts WHERE id='1'"
            ).fetchone()[0]
        self.assertEqual("sales-astana-v1", stored)


class EntityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = cleaner_db.DB_PATH
        cleaner_db.DB_PATH = os.path.join(self.temp.name, "entities.db")

    def tearDown(self):
        cleaner_db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_schema_migrates_candidate_priority_before_creating_index(self):
        conn = sqlite3.connect(cleaner_db.DB_PATH)
        conn.executescript(
            """
            CREATE TABLE entity_match_candidates (
                listing_id_low TEXT NOT NULL,
                listing_id_high TEXT NOT NULL,
                candidate_revision_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                is_current INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(listing_id_low,listing_id_high,candidate_revision_hash)
            );
            """
        )
        entity_store.create_schema(conn)
        columns = {
            item[1] for item in conn.execute(
                "PRAGMA table_info(entity_match_candidates)"
            )
        }
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_candidates_review'"
        ).fetchone()[0]
        conn.close()

        self.assertIn("priority", columns)
        self.assertIn("priority", index_sql)

    def test_entity_id_is_stable_and_presence_transitions_are_append_only(self):
        active = row(
            "episode-1",
            first_seen_at="2026-10-01T00:00:00+00:00",
            last_seen_at="2026-10-05T00:00:00+00:00",
        )
        missing = row(
            "episode-1",
            status="missing",
            first_seen_at="2026-10-01T00:00:00+00:00",
            last_seen_at="2026-10-05T00:00:00+00:00",
            missing_detected_at="2026-10-07T00:00:00+00:00",
        )
        with cleaner_db.connect() as conn:
            first = entity_store.observe_listings(
                conn, [active], "v1", collector_client.content_hash,
            )
            entity_before = conn.execute(
                "SELECT entity_id FROM entity_members WHERE listing_id='episode-1'"
            ).fetchone()[0]
            second = entity_store.observe_listings(
                conn, [missing], "v2", collector_client.content_hash,
            )
            entity_after = conn.execute(
                "SELECT entity_id FROM entity_members WHERE listing_id='episode-1'"
            ).fetchone()[0]
            events = [tuple(r) for r in conn.execute(
                "SELECT event_type,history_complete FROM listing_presence_events "
                "WHERE listing_id='episode-1' ORDER BY local_event_id"
            )]

        self.assertEqual(1, first["created_entities"])
        self.assertEqual(1, second["presence_transitions"])
        self.assertEqual(entity_before, entity_after)
        self.assertEqual([("bootstrap_state", 0), ("missing", 1)], events)

    def test_price_history_marks_bootstrap_incomplete_and_new_changes_complete(self):
        initial = row(
            "priced", initial_price=50_000_000, price=45_000_000,
            price_drop_count=1, scraped_at="2026-10-05T01:00:00+00:00",
        )
        changed = row(
            "priced", initial_price=50_000_000, price=44_000_000,
            price_drop_count=2, scraped_at="2026-10-06T01:00:00+00:00",
        )
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, [initial], "v1", collector_client.content_hash,
            )
            entity_store.observe_listings(
                conn, [changed], "v2", collector_client.content_hash,
            )
            entity_store.observe_listings(
                conn, [changed], "v2", collector_client.content_hash,
            )
            observations = [tuple(item) for item in conn.execute(
                "SELECT price,observation_type,history_complete "
                "FROM listing_price_observations ORDER BY observation_id"
            )]
            state = conn.execute(
                "SELECT initial_price,current_price,price_drop_count "
                "FROM listing_entity_state WHERE listing_id='priced'"
            ).fetchone()

        self.assertEqual([
            (50_000_000.0, "bootstrap_initial", 0),
            (45_000_000.0, "bootstrap_current", 0),
            (44_000_000.0, "observed_change", 1),
        ], observations)
        self.assertEqual((50_000_000.0, 44_000_000.0, 2), tuple(state))

    def test_merge_keeps_members_and_publishes_one_whole_active_row(self):
        old = row(
            "old-id", status="missing",
            first_seen_at="2026-09-01T00:00:00+00:00",
            last_seen_at="2026-09-10T00:00:00+00:00",
            missing_detected_at="2026-09-12T00:00:00+00:00",
            price=40_000_000,
        )
        new = row(
            "new-id", status="active",
            first_seen_at="2026-10-01T00:00:00+00:00",
            last_seen_at="2026-10-05T00:00:00+00:00",
            price=41_000_000,
        )
        verdict = {
            "usable": True, "reason_code": "ok", "confidence": "high",
            "reason": "", "source": "llm",
        }
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, [old, new], "v1", collector_client.content_hash,
            )
            survivor, changed = entity_store.merge_listings(
                conn, "old-id", "new-id", "same physical apartment", "review-1",
            )
            for value in (old, new):
                cleaner_db.upsert_verdict(
                    conn, value["id"], collector_client.content_hash(value),
                    verdict, "v1",
                )
            result = pipeline.publish_clean_baseline(conn, [old, new], "v1")
            published = list(cleaner_db.iter_clean_baseline(conn))
            members = list(conn.execute(
                "SELECT listing_id FROM entity_members WHERE entity_id=?",
                (survivor,),
            ))

        self.assertTrue(changed)
        self.assertEqual(1, result["published"])
        self.assertEqual(1, result["duplicates_removed"])
        self.assertEqual("new-id", published[0]["id"])
        self.assertEqual("new-id", published[0]["source_listing_id"])
        self.assertEqual(survivor, published[0]["entity_id"])
        self.assertEqual({"old-id", "new-id"}, {r[0] for r in members})
        self.assertEqual(41_000_000, published[0]["price"])

    def test_failed_merge_rolls_back_without_half_moved_members(self):
        listings = [row("left"), row("right")]
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, listings, "v1", collector_client.content_hash,
            )
            conn.commit()
            before = dict(conn.execute(
                "SELECT listing_id,entity_id FROM entity_members"
            ))
            with mock.patch.object(
                entity_store, "recompute_entity",
                side_effect=RuntimeError("simulated crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    entity_store.merge_listings(
                        conn, "left", "right", "test crash", "crash-1",
                    )
            conn.rollback()
            after = dict(conn.execute(
                "SELECT listing_id,entity_id FROM entity_members"
            ))
            incomplete_operations = conn.execute(
                "SELECT COUNT(*) FROM entity_operations WHERE status='processing'"
            ).fetchone()[0]

        self.assertEqual(before, after)
        self.assertEqual(0, incomplete_operations)

    def test_multiple_active_duplicates_publish_lowest_ready_price(self):
        listings = [
            row("agent-a", price=45_000_000),
            row("agent-b", price=42_000_000),
            row("agent-c", price=44_000_000),
        ]
        verdict = {
            "usable": True, "reason_code": "ok", "confidence": "high",
            "reason": "", "source": "llm",
        }
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, listings, "v1", collector_client.content_hash,
            )
            entity_store.merge_listings(
                conn, "agent-a", "agent-b", "same apartment", "review-price-1",
            )
            entity_store.merge_listings(
                conn, "agent-a", "agent-c", "same apartment", "review-price-2",
            )
            for item in listings:
                cleaner_db.upsert_verdict(
                    conn, item["id"], collector_client.content_hash(item),
                    verdict, "v1",
                )
            pipeline.publish_clean_baseline(conn, listings, "v1")
            published = list(cleaner_db.iter_clean_baseline(conn))

        self.assertEqual(1, len(published))
        self.assertEqual("agent-b", published[0]["id"])
        self.assertEqual(42_000_000, published[0]["price"])
        self.assertEqual(3, published[0]["entity_active_listing_count"])
        self.assertEqual(42_000_000, published[0]["entity_active_min_price"])
        self.assertEqual(45_000_000, published[0]["entity_active_max_price"])
        self.assertEqual(0, published[0]["entity_relist_count"])

    def test_separate_market_periods_count_as_relist_after_late_merge(self):
        old = row(
            "old", status="missing",
            first_seen_at="2026-08-01T00:00:00+00:00",
            last_seen_at="2026-08-20T00:00:00+00:00",
            missing_detected_at="2026-08-21T00:00:00+00:00",
        )
        new = row(
            "new", status="active",
            first_seen_at="2026-09-10T00:00:00+00:00",
            last_seen_at="2026-10-05T00:00:00+00:00",
        )
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, [old, new], "v1", collector_client.content_hash,
            )
            entity_id, _ = entity_store.merge_listings(
                conn, "old", "new", "same apartment relisted", "period-test",
            )
            relists = conn.execute(
                "SELECT relist_count FROM property_entities WHERE entity_id=?",
                (entity_id,),
            ).fetchone()[0]

        self.assertEqual(1, relists)

    def test_unreviewed_new_member_does_not_replace_ready_canonical(self):
        old = row("checked", status="active", price=50_000_000,
                  last_seen_at="2026-09-01T00:00:00+00:00")
        new = row("waiting", status="active", price=30_000_000,
                  last_seen_at="2026-10-05T00:00:00+00:00")
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, [old, new], "v1", collector_client.content_hash,
            )
            entity_store.merge_listings(
                conn, "checked", "waiting", "reviewed duplicate", "review-2",
            )
            cleaner_db.upsert_verdict(
                conn, "checked", collector_client.content_hash(old),
                {"usable": True, "reason_code": "ok", "confidence": "high",
                 "reason": "", "source": "llm"}, "v1",
            )
            result = pipeline.publish_clean_baseline(conn, [old, new], "v1")
            published = list(cleaner_db.iter_clean_baseline(conn))

        self.assertEqual(1, result["published"])
        self.assertEqual(1, result["waiting"])
        self.assertEqual("checked", published[0]["id"])
        self.assertEqual(2, published[0]["entity_active_listing_count"])
        self.assertEqual(1, published[0]["entity_ready_active_listing_count"])
        self.assertEqual(50_000_000, published[0]["entity_active_min_price"])
        self.assertEqual(50_000_000, published[0]["entity_active_max_price"])

    def test_split_is_idempotent_and_preserves_lineage(self):
        rows = [row("a"), row("b"), row("c")]
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, rows, "v1", collector_client.content_hash,
            )
            original_c_entity = conn.execute(
                "SELECT entity_id FROM entity_members WHERE listing_id='c'"
            ).fetchone()[0]
            parent, _ = entity_store.merge_listings(
                conn, "a", "b", "first merge", "m1",
            )
            parent, _ = entity_store.merge_listings(
                conn, "a", "c", "second merge", "m2",
            )
            child, changed = entity_store.split_entity(
                conn, parent, ["c"], "false merge", "s1",
            )
            same_child, changed_again = entity_store.split_entity(
                conn, parent, ["c"], "false merge", "s1",
            )
            child_members = [r[0] for r in conn.execute(
                "SELECT listing_id FROM entity_members WHERE entity_id=?", (child,)
            )]
            lineage = conn.execute(
                "SELECT COUNT(*) FROM entity_lineage WHERE child_entity_id=?", (child,)
            ).fetchone()[0]
            resolved_old_c_entity = entity_store.resolve_alias(
                conn, original_c_entity,
            )

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(child, same_child)
        self.assertEqual(["c"], child_members)
        self.assertEqual(1, lineage)
        self.assertEqual(child, resolved_old_c_entity)


class EntityMatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = cleaner_db.DB_PATH
        cleaner_db.DB_PATH = os.path.join(self.temp.name, "matcher.db")

    def tearDown(self):
        cleaner_db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_far_coordinates_are_never_a_hard_conflict(self):
        first = row(
            "a", street="Кабанбай батыра", house_num="10",
            latitude=51.10, longitude=71.40,
        )
        second = row(
            "b", street="Кабанбай батыра", house_num="10",
            latitude=51.14, longitude=71.40,
        )
        self.assertNotIn("coordinates", entity_matcher.hard_conflicts(first, second))
        self.assertFalse(any(
            item["signal"] == "near_coordinates"
            for item in entity_matcher.positive_signals(first, second)
        ))

    def test_shadow_candidates_are_durable_and_never_auto_merge(self):
        rows = [
            row(
                "200", street="Сыганак", house_num="5", complex_id="ЖК Тест",
                latitude=51.1, longitude=71.4,
            ),
            row(
                "100", street="Сыганак", house_num="5", complex_id="ЖК Тест",
                latitude=51.1001, longitude=71.4001,
            ),
        ]
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, rows, "v1", collector_client.content_hash,
            )
            first = entity_matcher.refresh_shadow_candidates(conn, rows, "v1")
            second = entity_matcher.refresh_shadow_candidates(conn, rows, "v2")
            stored = list(conn.execute(
                "SELECT listing_id_low,listing_id_high,status,is_current,priority "
                "FROM entity_match_candidates"
            ))
            entities = conn.execute(
                "SELECT COUNT(*) FROM property_entities"
            ).fetchone()[0]
            total, review = entity_store.match_candidates_page(conn)

        self.assertEqual(1, first["current_candidates"])
        self.assertEqual(0, first["auto_merged"])
        self.assertEqual(1, second["current_candidates"])
        self.assertEqual([("100", "200", "awaiting_photo_analysis", 1, 87)], [
            tuple(item) for item in stored
        ])
        self.assertEqual(2, entities)
        self.assertEqual(1, total)
        self.assertEqual("100", review[0]["listing_low"]["id"])

    def test_photo_evidence_pair_is_canonical_and_versions_create_revisions(self):
        rows = [
            row("b", street="Test", house_num="1", photo_set_hash="photos-b"),
            row("a", street="Test", house_num="1", photo_set_hash="photos-a"),
        ]
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, rows, "v1", collector_client.content_hash,
            )
            entity_matcher.refresh_shadow_candidates(conn, rows, "v1")
            candidate = conn.execute(
                "SELECT * FROM entity_match_candidates WHERE is_current=1"
            ).fetchone()
            evidence = {
                "candidate_revision_hash": candidate["candidate_revision_hash"],
                "decision": "likely_same", "score": 0.91,
                "normalization_version": "n1", "fingerprint_version": "f1",
                "intra_listing_dedupe_version": "d1",
                "photo_role_model_version": "r1", "embedding_version": "e1",
                "photo_asset_stats_version": "s1", "matcher_version": "m1",
                "listing_a": {
                    "identity_hash": entity_matcher.identity_hash(rows[0]),
                    "photo_set_hash": "photos-b",
                    "photo_content_set_hash": "content-b",
                    "photo_evidence_set_hash": "evidence-b",
                },
                "listing_b": {
                    "identity_hash": entity_matcher.identity_hash(rows[1]),
                    "photo_set_hash": "photos-a",
                    "photo_content_set_hash": "content-a",
                    "photo_evidence_set_hash": "evidence-a",
                },
                "matches": [{"room": "kitchen", "same_room": True}],
            }
            first = entity_store.record_photo_evidence(conn, "b", "a", evidence)
            repeat = entity_store.record_photo_evidence(conn, "b", "a", evidence)
            changed = entity_store.record_photo_evidence(
                conn, "a", "b", {**evidence, "fingerprint_version": "f2",
                                   "listing_a": evidence["listing_b"],
                                   "listing_b": evidence["listing_a"]},
            )
            refreshed = entity_matcher.refresh_shadow_candidates(conn, rows, "v2")
            revisions = [tuple(item) for item in conn.execute(
                "SELECT listing_id_low,listing_id_high,fingerprint_version,is_current "
                "FROM entity_match_edges ORDER BY fingerprint_version"
            )]
            entity_count = conn.execute(
                "SELECT COUNT(*) FROM property_entities"
            ).fetchone()[0]

        self.assertEqual(("a", "b"), (
            first["listing_id_low"], first["listing_id_high"],
        ))
        self.assertEqual(first["match_revision_hash"], repeat["match_revision_hash"])
        self.assertNotEqual(first["match_revision_hash"], changed["match_revision_hash"])
        self.assertEqual([
            ("a", "b", "f1", 0), ("a", "b", "f2", 1),
        ], revisions)
        self.assertEqual(1, refreshed["photo_evidence_ready"])
        self.assertEqual(2, entity_count)

    def test_stale_photo_evidence_is_rejected_after_photos_change(self):
        rows = [
            row("a", street="Test", house_num="1", photo_set_hash="old-a"),
            row("b", street="Test", house_num="1", photo_set_hash="old-b"),
        ]
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(conn, rows, "v1", collector_client.content_hash)
            entity_matcher.refresh_shadow_candidates(conn, rows, "v1")
            candidate = conn.execute(
                "SELECT * FROM entity_match_candidates WHERE is_current=1"
            ).fetchone()
            evidence = {
                "candidate_revision_hash": candidate["candidate_revision_hash"],
                "decision": "likely_same", "score": 0.9,
                "normalization_version": "n1", "fingerprint_version": "f1",
                "intra_listing_dedupe_version": "d1",
                "photo_role_model_version": "r1", "embedding_version": "e1",
                "photo_asset_stats_version": "s1", "matcher_version": "m1",
                "listing_a": {
                    "identity_hash": entity_matcher.identity_hash(rows[0]),
                    "photo_set_hash": "old-a",
                },
                "listing_b": {
                    "identity_hash": entity_matcher.identity_hash(rows[1]),
                    "photo_set_hash": "old-b",
                },
            }
            entity_store.record_photo_evidence(conn, "a", "b", evidence)
            changed = [dict(rows[0]), dict(rows[1])]
            changed[0]["photo_set_hash"] = "new-a"
            entity_store.observe_listings(
                conn, changed, "v2", collector_client.content_hash,
            )
            refreshed = entity_matcher.refresh_shadow_candidates(conn, changed, "v2")
            with self.assertRaises(entity_store.StalePhotoEvidenceError):
                entity_store.record_photo_evidence(conn, "a", "b", evidence)
            current_edges = conn.execute(
                "SELECT COUNT(*) FROM entity_match_edges WHERE is_current=1"
            ).fetchone()[0]
            conn.commit()

        self.assertEqual(1, refreshed["awaiting_photo_analysis"])
        self.assertEqual(0, current_edges)
        payload = verdicts_api.PhotoEvidenceRequest(
            listing_id_a="a", listing_id_b="b", evidence=evidence,
        )
        with self.assertRaises(HTTPException) as raised:
            verdicts_api.entity_match_evidence(payload)
        self.assertEqual(409, raised.exception.status_code)

    def test_manual_same_entity_decision_is_idempotent(self):
        rows = [row("left"), row("right")]
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, rows, "v1", collector_client.content_hash,
            )
            first = entity_store.apply_manual_match_decision(
                conn, "right", "left", "same_entity", "visual review",
                "tester", "review-1",
            )
            repeat = entity_store.apply_manual_match_decision(
                conn, "right", "left", "same_entity", "visual review",
                "tester", "review-1",
            )
            member_entities = {
                item[0] for item in conn.execute(
                    "SELECT entity_id FROM entity_members "
                    "WHERE listing_id IN ('left','right')"
                )
            }
            overrides = conn.execute(
                "SELECT COUNT(*) FROM entity_overrides"
            ).fetchone()[0]

        self.assertTrue(first["changed"])
        self.assertFalse(repeat["changed"])
        self.assertEqual(first["operation_id"], repeat["operation_id"])
        self.assertEqual(1, len(member_entities))
        self.assertEqual(1, overrides)

    def test_candidate_refresh_preserves_manual_override_status(self):
        rows = [
            row("left", street="Сыганак", house_num="5"),
            row("right", street="Сыганак", house_num="5"),
        ]
        with cleaner_db.connect() as conn:
            entity_store.observe_listings(
                conn, rows, "v1", collector_client.content_hash,
            )
            entity_matcher.refresh_shadow_candidates(conn, rows, "v1")
            entity_store.apply_manual_match_decision(
                conn, "left", "right", "different_entity", "different doors",
                "tester", "review-not-link-1",
            )
            summary = entity_matcher.refresh_shadow_candidates(conn, rows, "v2")
            status = conn.execute(
                "SELECT status FROM entity_match_candidates WHERE is_current=1"
            ).fetchone()[0]

        self.assertEqual("manual_not_link", status)
        self.assertEqual(1, summary["manual_not_link"])
        self.assertEqual(0, summary["awaiting_photo_analysis"])


class FullCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = cleaner_db.DB_PATH
        cleaner_db.DB_PATH = os.path.join(self.temp.name, "cycle.db")
        self.old_fetch = dict(pipeline._last_full_fetch)

    def tearDown(self):
        cleaner_db.DB_PATH = self.old_path
        pipeline._last_full_fetch.update(self.old_fetch)
        self.temp.cleanup()

    def test_mocked_cycle_applies_rule_without_network_or_ai(self):
        good = row("good")
        partial = row("partial", title="Продам долю в квартире")
        with cleaner_db.connect() as conn:
            cleaner_db.upsert_verdict(
                conn, "good", collector_client.content_hash(good),
                {"usable": True, "reason_code": "ok", "confidence": "high",
                 "reason": "", "source": "llm"}, "old",
                policy_version=openai_batch.POLICY_VERSION,
            )
            conn.commit()

        with mock.patch.object(
            collector_client, "fetch_all_rows", return_value=("collector-v2", [good, partial])
        ), mock.patch.object(openai_batch, "submit_batch") as paid_submit:
            result = pipeline.run_cycle()

        paid_submit.assert_not_called()
        self.assertEqual(1, result["published"])
        self.assertEqual(1, result["resolved_free"])
        with cleaner_db.connect() as conn:
            clean = list(cleaner_db.iter_clean_baseline(conn))
            verdict = conn.execute(
                "SELECT reason_code, policy_version FROM verdicts WHERE id='partial'"
            ).fetchone()
        self.assertEqual(["good"], [item["id"] for item in clean])
        self.assertEqual(("partial_property", "sales-astana-v1"), tuple(verdict))

    def test_unchanged_listing_is_submitted_only_once_while_batch_is_pending(self):
        listing = row("ai-needed")
        with mock.patch.object(
            collector_client, "fetch_all_rows", return_value=("collector-v1", [listing])
        ), mock.patch.object(openai_batch, "submit_batch", return_value="batch-1") as submit, \
                mock.patch.object(
                    openai_batch, "check_batch",
                    return_value=("in_progress", None, None, None),
                ):
            first = pipeline.run_cycle()
            second = pipeline.run_cycle()

        self.assertEqual(1, first["submitted_listings"])
        self.assertEqual(0, second["submitted_listings"])
        self.assertEqual(1, submit.call_count)
        with cleaner_db.connect() as conn:
            self.assertEqual({"ai-needed"}, cleaner_db.in_flight_ids(conn))
            batch = conn.execute(
                "SELECT group_size,model,policy_version FROM batches "
                "WHERE batch_id='batch-1'"
            ).fetchone()
        self.assertEqual(openai_batch.GROUP_SIZE, batch["group_size"])
        self.assertEqual(openai_batch.MODEL, batch["model"])
        self.assertEqual(openai_batch.POLICY_VERSION, batch["policy_version"])

    def test_temporary_collector_failure_keeps_previous_snapshot(self):
        existing = row("existing")
        with cleaner_db.connect() as conn:
            cleaner_db.upsert_verdict(
                conn, existing["id"], collector_client.content_hash(existing),
                {"usable": True, "reason_code": "ok", "confidence": "high",
                 "reason": "", "source": "llm"}, "collector-v1",
                policy_version=openai_batch.POLICY_VERSION,
            )
            conn.commit()
            pipeline.publish_clean_baseline(conn, [existing], "collector-v1")

        with mock.patch.object(
            collector_client, "fetch_all_rows", side_effect=requests.Timeout("temporary")
        ), mock.patch.object(pipeline, "ingest_completed_batches", return_value=0):
            result = pipeline.run_cycle()

        self.assertEqual("collector_unavailable", result["error"])
        with cleaner_db.connect() as conn:
            info = cleaner_db.clean_baseline_info(conn)
            published = list(cleaner_db.iter_clean_baseline(conn))
        self.assertEqual("collector-v1", info[1])
        self.assertEqual(["existing"], [item["id"] for item in published])


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self.payload


class CollectorPaginationTests(unittest.TestCase):
    def test_api_key_and_timeout_are_sent(self):
        response = _Response({
            "version": "v1", "total": 1, "rows": [collector_stub("1")],
        })
        with mock.patch.object(collector_client, "COLLECTOR_URL", "https://collector"), \
                mock.patch.object(collector_client, "COLLECTOR_API_KEY", "test-key"), \
                mock.patch.object(
                    collector_client.requests, "get", return_value=response,
                ) as get:
            collector_client.fetch_all_rows()

        _args, kwargs = get.call_args
        self.assertEqual({"X-API-Key": "test-key"}, kwargs["headers"])
        self.assertEqual(60, kwargs["timeout"])

    def test_table_http_errors_are_not_silenced(self):
        for status in (401, 404, 503):
            with self.subTest(status=status), \
                    mock.patch.object(collector_client, "COLLECTOR_URL", "https://collector"), \
                    mock.patch.object(
                        collector_client.requests, "get",
                        return_value=_Response({"detail": "error"}, status),
                    ):
                with self.assertRaises(requests.HTTPError):
                    collector_client.fetch_all_rows()

    def test_meta_503_has_actionable_error(self):
        with mock.patch.object(collector_client, "COLLECTOR_URL", "https://collector"), \
                mock.patch.object(
                    collector_client.requests, "get",
                    return_value=_Response({"detail": "snapshot is not ready"}, 503),
                ):
            with self.assertRaisesRegex(collector_client.CollectorError, "snapshot is not ready"):
                collector_client.fetch_meta()

    def test_short_page_never_becomes_a_truncated_snapshot(self):
        incomplete = _Response({
            "version": "v1", "total": 2, "rows": [collector_stub("only-one")],
        })
        with mock.patch.object(collector_client, "COLLECTOR_URL", "https://collector"), \
                mock.patch.object(collector_client, "PAGE_SIZE", 500), \
                mock.patch.object(
                    collector_client.requests, "get", return_value=incomplete,
                ) as get:
            with self.assertRaises(collector_client.CollectorError):
                collector_client.fetch_all_rows()

        self.assertEqual(3, get.call_count)

    def test_inconsistent_photo_identity_rejects_snapshot(self):
        broken = collector_stub("broken")
        broken["photo_count"] = 1
        response = _Response({"version": "v1", "total": 1, "rows": [broken]})
        with mock.patch.object(collector_client, "COLLECTOR_URL", "https://collector"), \
                mock.patch.object(collector_client.requests, "get", return_value=response):
            with self.assertRaisesRegex(collector_client.CollectorError, "photo_urls"):
                collector_client.fetch_all_rows()

    def test_version_change_restarts_from_zero(self):
        pages = [
            {"version": "old", "total": 501,
             "rows": [collector_stub(i) for i in range(500)]},
            {"version": "new", "total": 1, "rows": [collector_stub("new")]},
            {"version": "new", "total": 1, "rows": [collector_stub("new")]},
        ]
        offsets = []

        def fake_get(_url, params, **_kwargs):
            offsets.append(params["offset"])
            return _Response(pages.pop(0))

        with mock.patch.object(collector_client, "COLLECTOR_URL", "https://collector"), \
                mock.patch.object(collector_client.requests, "get", side_effect=fake_get):
            version, rows = collector_client.fetch_all_rows()

        self.assertEqual("new", version)
        self.assertEqual(["new"], [item["id"] for item in rows])
        self.assertEqual([0, 500, 0], offsets)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = cleaner_db.DB_PATH
        cleaner_db.DB_PATH = os.path.join(self.temp.name, "health.db")

    def tearDown(self):
        cleaner_db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_ingest_tick_does_not_replace_full_cycle_freshness(self):
        verdicts_api.record_full_cycle_result({"published": 10})
        verdicts_api.record_ingest_tick_result({"tick": True, "ingested": 2})

        response = verdicts_api.health()
        body = json.loads(response.body)
        self.assertEqual(200, response.status_code)
        self.assertEqual("ok", body["status"])
        self.assertEqual({"published": 10}, body["last_full_success_result"])
        self.assertEqual(
            {"tick": True, "ingested": 2},
            body["last_ingest_tick_result"],
        )

    def test_failed_attempt_is_degraded_without_replacing_success(self):
        verdicts_api.record_full_cycle_result({"published": 10})
        verdicts_api.record_full_cycle_result({"error": "collector_unavailable"})

        response = verdicts_api.health()
        body = json.loads(response.body)
        self.assertEqual(200, response.status_code)
        self.assertEqual("degraded", body["status"])
        self.assertEqual({"published": 10}, body["last_full_success_result"])

    def test_successful_health_access_log_is_hidden_only_for_health(self):
        access_filter = service._HideHealthAccessLog()
        health = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", "/health", "1.1", 200), None,
        )
        unhealthy = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", "/health", "1.1", 503), None,
        )
        self.assertFalse(access_filter.filter(health))
        self.assertTrue(access_filter.filter(unhealthy))


if __name__ == "__main__":
    unittest.main()
