"""
verdicts_api.py — HTTP-слой поверх cleaner_db, тем же паттерном, что
baseline_api.py в коллекторе (токен, пагинация, health с возрастом).

ДВА РАЗНЫХ ПОТРЕБИТЕЛЯ — ДВА РАЗНЫХ ЭНДПОИНТА.

/baseline/clean — основной. Отдаёт ГОТОВЫЙ чистый baseline, который
    хранится у нас и пересобирается в конце каждого цикла
    (pipeline.publish_clean_baseline). В нём только объявления, прошедшие
    разметку по текущему тексту; новые и изменённые ждут следующего цикла.

    Коллектор на запрос потребителя не дёргается: снимок согласован
    (все страницы из одной выгрузки) и доступен, даже когда коллектор
    лежит. Плата — цены на момент последнего цикла, до 12 часов давности.

/verdicts/* — служебный. Сами вердикты без данных объявлений: для
    ручной проверки качества разметки и для потребителей, которые
    предпочитают свести данные сами.
"""
import json
import logging
import os
import secrets
import sqlite3
import time
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import cleaner_db
import entity_store
import main_assessments
import openai_batch

API_KEY = os.environ.get("CLEANER_API_KEY", "").strip()
MANUAL_MATCH_WRITES_ENABLED = os.environ.get(
    "ENTITY_MANUAL_WRITES_ENABLED", "0"
).strip().lower() in {"1", "true", "yes"}
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 200

# Порог "давно не было ни одного успешного цикла". При 12-часовом цикле
# трое суток — это шесть пропущенных подряд, то есть точно поломка, а не
# разовая неудача из-за недоступного коллектора.
STALE_AFTER_SECONDS = int(os.environ.get("CLEANER_STALE_AFTER_S", str(3 * 24 * 3600)))

LAST_CYCLE_KEY = "last_cycle"  # legacy key for existing consumers
LAST_FULL_ATTEMPT_KEY = "last_full_attempt"
LAST_FULL_SUCCESS_KEY = "last_full_success"
LAST_INGEST_TICK_KEY = "last_ingest_tick"

app = FastAPI(title="rieltor-sales-cleaner-astana")
log = logging.getLogger("sales_cleaner.main_assessments")

_started_at = time.monotonic()


class PhotoEvidenceRequest(BaseModel):
    listing_id_a: str
    listing_id_b: str
    evidence: dict


class ManualMatchDecisionRequest(BaseModel):
    listing_id_a: str
    listing_id_b: str
    decision: str
    reason: str
    actor: str = "manual-review"
    decision_revision: str


class MainAssessmentRequest(BaseModel):
    listing_id: str
    text: Optional[dict] = None
    photo: Optional[dict] = None


def _record_result(key, result):
    with cleaner_db.connect() as conn:
        cleaner_db.set_meta(conn, key, json.dumps(result, ensure_ascii=False))
        conn.commit()


def record_full_cycle_result(result):
    """Записать попытку; не выдавать неудачу за новый успешный цикл."""
    payload = json.dumps(result, ensure_ascii=False)
    with cleaner_db.connect() as conn:
        cleaner_db.set_meta(conn, LAST_FULL_ATTEMPT_KEY, payload)
        cleaner_db.set_meta(conn, LAST_CYCLE_KEY, payload)
        if not result.get("error"):
            cleaner_db.set_meta(conn, LAST_FULL_SUCCESS_KEY, payload)
        conn.commit()


def record_ingest_tick_result(result):
    """Тик не должен сдвигать clock свежести полного Collector fetch."""
    _record_result(LAST_INGEST_TICK_KEY, result)


def record_cycle_result(result):
    """Совместимый alias для старых вызывающих модулей."""
    record_full_cycle_result(result)


def require_api_key(x_api_key: str = Header(default="")):
    if not API_KEY:
        return
    if not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="неверный или отсутствующий X-API-Key")


def require_write_api_key(x_api_key: str = Header(default="")):
    """Mutation endpoints must never become public through misconfiguration."""
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail="CLEANER_API_KEY is not configured; writes are disabled",
        )
    if not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="неверный или отсутствующий X-API-Key")


