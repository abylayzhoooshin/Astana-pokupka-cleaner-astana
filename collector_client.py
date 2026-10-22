"""
collector_client.py — забирает одну immutable candidate-версию Collector:
объявления, фото-метаданные, историю цен и presence events.

ПОЧЕМУ ДИФФ ПО ХЭШУ КОНТЕНТА, А НЕ ПО ВЕРСИИ CELIKOM.
Версия snapshot у Collector меняется при изменении её содержимого: если хоть у
одного объявления сдвинулась цена, версия уже
другая. Если гонять ИИ на "всё, что в новой версии есть, а в старой не
было по id" — это будет захватывать unchanged объявления просто потому,
что они лежат в новом файле. Нам нужно куда более узкое множество: то,
у чего реально изменилось то, что ВЛИЯЕТ НА ТЕКСТОВЫЙ ВЕРДИКТ (описание,
признак отделки) — не цена, не last_seen_at.

Поэтому здесь: тянем ВСЮ таблицу постранично (десятки тысяч строк всё ещё
укладываются в обычный HTTP snapshot), для каждой строки считаем content_hash по релевантным полям,
сравниваем со СВОИМ хранилищем (cleaner_db.get_known_hashes) — и в
работу идут только несовпадения. Все таблицы выгружаются с одним зафиксированным
`version`; подмена версии или оборванная пагинация отклоняют весь snapshot.
"""
import hashlib
import json
import logging
import os
from dataclasses import dataclass

import requests

log = logging.getLogger("collector_client")

COLLECTOR_URL = os.environ.get("COLLECTOR_URL", "").rstrip("/")
COLLECTOR_API_KEY = os.environ.get("COLLECTOR_API_KEY", "")
COLLECTOR_CANDIDATE_SCOPE = os.environ.get("COLLECTOR_CANDIDATE_SCOPE", "").strip()
PAGE_SIZE = int(os.environ.get("COLLECTOR_PAGE_SIZE", "500"))  # = потолок коллектора

# Это ключ для текстового вердикта Cleaner. Цена и фото не входят: при их
# изменении дорогую общую разметку не прогоняем заново. Вердикт Main, который
# видит цену и фотоотчёт, отдельно проверяет контекст конкретной ревизии.
CONTENT_FIELDS = (
    "title", "full_description", "rent_renovation", "priv_dorm",
    "square_m2", "rooms", "floor", "floor_total", "street", "house_num",
    "complex_id", "complex_name", "furniture",
)


def _content_value(row, field):
    return row.get(field)


class CollectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateSnapshot:
    """One immutable Collector candidate version and its source histories."""

    version: str
    meta: dict
    rows: list
    price_observations: list
    presence_events: list


def content_hash(row):
    # JSON устраняет коллизии от разделителя внутри пользовательского текста
    # и сохраняет границы полей. Типы структурных значений тоже значимы.
    payload = [_content_value(row, field) for field in CONTENT_FIELDS]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def photo_set_hash(photo_urls):
    """The Collector's canonical identity for an unordered photo set."""
    if not photo_urls:
        return None
    return hashlib.sha256("|".join(sorted(photo_urls)).encode("utf-8")).hexdigest()


