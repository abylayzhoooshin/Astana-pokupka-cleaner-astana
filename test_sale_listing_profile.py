import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import cleaner_db
import sale_listing_profile as profiles
import verdicts_api


def codes(row):
    return {signal["code"] for signal in profiles.classify(row)["signals"]}


class TextProfileTests(unittest.TestCase):
    def test_structured_fields_beat_missing_description(self):
        row = {"rent_renovation": "черновая отделка", "furniture": "без мебели"}
        self.assertEqual(codes(row), {"finish_rough", "furniture_none"})

    def test_independent_deal_and_condition_claims(self):
        row = {"full_description": (
            "Дом еще строится, продажа по переуступке. Только наличные. "
            "Квартира в предчистовой отделке, проживают арендаторы."
        )}
        self.assertEqual(codes(row), {
            "under_construction", "assignment_sale", "cash_only",
            "finish_pre_finish", "tenant_occupied",
        })

    def test_ordinary_mortgage_not_a_problem(self):
        row = {"full_description": "Ипотека проходит. Вся мебель остается."}
        self.assertEqual(codes(row), {"mortgage_possible_claim", "furniture_full"})
        self.assertNotIn("legal_issue_claim", codes(row))

    def test_marketing_price_per_m2_is_not_partial_sale_price(self):
        self.assertNotIn("partial_price_claim", codes({
            "full_description": "Лучшая цена за м² — отличный вариант для покупки!"}))

    def test_new_repair_needed_is_not_completed_repair(self):
        self.assertNotIn("finish_fresh", codes({
            "full_description": "Нужен новый ремонт; текущий старый."}))

    def test_negation_and_generic_repair_are_not_evidence(self):
        row = {"full_description": (
            "Не дизайнерский ремонт. После черновой отделки всё переделали. "
            "Нужен косметический ремонт. "
            "Без арестов и обременений. Без переуступки. Возможен торг."
        )}
        self.assertIn("finish_needs_repair", codes(row))
        self.assertNotIn("finish_designer_claim", codes(row))
        self.assertNotIn("finish_rough", codes(row))
        self.assertNotIn("legal_issue_claim", codes(row))
        self.assertNotIn("auction_sale", codes(row))
        self.assertNotIn("assignment_sale", codes(row))

    def test_damage_explicit_but_no_rejection(self):
        self.assertIn("water_damage_claim", codes({
            "full_description": "Квартиру затопило; после потопа требует ремонта."}))

    def test_rare_but_material_sale_variants(self):
        self.assertEqual(codes({"full_description": (
            "Дом под снос. Квартира после пожара, ремонт не закончен. "
            "Продаётся с действующим арендатором."
        )}), {"demolition_claim", "fire_damage_claim", "finish_unfinished",
              "tenant_occupied"})

    def test_conflicting_claims_are_visible_not_silently_resolved(self):
        profile = profiles.classify({"rent_renovation": "черновая отделка",
                                     "full_description": "Ремонт не требуется"})
        self.assertEqual(profile["conflicts"], [])
        profile = profiles.classify({"rent_renovation": "черновая отделка",
                                     "full_description": "Квартира в предчистовой отделке"})
        self.assertEqual(profile["conflicts"], ["finish"])

    def test_asking_price_status_not_in_profile_identity(self):
        row = {"title": "Квартира", "full_description": "Дом сдан в эксплуатацию"}
        original = profiles.profile_hash(row)
        self.assertEqual(original, profiles.profile_hash({**row, "price": 20000000,
                                                         "status": "missing"}))
        self.assertNotEqual(original, profiles.profile_hash({**row, "furniture": "полностью"}))

    def test_old_database_migration_preserves_verdict_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "cleaner.db")
            with patch.object(cleaner_db, "DB_PATH", db):
                with cleaner_db.connect() as conn:
                    conn.execute("INSERT INTO verdicts(id,content_hash,usable,processed_at) "
                                 "VALUES ('old','hash',1,'2026-10-01')")
                    conn.commit()
                with cleaner_db.connect() as conn:
                    changed = cleaner_db.update_text_profiles(conn, [{
                        "id": "old", "full_description": "Квартира в черновой отделке",
                    }])
                    conn.commit()
                    self.assertEqual(changed, 1)
                    self.assertEqual(cleaner_db.update_text_profiles(conn, [{
                        "id": "old", "full_description": "Квартира в черновой отделке",
                    }]), 0)
                    self.assertEqual(conn.execute(
                        "SELECT usable FROM verdicts WHERE id='old'").fetchone()[0], 1)
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM listing_text_profiles").fetchone()[0], 1)
                    self.assertEqual(cleaner_db.SCHEMA_VERSION,
                                     conn.execute("PRAGMA user_version").fetchone()[0])

    def test_api_returns_claims_not_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "cleaner.db")
            with patch.object(cleaner_db, "DB_PATH", db):
                with cleaner_db.connect() as conn:
                    cleaner_db.update_text_profiles(conn, [{"id": "123", "furniture": "полностью"}])
                    conn.commit()
                result = verdicts_api.text_profile("123")
                self.assertEqual(result["profile"]["signals"][0]["code"], "furniture_full")
                page = verdicts_api.text_profiles(limit=10, offset=0)
                self.assertEqual(page["total"], 1)


if __name__ == "__main__":
    unittest.main()
