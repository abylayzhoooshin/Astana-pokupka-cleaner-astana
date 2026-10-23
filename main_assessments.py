"""Versioned Main -> Cleaner evidence, separate from price eligibility verdicts.

An assessment is evidence about one exact revision of a listing, not permission
to publish it or a replacement for the Cleaner sale-price validity check.
"""

import json
import math
import os
import re
from decimal import Decimal, InvalidOperation

import collector_client
import cleaner_db

_TEXT_HASH = re.compile(r"[0-9a-f]{16}\Z")
_PHOTO_HASH = re.compile(r"[0-9a-f]{64}\Z")
_COVERAGE = {"sufficient", "partial", "insufficient"}
_PHOTO_STATUS = {"ready", "ready_with_download_gaps", "no_photos", "photos_unavailable"}
DEFAULT_TEXT_VERSION = "sale-astana-stage2-v7-interior-score-clarity"
DEFAULT_PHOTO_VERSION = "sale-astana-renovation-v13-original-scale-gpt6-luna"
COMPARISON_POLICY_VERSION = "sales-astana-comparable-v4"


def _version(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError("invalid evaluator_version")
    return value


def _listing_id(value):
    listing_id = str(value or "").strip()
    if not listing_id or len(listing_id) > 80:
        raise ValueError("invalid listing_id")
    return listing_id


def _json_object(value):
    if not isinstance(value, dict):
        raise ValueError("result must be an object")
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) > 16_384:
        raise ValueError("result too large")
    return raw


def _photo_report(value):
    if value is None:
        return None  # Older Main deployments sent only score and coverage.
    required = {"summary", "evidence", "limitations"}
    optional = {"visible_furniture", "visible_appliances"}
    if not isinstance(value, dict) or not required <= set(value) or not set(value) <= required | optional:
        raise ValueError("invalid photo report")
    visibility = {"none", "partial", "substantial", "unknown"}
    if any(value.get(field) not in visibility for field in optional if field in value):
        raise ValueError("invalid visible furniture or appliances")
    summary, evidence, limitations = (value[key] for key in ("summary", "evidence", "limitations"))
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 1500:
        raise ValueError("invalid photo report summary")
    if not isinstance(evidence, list) or len(evidence) > 4 or not isinstance(limitations, list) or len(limitations) > 3:
        raise ValueError("invalid photo report items")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"photo_ids", "observation"}:
            raise ValueError("invalid photo evidence")
        # Main validates every reference against photos actually sent to the
        # model.  Some large albums legitimately support one observation with
        # more than ten frames; the 16 KiB result limit above is the payload
        # bound, so Cleaner must not impose a conflicting per-observation cap.
        if not isinstance(item["photo_ids"], list) or not item["photo_ids"]:
            raise ValueError("invalid photo evidence ids")
        if not all(isinstance(pid, str) and len(pid) <= 80 for pid in item["photo_ids"]):
            raise ValueError("invalid photo evidence ids")
        if not isinstance(item["observation"], str) or not item["observation"].strip() or len(item["observation"]) > 1000:
            raise ValueError("invalid photo observation")
    if not all(isinstance(note, str) and note.strip() and len(note) <= 1000 for note in limitations):
        raise ValueError("invalid photo limitations")
    return value


