"""Бесплатные правила для Cleaner продажи квартир в Астане.

Правило возвращает готовый вердикт только для однозначного случая. Любая
неопределённость передаётся модели: ложное исключение полноценной квартиры
опаснее дополнительного вызова AI. Collector уже проверил город, цену,
площадь, продавца и дедупликацию — здесь эта работа не повторяется.
"""
import re

POLICY_VERSION = "sales-astana-v1"


def _rx(pattern):
    return re.compile(pattern, re.IGNORECASE)


# Паттерны применяются к заголовку, где формулировка обычно короткая и
# предмет объявления названа прямо. Они намеренно не ловят «1-комнатная».
PARTIAL_OBJECT_TITLE_PATTERNS = (
    _rx(r"\bдол(?:я|ю|и)\s+(?:в\s+)?квартир"),
    _rx(r"\bкомнат[ау]\s+(?:в\s+)?(?:\d+[\s-]*)?комнатн(?:ой|ую)\s+квартир"),
    _rx(r"\bчаст[ьи]\s+квартир"),
)

NON_RESIDENTIAL_TITLE_PATTERNS = (
    _rx(r"^(?:(?:срочно\s+)?(?:продам|прода[её]тся)\s+)?(?:офис|магазин|склад|гараж|паркинг|кладов(?:ая|ку))\b"),
    _rx(r"^(?:(?:срочно\s+)?(?:продам|прода[её]тся)\s+)?нежил(?:ое|ого)\s+помещен"),
)


def _verdict(reason_code, reason):
    return {
        "usable": False,
        "reason_code": reason_code,
        "confidence": "high",
        "reason": reason,
        "source": "rule",
    }


def classify(row):
    """Вернуть однозначный rule-verdict или ``None`` для AI-слоя.

    Черновая отделка, отсутствие мебели, срочность, ипотека, переуступка,
    строящийся дом и ``priv_dorm`` сами по себе не являются браком. Для
    продажи это характеристики/условия сделки, а не доказательство того,
    что объявленная сумма несопоставима с ценой целой квартиры.
    """
    title = (row.get("title") or "").strip()

    for pattern in PARTIAL_OBJECT_TITLE_PATTERNS:
        if pattern.search(title):
            return _verdict(
                "partial_property",
                "в заголовке продаётся доля, комната или часть квартиры",
            )

    for pattern in NON_RESIDENTIAL_TITLE_PATTERNS:
        if pattern.search(title):
            return _verdict(
                "not_apartment",
                "в заголовке указан нежилой объект, а не квартира",
            )

    return None
