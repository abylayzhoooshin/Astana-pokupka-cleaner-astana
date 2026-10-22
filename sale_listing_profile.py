"""Консервативные текстовые признаки продажи, отдельно от usable и цены.

Это утверждения автора объявления, а не проверка состояния квартиры или
юридических документов. Неизвестное остаётся неизвестным; фотографии здесь
не читаются и не подменяются текстом. Правила намеренно требуют явной фразы.
"""
import hashlib
import json
import re

PROFILE_VERSION = "sales-text-claims-v1"
PROFILE_FIELDS = ("title", "full_description", "rent_renovation", "furniture")

STRUCTURED = {
    "rent_renovation": {
        "черновая отделка": "finish_rough",
        "требует ремонта": "finish_needs_repair",
        "свежий ремонт": "finish_fresh",
        "не новый, но аккуратный ремонт": "finish_lived_in",
        "свободная планировка": "free_layout",
    },
    "furniture": {
        "полностью": "furniture_full",
        "частично": "furniture_partial",
        "без мебели": "furniture_none",
    },
}

# Только явные формулировки. Маркетинговое «дизайнерский» помечается как
# заявление продавца, а не подтверждённое качество ремонта.
TEXT_PATTERNS = {
    "finish_rough": r"\b(?:в\s+)?чернов(?:ой|ая|ую|ом)\s+(?:отделк[еауи]|вариант[еа])\b",
    "finish_pre_finish": r"\bпредчистов(?:ая|ой|ую)\s+отделк[а-я]*\b",
    "finish_needs_repair": r"\b(?:требует|нужен|под)\s+(?:(?:капитальн[а-я]*|косметическ[а-я]*)\s+)?ремонт[а-я]*\b|\bбез\s+ремонта\b",
    "finish_fresh": r"\b(?:свежий|новый)\s+ремонт\b|\bремонт\s+(?:свежий|новый)\b",
    "finish_lived_in": r"\b(?:ремонт\s+не\s+новый|старый\s+ремонт)\b",
    "finish_unfinished": r"\b(?:ремонт\s+не\s+закончен|незаконченный\s+ремонт|ремонт\s+в\s+процессе)\b",
    "finish_designer_claim": r"\bдизайнерск(?:ий|им|ого|ому)\s+ремонт[а-я]*\b",
    "move_in_ready_claim": r"\bзаезжай\s+и\s+живи\b|\bготова\s+к\s+проживанию\b",
    "furniture_full": r"\b(?:вся|полностью\s+с)\s+мебель(?:ю|\s+оста[её]тся)\b|\bмебель\s+и\s+техника\s+оста[её]тся\b",
    "furniture_none": r"\bбез\s+мебели\b|\bмебель\s+не\s+оста[её]тся\b",
    "tenant_occupied": r"\b(?:живут|проживают)\s+(?:арендатор[а-я]*|квартирант[а-я]*)\b|\bквартира\s+с\s+(?:арендатор[а-я]*|квартирант[а-я]*)\b|\bпрода[её]тся\s+с\s+действующ(?:им|ими)\s+арендатор[а-я]*\b",
    "vacant_claim": r"\bквартира\s+(?:свободна|пустует)\b|\bникто\s+не\s+проживает\b",
    "under_construction": r"\bстроящ(?:ийся|емся|егося)\s+дом[а-я]*\b|\bдом\s+ещ[её]\s+строится\b",
    "keys_received_claim": r"\bключи\s+(?:получены|на\s+руках)\b",
    "commissioned_claim": r"\bдом\s+сдан\s+в\s+эксплуатацию\b|\bакт\s+ввода\s+получен\b",
    "assignment_sale": r"\b(?:по\s+)?переуступк[еаиу]\b|\bдоговор\s+цессии\b",
    "cash_only": r"\bтолько\s+(?:за\s+)?наличн(?:ые|ый\s+расч[её]т)\b|\bипотек[а-я]*\s+не\s+(?:проходит|рассматрива[а-я]*)\b",
    "installment_offer": r"\bрассрочк[а-я]*\s+(?:возможна|есть|от\s+застройщика)\b|\bвозможна\s+рассрочка\b",
    "mortgage_possible_claim": r"\b(?:ипотек[а-я]*\s+(?:проходит|возможна|подходит)|подходит\s+под\s+ипотеку)\b",
    "mortgage_encumbered_claim": r"\b(?:квартира|объект)\s+(?:в\s+)?залоге\s+(?:у\s+)?банк[а-я]*\b|\bобременени[еям]\s+(?:в\s+)?виде\s+ипотек[а-я]*\b",
    "exchange_offer": r"\b(?:обмен\s+с\s+доплатой|рассмотр[а-я]*\s+обмен|возможен\s+обмен)\b",
    "auction_sale": r"\b(?:продажа\s+с\s+торгов|аукционн(?:ая|ые)\s+продаж[а-я]*|реализация\s+через\s+аукцион)\b",
    "legal_issue_claim": r"\b(?:квартира\s+под\s+арестом|документы\s+не\s+в\s+порядке|перепланировка\s+не\s+узаконена)\b",
    # «Лучшая цена за м²» — обычная реклама, а не неполная цена карточки.
    "partial_price_claim": r"\b(?:указан[а-я]*\s+первоначальн[а-я]*\s+взнос|(?:указана\s+цена|цена\s+в\s+объявлении)\s+за\s+(?:кв\.?\s*м|м²|долю)|полная\s+стоимость\s+по\s+запросу)\b",
    "water_damage_claim": r"\b(?:после\s+(?:затопления|потопа)|квартиру\s+затопило)\b",
    "fire_damage_claim": r"\b(?:квартира\s+после\s+пожара|после\s+пожара\s+в\s+квартире)\b",
    "demolition_claim": r"\b(?:дом\s+под\s+снос|дом\s+признан\s+аварийным)\b",
    "rental_income_claim": r"\b(?:готовый\s+арендный\s+бизнес|квартира\s+приносит\s+арендный\s+доход)\b",
    "urgent_claim": r"\bсрочн(?:о|ая|ый)\s+прода[а-я]*\b|\bсрочная\s+продажа\b",
}
COMPILED = {code: re.compile(pattern, re.IGNORECASE) for code, pattern in TEXT_PATTERNS.items()}