def save(conn, payload):
    """Idempotent upsert; text and photo revisions are independent."""
    if not isinstance(payload, dict):
        raise ValueError("assessment must be an object")
    listing_id = _listing_id(payload.get("listing_id"))
    text = payload.get("text")
    photo = payload.get("photo")
    if text is None and photo is None:
        raise ValueError("text or photo result required")
    if text is not None:
        if not isinstance(text, dict) or not _TEXT_HASH.fullmatch(str(text.get("content_hash") or "")):
            raise ValueError("invalid text content_hash")
        version = _version(text.get("evaluator_version"))
        encoded = _json_object(text.get("result"))
        conn.execute(
            """INSERT INTO main_text_assessments
               (listing_id, content_hash, evaluator_version, result_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(listing_id, content_hash, evaluator_version)
               DO UPDATE SET result_json=excluded.result_json, updated_at=excluded.updated_at""",
            (listing_id, text["content_hash"], version, encoded, cleaner_db.utcnow_iso()),
        )
    if photo is not None:
        if not isinstance(photo, dict):
            raise ValueError("invalid photo result")
        photo_hash = photo.get("photo_set_hash")
        if photo_hash is not None and not _PHOTO_HASH.fullmatch(str(photo_hash)):
            raise ValueError("invalid photo_set_hash")
        version = _version(photo.get("evaluator_version"))
        result = photo.get("result")
        if not isinstance(result, dict) or result.get("status") not in _PHOTO_STATUS:
            raise ValueError("invalid photo status")
        score, coverage = result.get("score"), result.get("coverage")
        if result["status"] in {"no_photos", "photos_unavailable"}:
            if score is not None or coverage is not None:
                raise ValueError("unavailable photos cannot have a score")
        elif coverage == "insufficient":
            if score is not None:
                raise ValueError("insufficient coverage cannot have a score")
        elif coverage in {"partial", "sufficient"}:
            if type(score) not in (int, float) or not math.isfinite(score) or not 1 <= score <= 10:
                raise ValueError("invalid photo score")
        else:
            raise ValueError("invalid photo coverage")
        report = _photo_report(result.get("report"))
        if result["status"] in {"no_photos", "photos_unavailable"} and report is not None:
            raise ValueError("unavailable photos cannot have a report")
        encoded = _json_object(result)
        conn.execute(
            """INSERT INTO main_photo_assessments
               (listing_id, photo_hash_key, evaluator_version, result_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(listing_id, photo_hash_key, evaluator_version)
               DO UPDATE SET result_json=excluded.result_json, updated_at=excluded.updated_at""",
            (listing_id, photo_hash or "", version, encoded, cleaner_db.utcnow_iso()),
        )
    return {"listing_id": listing_id, "text_saved": text is not None, "photo_saved": photo is not None}


def current_for_row(conn, row, *, text_version, photo_version):
    """Return only evidence matching the current Collector source revision."""
    result = {
        "main_text_assessment": "",
        "main_photo_status": "",
        "main_photo_score": "",
        "main_photo_coverage": "",
        "main_photo_report": "",
    }
    listing_id = _listing_id(row.get("id"))
    if text_version:
        hit = conn.execute(
            """SELECT result_json FROM main_text_assessments
               WHERE listing_id=? AND content_hash=? AND evaluator_version=?""",
            (listing_id, collector_client.content_hash(row), text_version),
        ).fetchone()
        if hit:
            result["main_text_assessment"] = hit[0]
    if photo_version:
        raw_urls = row.get("photo_urls")
        try:
            urls = json.loads(raw_urls) if isinstance(raw_urls, str) else (raw_urls or [])
        except json.JSONDecodeError:
            return result
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            return result
        photo_hash = collector_client.photo_set_hash(urls)
        # The full Collector fetch rejects inconsistent snapshots upstream.
        # This local enrichment must still be fail-open for direct rebuilds
        # and legacy rows: do not attach a score to mismatched photo identity.
        if (row.get("photo_set_hash") or None) == photo_hash:
            hit = conn.execute(
                """SELECT result_json FROM main_photo_assessments
                   WHERE listing_id=? AND photo_hash_key=? AND evaluator_version=?""",
                (listing_id, photo_hash or "", photo_version),
            ).fetchone()
            if hit:
                photo = json.loads(hit[0])
                result["main_photo_status"] = photo.get("status") or ""
                result["main_photo_score"] = photo.get("score") if photo.get("score") is not None else ""
                result["main_photo_coverage"] = photo.get("coverage") or ""
                if photo.get("report") is not None:
                    result["main_photo_report"] = json.dumps(
                        photo["report"], ensure_ascii=False, separators=(",", ":"),
                    )
    return result


