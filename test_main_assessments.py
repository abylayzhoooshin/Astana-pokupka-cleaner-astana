import json
import os
import tempfile
import unittest
from unittest import mock

import cleaner_db
import collector_client
import main_assessments
import main_photo_sync
import pipeline


class MainAssessmentTests(unittest.TestCase):
    def _context(self, row):
        return {"price": row["price"], "photo_set_hash": row.get("photo_set_hash"),
                "furniture": row.get("furniture")}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = cleaner_db.DB_PATH
        cleaner_db.DB_PATH = os.path.join(self.temp.name, "cleaner.db")
        self.row = {
            "id": "42", "title": "Квартира", "full_description": "Продажа",
            "rent_renovation": "хорошее", "priv_dorm": None,
            "square_m2": 60.0, "rooms": 2, "price": 42_000_000,
            "seller_type": "professional",
            "baseline_eligible": 1, "photo_urls": ["https://example.test/a.jpg"],
        }
        self.row["photo_set_hash"] = collector_client.photo_set_hash(self.row["photo_urls"])

    def tearDown(self):
        cleaner_db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_professional_only_even_when_collector_marks_owner_eligible(self):
        rows = [
            self.row,
            {**self.row, "id": "owner", "seller_type": "owner"},
            {**self.row, "id": "unknown", "seller_type": ""},
        ]
        self.assertEqual(["42"], [r["id"] for r in pipeline._comparison_rows(rows)])

    def test_direct_publish_cannot_smuggle_owner_into_price_baseline(self):
        professional = {**self.row, "photo_urls": "[]", "photo_set_hash": None}
        owner = {**professional, "id": "owner", "seller_type": "owner"}
        with cleaner_db.connect() as conn:
            for listing in (professional, owner):
                cleaner_db.upsert_verdict(
                    conn, listing["id"], collector_client.content_hash(listing),
                    {"usable": True, "reason_code": "ok", "confidence": "high",
                     "reason": "", "source": "llm"}, "v1",
                )
            conn.commit()
            pipeline.publish_clean_baseline(conn, [professional, owner], "v1")
            actual = [r["id"] for r in cleaner_db.iter_clean_baseline(conn)]
        self.assertEqual(["42"], actual)

    def test_exact_text_photo_revision_and_version_required(self):
        payload = {
            "listing_id": "42",
            "text": {
                "content_hash": collector_client.content_hash(self.row),
                "evaluator_version": "sale-text-v1",
                "result": {"category": "whole_apartment", "source": "text"},
            },
            "photo": {
                "photo_set_hash": self.row["photo_set_hash"],
                "evaluator_version": "sale-photo-v1",
                "result": {"status": "ready", "score": 7.1, "coverage": "partial"},
            },
        }
        with cleaner_db.connect() as conn:
            main_assessments.save(conn, payload)
            main_assessments.save(conn, payload)
            conn.commit()
            same = main_assessments.current_for_row(
                conn, self.row, text_version="sale-text-v1", photo_version="sale-photo-v1"
            )
            self.assertEqual("whole_apartment", json.loads(same["main_text_assessment"])["category"])
            self.assertEqual(7.1, same["main_photo_score"])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM main_text_assessments").fetchone()[0])
            changed_text = {**self.row, "full_description": "Описание изменилось"}
            changed_photo = {**self.row, "photo_urls": [], "photo_set_hash": None}
            self.assertEqual("", main_assessments.current_for_row(
                conn, changed_text, text_version="sale-text-v1", photo_version=""
            )["main_text_assessment"])
            self.assertEqual("", main_assessments.current_for_row(
                conn, changed_photo, text_version="", photo_version="sale-photo-v1"
            )["main_photo_status"])
            self.assertEqual("", main_assessments.current_for_row(
                conn, self.row, text_version="sale-text-v2", photo_version="sale-photo-v2"
            )["main_text_assessment"])

    def test_furnishing_classes_are_preserved_in_current_baseline_evidence(self):
        profile = {
            "offer_type": "whole_apartment", "condition_class": "renovated",
            "furniture_level": "partial", "appliance_level": "none",
            "furniture_terms": "extra_price", "appliance_terms": "unknown",
            "photo_scope": "unknown", "claims": [],
        }
        payload = {
            "listing_id": self.row["id"],
            "text": {
                "content_hash": collector_client.content_hash(self.row),
                "evaluator_version": main_assessments.DEFAULT_TEXT_VERSION,
                "result": {"sale_profile": profile},
            },
        }
        with cleaner_db.connect() as conn:
            main_assessments.save(conn, payload)
            conn.commit()
            enriched = main_assessments.current_for_row(
                conn, self.row, text_version=main_assessments.DEFAULT_TEXT_VERSION,
                photo_version="",
            )
        self.assertEqual(profile, json.loads(enriched["main_text_assessment"])["sale_profile"])

    def test_missing_assessment_never_filters_or_invents_score(self):
        with cleaner_db.connect() as conn, mock.patch.dict(os.environ, {
            "SALE_TEXT_EVALUATOR_VERSION": "sale-text-v1",
            "SALE_PHOTO_EVALUATOR_VERSION": "sale-photo-v1",
        }):
            rows = main_assessments.enrich_rows(conn, [self.row])
        self.assertEqual(1, len(rows))
        self.assertEqual("", rows[0]["main_text_assessment"])
        self.assertEqual("", rows[0]["main_photo_score"])

    def test_main_comparison_reused_only_for_exact_revision_and_policy(self):
        row = {**self.row, "full_description": "Продаётся комната в квартире"}
        result = {
            "sale_profile": {"offer_type": "partial_property", "claims": []},
            "comparison_policy_version": main_assessments.COMPARISON_POLICY_VERSION,
            "assessment_context": self._context(row),
            "comparison_verdict": {
                "usable": False, "reason_code": "partial_property",
                "confidence": "high", "evidence": "комната в квартире",
            },
        }
        with cleaner_db.connect() as conn:
            main_assessments.save(conn, {
                "listing_id": row["id"], "text": {
                    "content_hash": collector_client.content_hash(row),
                    "evaluator_version": main_assessments.DEFAULT_TEXT_VERSION,
                    "result": result,
                },
            })
            conn.commit()
            verdict = main_assessments.comparison_for_row(conn, row)
            self.assertEqual("partial_property", verdict["reason_code"])
            remainder, reused = pipeline.apply_main_comparison(
                conn, [(row, collector_client.content_hash(row))], "v1",
            )
            self.assertEqual(([], 1), (remainder, reused))
            self.assertEqual("main", cleaner_db.verdict_source_map(conn)[row["id"]][0])
            self.assertIsNone(main_assessments.comparison_for_row(
                conn, {**row, "full_description": "Продажа целой квартиры"},
            ))
            result["comparison_policy_version"] = "old-policy"
            main_assessments.save(conn, {
                "listing_id": row["id"], "text": {
                    "content_hash": collector_client.content_hash(row),
                    "evaluator_version": main_assessments.DEFAULT_TEXT_VERSION,
                    "result": result,
                },
            })
            self.assertIsNone(main_assessments.comparison_for_row(conn, row))

    def test_main_comparison_falls_back_on_missing_evidence_or_conflict(self):
        row = {**self.row, "full_description": "Обычная квартира"}
        for verdict in (
            {"usable": False, "reason_code": "partial_property", "confidence": "high", "evidence": "комната в квартире"},
            {"usable": True, "reason_code": "ok", "confidence": "high", "evidence": ""},
        ):
            result = {
                "sale_profile": {"offer_type": "partial_property", "claims": []},
                "comparison_policy_version": main_assessments.COMPARISON_POLICY_VERSION,
                "assessment_context": self._context(row),
                "comparison_verdict": verdict,
            }
            with cleaner_db.connect() as conn:
                main_assessments.save(conn, {
                    "listing_id": row["id"], "text": {
                        "content_hash": collector_client.content_hash(row),
                        "evaluator_version": main_assessments.DEFAULT_TEXT_VERSION,
                        "result": result,
                    },
                })
                self.assertIsNone(main_assessments.comparison_for_row(conn, row))

    def test_multi_unit_advertisement_is_not_auto_accepted_as_price_comparable(self):
        row = {**self.row, "full_description": "В продаже несколько квартир разной площади"}
        with cleaner_db.connect() as conn:
            main_assessments.save(conn, {
                "listing_id": row["id"], "text": {
                    "content_hash": collector_client.content_hash(row),
                    "evaluator_version": main_assessments.DEFAULT_TEXT_VERSION,
                    "result": {
                        "sale_profile": {"offer_type": "multi_unit_offer", "claims": []},
                        "comparison_policy_version": main_assessments.COMPARISON_POLICY_VERSION,
                        "assessment_context": self._context(row),
                        "comparison_verdict": {
                            "usable": True, "reason_code": "ok", "confidence": "low", "evidence": "",
                        },
                    },
                },
            })
            self.assertIsNone(main_assessments.comparison_for_row(conn, row))

    def test_ambiguous_multi_unit_main_result_is_excluded_from_price_baseline(self):
        row = {**self.row, "full_description": "Продаются квартиры разной площади и этажности"}
        with cleaner_db.connect() as conn:
            main_assessments.save(conn, {
                "listing_id": row["id"], "text": {
                    "content_hash": collector_client.content_hash(row),
                    "evaluator_version": main_assessments.DEFAULT_TEXT_VERSION,
                    "result": {
                        "sale_profile": {"offer_type": "multi_unit_offer", "photo_scope": "mixed_units"},
                        "comparison_policy_version": main_assessments.COMPARISON_POLICY_VERSION,
                        "assessment_context": self._context(row),
                        "comparison_verdict": {"usable": None, "reason_code": "unclear",
                                               "confidence": "medium", "evidence": ""},
                    },
                },
            })
            conn.commit()
            verdict = main_assessments.comparison_for_row(conn, row)
            self.assertEqual("unit_ambiguous", verdict["reason_code"])
            remainder, reused = pipeline.apply_main_comparison(
                conn, [(row, collector_client.content_hash(row))], "v1",
            )
            self.assertEqual(([], 1), (remainder, reused))
            pipeline.publish_clean_baseline(conn, [row], "v1")
            self.assertEqual([], list(cleaner_db.iter_clean_baseline(conn)))

    def test_changed_price_cannot_reuse_main_photo_text_verdict(self):
        row = dict(self.row)
        result = {
            "sale_profile": {"offer_type": "whole_apartment", "photo_scope": "one_unit"},
            "assessment_context": self._context(row),
            "comparison_policy_version": main_assessments.COMPARISON_POLICY_VERSION,
            "comparison_verdict": {"usable": True, "reason_code": "ok",
                                   "confidence": "high", "evidence": ""},
        }
        with cleaner_db.connect() as conn:
            main_assessments.save(conn, {
                "listing_id": row["id"], "text": {
                    "content_hash": collector_client.content_hash(row),
                    "evaluator_version": main_assessments.DEFAULT_TEXT_VERSION,
                    "result": result,
                },
            })
            pipeline.apply_main_comparison(conn, [(row, collector_client.content_hash(row))], "v1")
            pipeline.publish_clean_baseline(conn, [row], "v1")
            changed = {**row, "price": row.get("price", 40_000_000) - 1_000_000}
            self.assertEqual(collector_client.content_hash(row), collector_client.content_hash(changed))
            self.assertIsNone(main_assessments.comparison_for_row(conn, changed))
            published = pipeline.publish_clean_baseline(conn, [changed], "v2")
            self.assertEqual("empty_result", published["publish_skipped"])
            self.assertEqual("v1", cleaner_db.clean_baseline_info(conn)[1])

    def test_late_main_ambiguity_overrides_old_cleaner_acceptance(self):
        row = {**self.row, "full_description": "Продаются несколько разных квартир"}
        good = {**self.row, "id": "43", "full_description": "Одна конкретная квартира"}
        with cleaner_db.connect() as conn:
            for listing in (row, good):
                cleaner_db.upsert_verdict(
                    conn, listing["id"], collector_client.content_hash(listing),
                    {"usable": True, "reason_code": "ok", "confidence": "low",
                     "reason": "", "source": "llm"}, "v1",
                )
            conn.commit()
            pipeline.publish_clean_baseline(conn, [row, good], "v1")
            main_assessments.save(conn, {
                "listing_id": row["id"], "text": {
                    "content_hash": collector_client.content_hash(row),
                    "evaluator_version": main_assessments.DEFAULT_TEXT_VERSION,
                    "result": {
                        "sale_profile": {"offer_type": "multi_unit_offer", "photo_scope": "mixed_units"},
                        "assessment_context": self._context(row),
                        "comparison_policy_version": main_assessments.COMPARISON_POLICY_VERSION,
                        "comparison_verdict": {"usable": None, "reason_code": "unclear",
                                               "confidence": "medium", "evidence": ""},
                    },
                },
            })
            conn.commit()
            pipeline.publish_clean_baseline(conn, [row, good], "v2")
            self.assertEqual("unit_ambiguous",
                             cleaner_db.verdict_source_map(conn)[row["id"]][2])
            self.assertEqual(["43"], [r["id"] for r in cleaner_db.iter_clean_baseline(conn)])

    def test_unclear_main_type_falls_back_to_cleaner_classifier(self):
        row = {**self.row, "full_description": ""}
        with cleaner_db.connect() as conn:
            main_assessments.save(conn, {
                "listing_id": row["id"], "text": {
                    "content_hash": collector_client.content_hash(row),
                    "evaluator_version": main_assessments.DEFAULT_TEXT_VERSION,
                    "result": {
                        "sale_profile": {"offer_type": "unclear", "claims": []},
                        "comparison_policy_version": main_assessments.COMPARISON_POLICY_VERSION,
                        "assessment_context": self._context(row),
                        "comparison_verdict": {
                            "usable": None, "reason_code": "unclear", "confidence": "low",
                            "evidence": "",
                        },
                    },
                },
            })
            self.assertIsNone(main_assessments.comparison_for_row(conn, row))
            remainder, reused = pipeline.apply_main_comparison(
                conn, [(row, collector_client.content_hash(row))], "v1",
            )
            self.assertEqual(0, reused)
            self.assertEqual(1, len(remainder))

    def test_reject_invalid_photo_score(self):
        with cleaner_db.connect() as conn:
            with self.assertRaises(ValueError):
                main_assessments.save(conn, {
                    "listing_id": "42", "photo": {
                        "photo_set_hash": self.row["photo_set_hash"],
                        "evaluator_version": "sale-photo-v1",
                        "result": {"status": "ready", "score": 99, "coverage": "sufficient"},
                    },
                })

    def test_photo_sync_commits_before_ack_and_reuses_exact_photo(self):
        report = {
            "summary": "Visible finish is good, furniture is not a fixture.",
            "evidence": [{"photo_ids": ["p1"], "observation": "Finished walls and floor."}],
            "limitations": ["The bathroom is not shown."],
            "visible_furniture": "partial",
            "visible_appliances": "unknown",
        }
        evaluation = {
            "evaluation_id": 1, "listing_id": "42",
            "photo_set_hash": self.row["photo_set_hash"],
            "evaluator_version": main_assessments.DEFAULT_PHOTO_VERSION,
            "status": "ready", "score": 7.1, "coverage": "sufficient",
            "report": report,
        }
        calls = []

        def fake_request(method, url, key, **kwargs):
            calls.append(method)
            if method == "GET":
                return {"results": [evaluation], "count": 1,
                        "has_more": False, "next_after_id": 1}
            with cleaner_db.connect() as independent:
                stored = independent.execute(
                    "SELECT COUNT(*) FROM main_photo_assessments"
                ).fetchone()[0]
            self.assertEqual(1, stored, "ACK happened before durable commit")
            return {"requested": 1, "acknowledged": 1,
                    "already_acknowledged": 0, "missing": 0, "not_ready": 0}

        with mock.patch.dict(os.environ, {
            "MAINRIELTOR_PHOTO_SYNC_ENABLED": "1", "MAINRIELTOR_URL": "http://main.test",
            "MAINRIELTOR_INTERIOR_API_KEY": "test-key",
        }), mock.patch.object(main_photo_sync, "_request", side_effect=fake_request):
            with cleaner_db.connect() as conn:
                result = main_photo_sync.sync(conn)
                enriched = main_assessments.enrich_rows(conn, [self.row])[0]
        self.assertEqual(["GET", "POST"], calls)
        self.assertEqual(1, result["acknowledged"])
        self.assertEqual(7.1, enriched["interior_score"])
        self.assertEqual(report, json.loads(enriched["main_photo_report"]))
        self.assertEqual(main_assessments.DEFAULT_PHOTO_VERSION,
                         enriched["interior_evaluator_version"])

    def test_photo_report_does_not_follow_changed_photo_revision(self):
        report = {"summary": "Finished walls", "evidence": [], "limitations": []}
        with cleaner_db.connect() as conn:
            main_assessments.save(conn, {
                "listing_id": self.row["id"], "photo": {
                    "photo_set_hash": self.row["photo_set_hash"],
                    "evaluator_version": main_assessments.DEFAULT_PHOTO_VERSION,
                    "result": {"status": "ready", "score": 6.0,
                               "coverage": "partial", "report": report},
                },
            })
            changed = {**self.row, "photo_urls": ["https://example.test/new.jpg"]}
            changed["photo_set_hash"] = collector_client.photo_set_hash(changed["photo_urls"])
            current = main_assessments.current_for_row(
                conn, changed, text_version="",
                photo_version=main_assessments.DEFAULT_PHOTO_VERSION,
            )
            self.assertEqual("", current["main_photo_report"])
            with self.assertRaises(ValueError):
                main_assessments.save(conn, {
                    "listing_id": self.row["id"], "photo": {
                        "photo_set_hash": self.row["photo_set_hash"],
                        "evaluator_version": main_assessments.DEFAULT_PHOTO_VERSION,
                        "result": {"status": "ready", "score": 6.0,
                                   "coverage": "partial", "report": {
                                       **report, "photo_urls": ["https://example.test/a.jpg"],
                                   }},
                    },
                })


if __name__ == "__main__":
    unittest.main()