@app.post("/main/assessments", dependencies=[Depends(require_write_api_key)])
def main_assessment(payload: MainAssessmentRequest):
    try:
        with cleaner_db.connect() as conn:
            result = main_assessments.save(conn, payload.dict())
            conn.commit()
        log.info(
            "saved listing_id=%s text=%s photo=%s",
            result["listing_id"], result["text_saved"], result["photo_saved"],
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        # The full Cleaner cycle is the only long-lived writer. Main keeps a
        # durable outbox, so an explicit retry response is safe and avoids an
        # ASGI traceback while the cycle owns SQLite's writer lock.
        raise HTTPException(
            status_code=503,
            detail="Cleaner database is busy; retry delivery",
            headers={"Retry-After": "10"},
        ) from exc


@app.get("/main/photo-scores", dependencies=[Depends(require_write_api_key)])
def main_photo_scores(version: str = Query(..., min_length=1, max_length=100)):
    """All current-version photo scores for exact listing/photo revisions.

    The reviewed pilot Main uses this compact export without consuming the
    moving clean-price baseline or exposing assessment reports.
    """
    try:
        with cleaner_db.connect() as conn:
            rows = main_assessments.export_photo_scores(conn, version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"evaluator_version": version, "rows": rows, "count": len(rows)}


# ========================= чистый baseline =========================

@app.get("/baseline/clean", dependencies=[Depends(require_api_key)])
def baseline_clean(limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
                   offset: int = Query(0, ge=0)):
    """Опубликованный чистый baseline, постранично.

    `total` — число строк в снимке, страницы полные вплоть до последней.
    `built_at` меняется при каждой пересборке: если он сменился посреди
    обхода, страницы из разных снимков — начните обход заново.
    """
    with cleaner_db.connect() as conn:
        info = cleaner_db.clean_baseline_info(conn)
        if info is None:
            # 503, а не пустой список: пустой ответ потребитель принял бы
            # за «объявлений нет» и затёр бы свои данные.
            raise HTTPException(status_code=503,
                                detail="чистый baseline ещё не собран — дождитесь первого цикла")
        total, version, built_at = info
        rows = cleaner_db.clean_baseline_page(conn, limit, offset)
    return {
        "version": version,
        "built_at": built_at,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(rows),
        "rows": rows,
    }


@app.get("/baseline/clean.csv", dependencies=[Depends(require_api_key)])
def baseline_clean_csv():
    """Весь чистый baseline одним CSV-файлом (UTF-8, без BOM, разделитель — запятая).

    Для потребителя, которому нужен просто файл: никакой пагинации и
    проверки built_at, весь снимок читается одним согласованным чтением.
    Значения — текст: None приходит пустой ячейкой, списки (photo_urls) —
    JSON-строкой. Файл заранее собирается на persistent disk вместе со snapshot,
    поэтому HTTP-запрос не держит полную копию baseline в памяти.
    """
    with cleaner_db.connect() as conn:
        info = cleaner_db.clean_baseline_info(conn)
        if info is None:
            raise HTTPException(status_code=503,
                                detail="чистый baseline ещё не собран — дождитесь первого цикла")
        total, _version, built_at = info
        csv_path = cleaner_db.ensure_clean_baseline_csv(conn)

    return FileResponse(
        path=csv_path,
        media_type="text/csv; charset=utf-8",
        filename="clean_baseline.csv",
        headers={
            "X-Built-At": built_at,
            "X-Total-Rows": str(total),
        },
    )


# ============================= вердикты =============================

@app.get("/text-profiles/table", dependencies=[Depends(require_api_key)])
def text_profiles(limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
                  offset: int = Query(0, ge=0)):
    """Заявленные в объявлении признаки, отдельно от решения usable."""
    with cleaner_db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM listing_text_profiles").fetchone()[0]
        rows = [dict(row) for row in conn.execute(
            "SELECT id,profile_hash,profile_version,profile_json,updated_at "
            "FROM listing_text_profiles ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
        )]
    for row in rows:
        row["profile"] = json.loads(row.pop("profile_json"))
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


@app.get("/text-profiles/{listing_id}", dependencies=[Depends(require_api_key)])
def text_profile(listing_id: str):
    with cleaner_db.connect() as conn:
        row = conn.execute(
            "SELECT id,profile_hash,profile_version,profile_json,updated_at "
            "FROM listing_text_profiles WHERE id=?", (listing_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="text profile not collected")
    result = dict(row)
    result["profile"] = json.loads(result.pop("profile_json"))
    return result

# Колонки вердикта без снимков текста: снимки нужны только для ручной
# проверки через /verdicts/review, а в постраничной выгрузке они раздували
# бы ответ на порядок (до 1000 символов описания на строку).
_VERDICT_COLUMNS = (
    "id, usable, reason_code, confidence, reason, source, model, policy_version, "
    "processed_at, baseline_version"
)


@app.get("/verdicts/table", dependencies=[Depends(require_api_key)])
def table(limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
          offset: int = Query(0, ge=0),
          usable: bool = Query(None, description="только годные (true) или только отсеянные (false)"),
          reason_code: str = Query(None, description="фильтр по причине, напр. partial_property")):
    with cleaner_db.connect() as conn:
        clauses, params = [], []
        if usable is not None:
            clauses.append("usable = ?")
            params.append(int(usable))
        if reason_code:
            clauses.append("reason_code = ?")
            params.append(reason_code)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        total = conn.execute(f"SELECT COUNT(*) FROM verdicts {where}", params).fetchone()[0]
        # ORDER BY по (processed_at, id), а не по одному processed_at.
        # Метка времени имеет точность до секунды, и целый batch пишется
        # одной и той же секундой: на реальных данных 1000 из 1014 строк
        # имели идентичный processed_at. Без уникального tie-break порядок
        # строк между запросами не определён, и потребитель, идущий по
        # страницам, мог бы получить дубли и пропуски.
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_VERDICT_COLUMNS} FROM verdicts {where} "
            "ORDER BY processed_at, id LIMIT ? OFFSET ?",
            params + [limit, offset],
        )]
    return {"total": total, "limit": limit, "offset": offset, "returned": len(rows), "rows": rows}