def profile_hash(row):
    payload = [row.get(field) for field in PROFILE_FIELDS]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def classify(row):
    """Вернуть только явно заявленные признаки; не принимать решение о цене."""
    signals = []
    for field, values in STRUCTURED.items():
        value = (row.get(field) or "").strip().lower().replace("ё", "е")
        for phrase, code in values.items():
            if value == phrase.replace("ё", "е"):
                signals.append({"code": code, "source": field, "evidence": row[field]})

    text = " ".join(str(row.get(field) or "") for field in ("title", "full_description"))
    for code, pattern in COMPILED.items():
        match = pattern.search(text)
        if not match:
            continue
        before = text[max(0, match.start() - 20):match.start()]
        negated = re.search(r"\b(?:не|нет|без|после)\s+(?:в\s+)?$", before, re.IGNORECASE)
        fresh_work_requested = (
            code == "finish_fresh" and (
                re.search(r"\b(?:нужен|требуется)\s+$", before, re.IGNORECASE)
                or re.match(r"\s+не\s+(?:нужен|требуется)\b",
                            text[match.end():], re.IGNORECASE)
            )
        )
        if not negated and not fresh_work_requested:
            signals.append({"code": code, "source": "text", "evidence": match.group(0)[:100]})

    # Разные источники могут противоречить друг другу. Сохраняем оба
    # утверждения для аудита, не выбираем «победителя» без фото/документов.
    signals.sort(key=lambda item: (item["code"], item["source"]))
    codes = {item["code"] for item in signals}
    conflicts = []
    finish_claims = codes & {
        "finish_rough", "finish_pre_finish", "finish_needs_repair",
        "finish_fresh", "finish_lived_in",
    }
    if len(finish_claims) > 1:
        conflicts.append("finish")
    if "furniture_full" in codes and "furniture_none" in codes:
        conflicts.append("furniture")
    return {"version": PROFILE_VERSION, "signals": signals, "conflicts": conflicts}