def photo_identity_valid(row):
    """Validate the three photo fields without feeding them into text AI."""
    try:
        raw_urls = row["photo_urls"]
        urls = json.loads(raw_urls) if isinstance(raw_urls, str) else raw_urls
        if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
            return False
        return (
            row["photo_count"] == len(urls)
            and row["photo_set_hash"] == photo_set_hash(urls)
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _validate_snapshot_photos(rows):
    invalid = [str(row.get("id", "?")) for row in rows if not photo_identity_valid(row)]
    if invalid:
        sample = ", ".join(invalid[:5])
        raise CollectorError(
            f"несогласованные photo_urls/photo_count/photo_set_hash у "
            f"{len(invalid)} строк (первые id: {sample})"
        )


def _headers():
    h = {}
    if COLLECTOR_API_KEY:
        h["X-API-Key"] = COLLECTOR_API_KEY
    return h


def _get_json(path, *, params=None, timeout=60):
    if not COLLECTOR_URL:
        raise CollectorError("COLLECTOR_URL is not configured")
    response = requests.get(
        f"{COLLECTOR_URL}{path}", params=params, headers=_headers(), timeout=timeout,
    )
    if response.status_code == 503:
        try:
            detail = response.json().get("detail")
        except (AttributeError, ValueError):
            detail = None
        raise CollectorError(f"Collector candidate snapshot is not ready: {detail or '503'}")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise CollectorError(f"Collector returned invalid JSON object for {path}")
    return data


def fetch_candidate_meta():
    """Pin the immutable candidate version used by the entire download."""
    params = {"scope": COLLECTOR_CANDIDATE_SCOPE} if COLLECTOR_CANDIDATE_SCOPE else None
    meta = _get_json("/baseline/candidates/meta", params=params, timeout=20)
    version = meta.get("version")
    if not isinstance(version, str) or not version:
        raise CollectorError("candidate meta has no valid version")
    if COLLECTOR_CANDIDATE_SCOPE:
        if meta.get("scope") != COLLECTOR_CANDIDATE_SCOPE or not meta.get("scope_revision"):
            raise CollectorError("Collector returned a different or unversioned candidate scope")
    for field in (
        "listings_total", "photos_total", "price_observations_total",
        "presence_events_total",
    ):
        value = meta.get(field)
        if not isinstance(value, int) or value < 0:
            raise CollectorError(f"candidate meta has invalid {field}: {value!r}")
    return meta


def _fetch_candidate_table(path, version, expected_total, scope_revision=None):
    rows = []
    offset = 0
    while offset < expected_total:
        params = {"version": version, "limit": PAGE_SIZE, "offset": offset}
        if COLLECTOR_CANDIDATE_SCOPE:
            params["scope"] = COLLECTOR_CANDIDATE_SCOPE
        data = _get_json(path, params=params)
        if COLLECTOR_CANDIDATE_SCOPE and (
            data.get("scope") != COLLECTOR_CANDIDATE_SCOPE
            or data.get("scope_revision") != scope_revision
        ):
            raise CollectorError(f"candidate endpoint {path} changed scope during download")
        if data.get("version") != version:
            raise CollectorError(
                f"candidate endpoint {path} returned version {data.get('version')!r}, "
                f"expected {version!r}"
            )
        if data.get("total") != expected_total:
            raise CollectorError(
                f"candidate endpoint {path} total changed: "
                f"{data.get('total')!r} != {expected_total}"
            )
        page = data.get("rows")
        if not isinstance(page, list) or not page:
            raise CollectorError(
                f"candidate endpoint {path} ended at {offset} of {expected_total}"
            )
        rows.extend(page)
        offset += len(page)
        if len(rows) > expected_total:
            raise CollectorError(
                f"candidate endpoint {path} returned more than {expected_total} rows"
            )
    return rows


def _validate_source_events(events, listing_ids, label):
    source_ids = set()
    for event in events:
        source_event_id = event.get("source_event_id")
        listing_id = str(event.get("listing_id"))
        if not isinstance(source_event_id, str) or not source_event_id:
            raise CollectorError(f"{label} contains an event without source_event_id")
        if source_event_id in source_ids:
            raise CollectorError(f"{label} contains duplicate source_event_id={source_event_id}")
        if listing_id not in listing_ids:
            raise CollectorError(
                f"{label} event {source_event_id} references absent listing {listing_id}"
            )
        source_ids.add(source_event_id)


def fetch_candidate_snapshot():
    """Download one version-consistent pre-dedupe snapshot.

    Listing pages reconstruct ``photo_urls`` from the immutable
    ``listing_photos`` table.  Comparing their aggregate ``photo_count`` with
    ``photos_total`` validates that photo data belongs to the same version
    without downloading 880k URL rows a second time.
    """
    meta = fetch_candidate_meta()
    version = meta["version"]
    scope_revision = meta.get("scope_revision")
    rows = _fetch_candidate_table(
        "/baseline/candidates/listings", version, meta["listings_total"], scope_revision
    )
    _validate_snapshot_photos(rows)
    photo_total = sum(int(row.get("photo_count") or 0) for row in rows)
    if photo_total != meta["photos_total"]:
        raise CollectorError(
            f"candidate photo total mismatch: listings contain {photo_total}, "
            f"meta declares {meta['photos_total']}"
        )

    prices = _fetch_candidate_table(
        "/baseline/candidates/prices", version, meta["price_observations_total"], scope_revision
    )
    presence = _fetch_candidate_table(
        "/baseline/candidates/presence", version, meta["presence_events_total"], scope_revision
    )
    listing_ids = {str(row["id"]) for row in rows}
    if len(listing_ids) != len(rows):
        raise CollectorError("candidate listings contain duplicate listing IDs")
    _validate_source_events(prices, listing_ids, "price history")
    _validate_source_events(presence, listing_ids, "presence history")
    effective_version = (f"{version}:{COLLECTOR_CANDIDATE_SCOPE}:{scope_revision}"
                         if COLLECTOR_CANDIDATE_SCOPE else version)
    return CandidateSnapshot(effective_version, meta, rows, prices, presence)


def fetch_meta():
    if not COLLECTOR_URL:
        raise CollectorError("COLLECTOR_URL не задан")
    r = requests.get(f"{COLLECTOR_URL}/baseline/meta", headers=_headers(), timeout=20)
    if r.status_code == 503:
        raise CollectorError(f"коллектор ещё не готов: {r.json().get('detail')}")
    r.raise_for_status()
    return r.json()


def fetch_all_rows():
    """Все строки текущей опубликованной версии baseline, постранично.

    Гарантия консистентности: если версия сменилась ПОСРЕДИ обхода
    страниц (коллектор пересобрался за это время), мы это заметим по
    полю version в ответе и начнём обход заново — иначе получили бы
    смесь строк из двух разных версий.
    """
    if not COLLECTOR_URL:
        raise CollectorError("COLLECTOR_URL не задан")

    for attempt in range(3):
        rows = []
        offset = 0
        version = None
        expected_total = None
        while True:
            r = requests.get(
                f"{COLLECTOR_URL}/baseline/table",
                params={"limit": PAGE_SIZE, "offset": offset},
                headers=_headers(), timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            if version is None:
                version = data["version"]
                expected_total = data["total"]
            elif data["version"] != version:
                log.warning(
                    "версия baseline сменилась во время обхода страниц "
                    "(%s -> %s), перечитываю с начала [попытка %s/3]",
                    version, data["version"], attempt + 1,
                )
                break
            elif data["total"] != expected_total:
                log.warning(
                    "total baseline изменился без смены версии (%s -> %s), "
                    "перечитываю с начала [попытка %s/3]",
                    expected_total, data["total"], attempt + 1,
                )
                break
            rows.extend(data["rows"])
            if len(rows) == expected_total:
                _validate_snapshot_photos(rows)
                return version, rows
            if len(rows) > expected_total or len(data["rows"]) < PAGE_SIZE:
                log.warning(
                    "коллектор вернул неполный baseline: получено %s из %s, "
                    "перечитываю с начала [попытка %s/3]",
                    len(rows), expected_total, attempt + 1,
                )
                break
            offset += PAGE_SIZE
    raise CollectorError("не удалось получить консистентный снимок baseline за 3 попытки")


def diff_against_known(rows, known_hashes):
    """rows — из fetch_all_rows(). known_hashes — {id: hash} из cleaner_db.

    Возвращает (to_process, unchanged_count): to_process — список
    (row, content_hash) для строк, которых нет в known_hashes или хэш
    разошёлся.
    """
    to_process = []
    unchanged = 0
    for row in rows:
        h = content_hash(row)
        if known_hashes.get(row["id"]) == h:
            unchanged += 1
        else:
            to_process.append((row, h))
    return to_process, unchanged