@app.get("/verdicts/review", dependencies=[Depends(require_api_key)])
def review(limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
           offset: int = Query(0, ge=0),
           reason_code: str = Query(None)):
    """Отбракованные объявления вместе с текстом — для проверки глазами.

    Главный вопрос при приёмке модели: не выкидывает ли она нормальные
    квартиры. Отвечать на него, глядя на голые id и reason_code,
    невозможно, поэтому здесь отдаётся сохранённый снимок заголовка и
    описания (см. snapshot_* в cleaner_db).

    Сортировка по confidence: сначала low — там, где модель сама
    сомневалась, ложные срабатывания вероятнее всего.
    """
    with cleaner_db.connect() as conn:
        clauses = ["(usable = 0 OR usable IS NULL)"]
        params = []
        if reason_code:
            clauses.append("reason_code = ?")
            params.append(reason_code)
        where = "WHERE " + " AND ".join(clauses)
        total = conn.execute(f"SELECT COUNT(*) FROM verdicts {where}", params).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"""SELECT id, usable, reason_code, confidence, reason, source, model,
                       policy_version, snapshot_title, snapshot_desc, snapshot_url, processed_at
                FROM verdicts {where}
                ORDER BY CASE confidence WHEN 'low' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                         processed_at, id
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )]
    return {"total": total, "limit": limit, "offset": offset,
            "returned": len(rows), "rows": rows}


@app.get("/verdicts/meta", dependencies=[Depends(require_api_key)])
def meta():
    with cleaner_db.connect() as conn:
        stats = cleaner_db.stats(conn)
        raw, updated_at = cleaner_db.get_meta(conn, LAST_CYCLE_KEY)
        success_raw, success_at = cleaner_db.get_meta(conn, LAST_FULL_SUCCESS_KEY)
        attempt_raw, attempt_at = cleaner_db.get_meta(conn, LAST_FULL_ATTEMPT_KEY)
        tick_raw, tick_at = cleaner_db.get_meta(conn, LAST_INGEST_TICK_KEY)
    stats["last_cycle_at"] = updated_at
    stats["last_cycle_result"] = json.loads(raw) if raw else None
    stats["last_full_success_at"] = success_at
    stats["last_full_success_result"] = json.loads(success_raw) if success_raw else None
    stats["last_full_attempt_at"] = attempt_at
    stats["last_full_attempt_result"] = json.loads(attempt_raw) if attempt_raw else None
    stats["last_ingest_tick_at"] = tick_at
    stats["last_ingest_tick_result"] = json.loads(tick_raw) if tick_raw else None
    stats["policy_version"] = openai_batch.POLICY_VERSION
    return stats


# ======================== physical apartment entities ========================

@app.get("/entities/stats", dependencies=[Depends(require_api_key)])
def entity_stats():
    with cleaner_db.connect() as conn:
        source_presence = conn.execute(
            "SELECT COUNT(*) FROM collector_presence_events"
        ).fetchone()[0]
        source_prices = conn.execute(
            "SELECT COUNT(*) FROM collector_price_events"
        ).fetchone()[0]
        entity_counts = {
            row["lifecycle_status"]: row["amount"]
            for row in conn.execute(
                "SELECT lifecycle_status,COUNT(*) AS amount "
                "FROM property_entities GROUP BY lifecycle_status"
            )
        }
        result = {
            "entities": sum(entity_counts.values()),
            "by_status": entity_counts,
            "members": conn.execute(
                "SELECT COUNT(*) FROM entity_members"
            ).fetchone()[0],
            "entities_with_multiple_active_listings": conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT m.entity_id
                       FROM entity_members m
                       JOIN listing_entity_state s ON s.listing_id=m.listing_id
                       WHERE s.status='active'
                       GROUP BY m.entity_id HAVING COUNT(*) > 1
                   )"""
            ).fetchone()[0],
            "presence_events": conn.execute(
                "SELECT COUNT(*) FROM listing_presence_events"
            ).fetchone()[0] if not source_presence else source_presence,
            "price_observations": conn.execute(
                "SELECT COUNT(*) FROM listing_price_observations"
            ).fetchone()[0] if not source_prices else source_prices,
            "source_history": bool(source_presence or source_prices),
            "current_match_candidates": conn.execute(
                "SELECT COUNT(*) FROM entity_match_candidates WHERE is_current=1"
            ).fetchone()[0],
            "auto_merge_enabled": False,
        }
    return result


