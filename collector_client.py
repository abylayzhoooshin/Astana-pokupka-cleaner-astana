"""
collector_client.py — забирает baseline у сервиса-коллектора и решает,
что из него РЕАЛЬНО новое для нас.

ПОЧЕМУ ДИФФ ПО ХЭШУ КОНТЕНТА, А НЕ ПО ВЕРСИИ CELIKOM.
Версия baseline у коллектора меняется почти при каждой пересборке (раз
в 30 минут) — цена хоть одного объявления сдвинулась, и версия уже
другая. Если гонять ИИ на "всё, что в новой версии есть, а в старой не
было по id" — это будет захватывать unchanged объявления просто потому,
что они лежат в новом файле. Нам нужно куда более узкое множество: то,
у чего реально изменилось то, что ВЛИЯЕТ НА ВЕРДИКТ (описание, фото,
признак отделки) — не цена, не last_seen_at.

Поэтому здесь: тянем ВСЮ таблицу постранично (десятки тысяч строк всё ещё
укладываются в обычный HTTP snapshot), для каждой строки считаем content_hash по релевантным полям,
сравниваем со СВОИМ хранилищем (cleaner_db.get_known_hashes) — и в
работу идут только несовпадения. Не оптимально по трафику (качаем всё
каждый раз), но коллектор рассчитан именно на это (см. пагинацию в
baseline_api.py), а объём (пара МБ раз в сутки) не то, ради чего стоит
городить инкрементальный API на стороне коллектора.
"""
import hashlib
import json
import logging
import os

import requests

log = logging.getLogger("collector_client")

COLLECTOR_URL = os.environ.get("COLLECTOR_URL", "").rstrip("/")
COLLECTOR_API_KEY = os.environ.get("COLLECTOR_API_KEY", "")
PAGE_SIZE = int(os.environ.get("COLLECTOR_PAGE_SIZE", "500"))  # = потолок коллектора

# Поля, которые реально влияют на текстовый вердикт.
#
# photo_set_hash здесь СОЗНАТЕЛЬНО НЕТ, хотя поле доступно. В модель
# уходит только текст (см. openai_batch._format_listing) — фотографии
# не отправляются. Значит смена фото при неизменном описании даст при
# переоценке ровно тот же вердикт: платим за повторный прогон, получаем
# идентичный результат. Хуже того, krisha при перевыкладке часто
# выдаёт тем же снимкам новые URL, поэтому поле генерировало бы ложные
# срабатывания на пустом месте.
#
# Вернуть сюда photo_set_hash нужно будет ровно тогда, когда появится
# vision-слой и фотографии начнут влиять на вердикт — не раньше.
#
# Цена и last_seen_at не входят по той же логике: цена меняется часто,
# на "комната это или квартира" не влияет никак.
CONTENT_FIELDS = (
    "title", "full_description", "rent_renovation", "priv_dorm",
    "square_m2", "rooms",
)


class CollectorError(RuntimeError):
    pass


def content_hash(row):
    # JSON устраняет коллизии от разделителя внутри пользовательского текста
    # и сохраняет границы полей. Типы структурных значений тоже значимы.
    payload = [row.get(field) for field in CONTENT_FIELDS]
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