def comparison_for_row(conn, row):
    """Reuse Main's comparable-price decision only for the exact source revision.

    Missing, malformed or contradictory evidence returns None; the Cleaner
    then follows its own classifier. A text profile alone is not a verdict.
    """
    version = os.environ.get("SALE_TEXT_EVALUATOR_VERSION", DEFAULT_TEXT_VERSION).strip()
    if not version:
        return None
    hit = conn.execute(
        """SELECT result_json FROM main_text_assessments
           WHERE listing_id=? AND content_hash=? AND evaluator_version=?""",
        (_listing_id(row.get("id")), collector_client.content_hash(row), version),
    ).fetchone()
    if not hit:
        return None
    try:
        result = json.loads(hit[0])
        if result.get("comparison_policy_version") != COMPARISON_POLICY_VERSION:
            return None
        context = result.get("assessment_context")
        if not isinstance(context, dict) or set(context) != {"price", "photo_set_hash", "furniture"}:
            return None
        try:
            if Decimal(str(context["price"])) != Decimal(str(row.get("price"))):
                return None
        except (InvalidOperation, TypeError):
            return None
        if (context["photo_set_hash"] or None) != (row.get("photo_set_hash") or None):
            return None
        if (context["furniture"] or None) != (row.get("furniture") or None):
            return None
        profile = result["sale_profile"]
        verdict = result["comparison_verdict"]
        offer_type = profile["offer_type"]
        usable, code = verdict["usable"], verdict["reason_code"]
        confidence, evidence = verdict["confidence"], verdict["evidence"]
        if offer_type not in {"whole_apartment", "multi_unit_offer", "partial_property", "non_apartment", "not_sale", "unclear"}:
            return None
        if (usable is None and code == "unclear" and
                (offer_type == "multi_unit_offer"
                 or profile.get("photo_scope") == "mixed_units"
                 or profile.get("data_consistency") == "conflicting")):
            # A price for a collection of variants is not a bad price; it is
            # simply not a price of one identifiable apartment. Do not fall
            # back to the older Cleaner prompt, which cannot see photo scope.
            return {
                "usable": False, "reason_code": "unit_ambiguous",
                "confidence": "high" if offer_type == "multi_unit_offer" else "medium",
                "reason": "цена и фотографии не привязаны к одной квартире",
                "source": "main",
            }
        if type(usable) is not bool or code not in {
            "ok", "partial_property", "not_apartment", "not_sale", "price_not_full",
            "legal_restriction", "auction_distress", "non_market_terms", "other_red_flag",
        } or confidence not in {"high", "medium", "low"}:
            return None
        if usable != (code == "ok") or not isinstance(evidence, str) or len(evidence) > 180:
            return None
        if usable:
            if evidence.strip() or offer_type in {"unclear", "multi_unit_offer", "partial_property", "non_apartment", "not_sale"}:
                return None
        else:
            if not evidence.strip():
                return None
            source_text = " ".join(str(row.get(field) or "") for field in (
                "title", "full_description", "rent_renovation", "priv_dorm",
            )).casefold()
            if " ".join(evidence.casefold().split()) not in " ".join(source_text.split()):
                return None
            if code == "partial_property" and offer_type != "partial_property":
                return None
            if code == "not_apartment" and offer_type != "non_apartment":
                return None
            if code == "not_sale" and offer_type != "not_sale":
                return None
        return {
            "usable": usable, "reason_code": code, "confidence": confidence,
            "reason": "" if usable else evidence.strip(), "source": "main",
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def has_current_text_version(conn, row):
    """Whether the new, context-sensitive Main verdict exists for this text."""
    version = os.environ.get("SALE_TEXT_EVALUATOR_VERSION", DEFAULT_TEXT_VERSION).strip()
    if not version:
        return False
    return conn.execute(
        """SELECT 1 FROM main_text_assessments
           WHERE listing_id=? AND content_hash=? AND evaluator_version=?""",
        (_listing_id(row.get("id")), collector_client.content_hash(row), version),
    ).fetchone() is not None


def enrich_rows(conn, rows):
    """Baseline enrichment is optional and never filters a listing."""
    text_version = os.environ.get("SALE_TEXT_EVALUATOR_VERSION", DEFAULT_TEXT_VERSION).strip()
    photo_version = os.environ.get("SALE_PHOTO_EVALUATOR_VERSION", DEFAULT_PHOTO_VERSION).strip()
    if not text_version and not photo_version:
        return rows
    enriched = []
    for row in rows:
        copy = dict(row)
        copy.update(current_for_row(
            conn, row, text_version=text_version, photo_version=photo_version,
        ))
        # The Main photo gateway reads these compact interior_* columns from
        # the published baseline and can reuse an exact photo/version match.
        status = copy["main_photo_status"]
        copy.update({
            "interior_status": status,
            "interior_score": copy["main_photo_score"],
            "interior_coverage": copy["main_photo_coverage"],
            "interior_evaluator_version": photo_version if status else "",
        })
        enriched.append(copy)
    return enriched