@app.get("/entities/{entity_id}", dependencies=[Depends(require_api_key)])
def entity_by_id(entity_id: str):
    with cleaner_db.connect() as conn:
        result = entity_store.entity_detail(conn, entity_id)
    if result is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return result


@app.get("/entity-matches/review", dependencies=[Depends(require_api_key)])
def entity_match_review(
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    status: str = Query(None),
    min_priority: Optional[int] = Query(None, ge=0),
    min_reason_count: Optional[int] = Query(None, ge=1, le=4),
    reason_count: Optional[int] = Query(None, ge=1, le=4),
    activity: Optional[
        Literal["both_active", "active_missing", "both_missing"]
    ] = Query(None),
    eligibility: Optional[
        Literal["both", "at_least_one", "neither"]
    ] = Query(None),
    photo_decision: Optional[
        Literal["likely_same", "likely_different", "uncertain"]
    ] = Query(None),
    min_score: Optional[float] = Query(None, ge=0, le=1),
    max_score: Optional[float] = Query(None, ge=0, le=1),
    order_by: Literal["priority", "revision_hash"] = Query("priority"),
):
    if min_score is not None and max_score is not None and min_score > max_score:
        raise HTTPException(status_code=422, detail="min_score must not exceed max_score")
    if min_reason_count is not None and reason_count is not None:
        raise HTTPException(
            status_code=422,
            detail="min_reason_count and reason_count are mutually exclusive",
        )
    with cleaner_db.connect() as conn:
        total, rows = entity_store.match_candidates_page(
            conn, status=status, limit=limit, offset=offset,
            min_priority=min_priority, min_reason_count=min_reason_count,
            reason_count=reason_count,
            activity=activity, eligibility=eligibility,
            photo_decision=photo_decision, min_score=min_score,
            max_score=max_score, order_by=order_by,
        )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(rows),
        "auto_merge_enabled": False,
        "filters": {
            "status": status,
            "min_priority": min_priority,
            "min_reason_count": min_reason_count,
            "reason_count": reason_count,
            "activity": activity,
            "eligibility": eligibility,
            "photo_decision": photo_decision,
            "min_score": min_score,
            "max_score": max_score,
            "order_by": order_by,
        },
        "rows": rows,
    }


@app.post("/entity-matches/evidence", dependencies=[Depends(require_write_api_key)])
def entity_match_evidence(payload: PhotoEvidenceRequest):
    with cleaner_db.connect() as conn:
        try:
            result = entity_store.record_photo_evidence(
                conn, payload.listing_id_a, payload.listing_id_b,
                payload.evidence,
            )
            conn.commit()
        except entity_store.StalePhotoEvidenceError as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.post("/entity-matches/manual-decision", dependencies=[Depends(require_write_api_key)])
def entity_match_manual_decision(payload: ManualMatchDecisionRequest):
    if not MANUAL_MATCH_WRITES_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="manual entity changes are disabled in this deployment",
        )
    with cleaner_db.connect() as conn:
        try:
            result = entity_store.apply_manual_match_decision(
                conn, payload.listing_id_a, payload.listing_id_b,
                payload.decision, payload.reason, payload.actor,
                payload.decision_revision,
            )
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


@app.get("/health")
def health():
    """Как и у коллектора: не 503 на "ещё не было ни одного цикла" (иначе
    платформа перезапускала бы сервис, который просто ждёт первого
    тика) — 503 только если последний УСПЕШНЫЙ цикл был давно."""
    uptime = time.monotonic() - _started_at
    with cleaner_db.connect() as conn:
        success_raw, success_at = cleaner_db.get_meta(conn, LAST_FULL_SUCCESS_KEY)
        attempt_raw, attempt_at = cleaner_db.get_meta(conn, LAST_FULL_ATTEMPT_KEY)
        tick_raw, tick_at = cleaner_db.get_meta(conn, LAST_INGEST_TICK_KEY)

    attempt_result = json.loads(attempt_raw) if attempt_raw else None
    if not success_at:
        if uptime > STALE_AFTER_SECONDS:
            return JSONResponse(
                {
                    "status": "broken",
                    "detail": "нет успешного полного цикла за отведённое время",
                    "last_full_attempt_at": attempt_at,
                    "last_full_attempt_result": attempt_result,
                },
                status_code=503,
            )
        return JSONResponse(
            {
                "status": "starting",
                "uptime_seconds": int(uptime),
                "last_full_attempt_at": attempt_at,
                "last_full_attempt_result": attempt_result,
            },
            status_code=200,
        )

    age = time.time() - cleaner_db.parse_iso(success_at)
    stale = age > STALE_AFTER_SECONDS
    last_attempt_failed = bool(attempt_result and attempt_result.get("error"))
    body = {
        "status": "stale" if stale else ("degraded" if last_attempt_failed else "ok"),
        "last_cycle_age_seconds": int(age),
        "last_cycle_result": json.loads(success_raw) if success_raw else None,
        "last_full_success_at": success_at,
        "last_full_success_result": json.loads(success_raw) if success_raw else None,
        "last_full_attempt_at": attempt_at,
        "last_full_attempt_result": attempt_result,
        "last_ingest_tick_at": tick_at,
        "last_ingest_tick_result": json.loads(tick_raw) if tick_raw else None,
    }
    return JSONResponse(body, status_code=503 if stale else 200)
