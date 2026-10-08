# Entity resolution для продажи квартир в Астане

Статус документа: согласованная архитектура и описание первой реализованной
фазы Cleaner. Автоматическое объединение дублей выключено. Старая
дедупликация Collector пока не отключается.

## 1. Задача и границы

Нужно очищать baseline квартир на продажу так, чтобы несколько объявлений об
одной физической квартире не искажали цену и статистику экспозиции.

- `listing_id` — неизменяемый ID отдельного объявления Krisha.
- `entity_id` — наш стабильный ID физической квартиры.
- Все исходные `listing_id` сохраняются. Объединение не удаляет объявления.
- У одной квартиры может одновременно быть два, шесть и больше активных
  объявлений от разных агентов и с разными ценами.
- `missing` — не мусор. Такие объявления остаются в истории и участвуют в
  расчётах скорости исчезновения, возвратов и изменения цены.
- В чистый baseline выходит одна целая строка одного объявления на entity.
  Поля разных объявлений никогда не смешиваются.

Это проект продажи квартир в Астане. Правила аренды Алматы не переносятся.
Существующий арендный Main использовался только как технический пример
фотооценки; он не является частью этого проекта и не изменяется здесь.

## 2. Распределение ответственности

### Collector

Collector отвечает за факты источника, а не за физическую идентичность:

1. хранит каждое объявление Krisha отдельно;
2. выполняет только технические фильтры;
3. фиксирует наблюдения цены и присутствия;
4. публикует согласованный immutable candidate snapshot до дедупликации;
5. временно продолжает публиковать старый `/baseline/table` для отката.

Collector не должен окончательно удалять дубль или заменять один `listing_id`
другим.

### Cleaner

Cleaner:

1. проверяет, пригодно ли объявление для ценового baseline;
2. сохраняет отдельные listing episodes и их локальную историю;
3. создаёт стабильные entities и историю membership;
4. формирует консервативные пары-кандидаты по метаданным;
5. хранит версионированные photo evidence, полученные извне;
6. выполняет транзакционные merge/split и manual override;
7. выбирает одну публикационно готовую строку на entity.

Cleaner не скачивает фотографии, не хранит их бинарники и не запускает vision.

### Фотооценка покупки

Фотообработку следует оставить рядом с будущей оценкой новых объявлений
покупки. Она получает от Cleaner пару-кандидат, анализирует оригинальные
цветные фотографии и возвращает версионированные evidence. Это интеграционный
контракт, а не требование создавать новый сервис в этом репозитории.

## 3. Что уже реализовано в Cleaner

- стабильные `entity_id` и singleton entity для каждого нового `listing_id`;
- текущая принадлежность и append-only история принадлежности;
- локальные price observations и presence events;
- lifecycle entity, рассчитанный по всем members;
- транзакционные и идемпотентные merge/split;
- alias для merge и lineage для split;
- история переключения canonical;
- metadata shortlist дублей без автоматического merge;
- очередь review через API;
- приём полностью версионированных photo evidence;
- ручные решения `same_entity`/`different_entity`;
- публикация одной целой строки на entity;
- регрессионные тесты миграций, истории, canonical, evidence и overrides.

Текущий вход Cleaner всё ещё `/baseline/table`, поэтому он видит уже
дедуплицированный Collector baseline. Полная точность возможна только после
реализации candidate snapshot в Collector.

## 4. Окончательный Collector → Cleaner контракт

### 4.1 Immutable candidate version

Одна candidate version — логически единый снимок следующих наборов:

1. `listings` — все объявления после технических фильтров, до дедупликации;
2. `listing_photos` — упорядоченные URL фотографий каждого объявления;
3. `price_history` — известные наблюдения цены;
4. `presence_events` — известные события присутствия.

Наборы могут физически отдаваться разными пагинируемыми endpoints, но каждый
запрос обязан быть закреплён за одним и тем же `version`. Нельзя подмешивать
историю или фотографии из текущего mutable состояния.

Публичный pointer `current_version` переключается атомарно только после записи
всех четырёх наборов и manifest. Опубликованная версия больше не изменяется.
Любое изменение содержимого любого набора создаёт новую version. Старую версию
нужно сохранять минимум на время, достаточное Cleaner для полного чтения и
повтора после сбоя.

Manifest версии:

```json
{
  "version": "opaque-immutable-id",
  "built_at": "2026-10-08T12:00:00Z",
  "schema_version": "candidate-v1",
  "listings_total": 0,
  "photos_total": 0,
  "price_observations_total": 0,
  "presence_events_total": 0
}
```

На каждой странице возвращаются `version`, стабильный `total`, `limit`,
`offset` и rows. Cleaner прекращает чтение и начинает заново, если версия или
total неожиданно изменились. Предпочтительный интерфейс:

- `GET /baseline/candidates/meta` — текущий pointer и manifest;
- `GET /baseline/candidates/listings?version=...&limit=...&offset=...`;
- `GET /baseline/candidates/photos?version=...&limit=...&offset=...`;
- `GET /baseline/candidates/prices?version=...&limit=...&offset=...`;
- `GET /baseline/candidates/presence?version=...&limit=...&offset=...`.

Старый `/baseline/table` сохраняется на период миграции и отката.

### 4.2 Обязательные поля listings

Кроме действующих полей baseline:

- `id`, `url`, `status`;
- `first_seen_at`, `last_seen_at`, `missing_detected_at`;
- `price`, `initial_price`, `price_drop_count`, `reactivation_count`;
- комнаты, площадь, этаж, адрес, ЖК и координаты;
- текстовые поля для verdict;
- `photo_urls`, `photo_count`, `photo_set_hash`.

`photo_set_hash` считается детерминированно по согласованному набору URL.
`photo_count == len(photo_urls)` обязательно. Cleaner отклоняет весь снимок,
если эти три значения противоречат друг другу.

### 4.3 Price history

Минимальная строка:

```text
(listing_id, observed_at, price, event_type, source_event_id)
```

`source_event_id` уникален и стабилен. Допустимые типы включают bootstrap,
price_change и восстановленные исторические агрегаты с явным признаком
неполноты. Price history входит в ту же immutable version.

### 4.4 Presence history

Минимальная строка:

```text
(source_event_id, listing_id, event_type, status_after, observed_at,
 history_complete)
```

События: `first_seen`, `seen`, `missing_confirmed`, `reactivated`. Повторы одного
`source_event_id` не создают второй эффект.

Точную старую presence history задним числом восстановить нельзя. Для
существующих строк известны только текущее состояние и отдельные агрегаты.
Поэтому bootstrap-события помечаются `history_complete=false`; достоверная
история начинается с момента внедрения событий в Collector.

## 5. Надёжный online flow

Периодический snapshot нужен для полного baseline и восстановления. Быстрый
путь новых событий не должен ждать фотографий или ручного review.

1. Collector отдаёт source event с уникальным `source_event_id` и курсором.
2. Cleaner в одной транзакции сохраняет inbox event и создаёт необходимые
   jobs/outbox records.
3. После commit Cleaner подтверждает cursor. Повтор того же события безопасен.
4. Text verdict и entity/photo resolution выполняются независимо после приёма.
5. Когда entity готова, Cleaner публикует отдельное entity event.
6. Ошибка загрузки фото или ожидание review не блокирует следующие source
   events.

Пока такого курсорного API Collector нет, Cleaner работает полными версиями.
Это не нарушает корректность baseline, но увеличивает задержку определения
перевыложенного объявления.

## 6. Поиск пар-кандидатов

Cleaner строит shortlist, а не выносит окончательный вердикт. Текущие причины:

- одинаковый `photo_set_hash` URL;
- точный адрес + комнаты + округлённая площадь + этаж;
- ЖК + комнаты + округлённая площадь + этаж;
- близкая координатная ячейка вместе со структурными полями.

Координаты — только положительный сигнал. Большое расстояние не является
ошибкой или hard conflict: автор объявления мог поставить неверную точку даже
в нескольких километрах.

Структурные противоречия — разные комнаты, существенная разница площади,
несовместимый этаж или два достоверных разных адреса. Они отправляют пару в
review, но в shadow-режиме ничего не удаляют.

Пары всегда канонизируются как `listing_id_low < listing_id_high`, поэтому
A–B и B–A — одна пара.

## 7. Фото evidence

Одинаковый URL или даже одинаковый набор URL не доказывает одну квартиру:
агенты и застройщики используют типовые фотографии, фасады, планы и рендеры.
Поэтому сейчас нет production-порога «два похожих фото = дубль».

Фотооценка покупки должна:

- сравнивать оригинальные цветные изображения;
- различать кухню, зал, спальню, санузел, фасад, план и рендер;
- не засчитывать один кадр несколько раз после crop/resize/watermark;
- делать one-to-one matching кадров между listings;
- учитывать распространённость photo asset среди разных квартир;
- сохранять совпадения и устойчивые противоречия, а не только общий score;
- отличать переставляемые предметы от устойчивой геометрии: расположения
  духовки, сантехники, дверей, окон, встроенного гарнитура и отделки.

Perceptual fingerprint или grayscale pHash допустимы только как дешёвый поиск
кандидатов внутри фотооценки. Финальное сравнение должно видеть цвет и детали.
Cleaner не определяет конкретный алгоритм нормализации изображения.

Evidence принимается только для текущего задания и с версиями всех входов:

```text
candidate_revision_hash
low/high identity_hash
low/high photo_set_hash
low/high photo_content_set_hash
low/high photo_evidence_set_hash
normalization_version
fingerprint_version
intra_listing_dedupe_version
photo_role_model_version
embedding_version (nullable)
photo_asset_stats_version
matcher_version
```

Из этих полей считается `match_revision_hash`. Изменение любого входного
набора или алгоритма создаёт новую revision; предыдущая остаётся для аудита.
Main обязан вернуть `candidate_revision_hash`, `identity_hash` и
`photo_set_hash`, которые получил из review job. Если candidate или фотографии
уже изменились, Cleaner отвечает HTTP 409, не сохраняет устаревший результат и
оставляет новую revision в `awaiting_photo_analysis`.
Полный evidence JSON сохраняется. Высокий score сейчас не запускает merge.

Минимальный write-контракт Main → Cleaner:

```json
{
  "listing_id_a": "123",
  "listing_id_b": "456",
  "evidence": {
    "candidate_revision_hash": "...",
    "decision": "likely_same",
    "score": 0.91,
    "normalization_version": "...",
    "fingerprint_version": "...",
    "intra_listing_dedupe_version": "...",
    "photo_role_model_version": "...",
    "embedding_version": "...",
    "photo_asset_stats_version": "...",
    "matcher_version": "...",
    "listing_a": {
      "identity_hash": "...",
      "photo_set_hash": "...",
      "photo_content_set_hash": "...",
      "photo_evidence_set_hash": "..."
    },
    "listing_b": {
      "identity_hash": "...",
      "photo_set_hash": "...",
      "photo_content_set_hash": "...",
      "photo_evidence_set_hash": "..."
    },
    "matches": [],
    "conflicts": []
  }
}
```

`listing_a` и `listing_b` соответствуют ID верхнего уровня; Cleaner сам
канонизирует порядок пары. `embedding_version` может быть `null`, остальные
версии обязательны. Повтор того же payload идемпотентен.

## 8. Ключи и версии таблиц Cleaner

| Таблица | Ключ/версия | Назначение |
|---|---|---|
| `property_entities` | `entity_id` | Текущее состояние физической квартиры |
| `entity_members` | `listing_id` | Текущая принадлежность listing |
| `entity_membership_history` | append-only `history_id`; одна открытая запись на listing | История перемещений |
| `listing_entity_state` | `listing_id` | Текущее состояние listing |
| `listing_identity_snapshots` | `listing_id` | Компактные данные для review |
| `listing_price_observations` | unique `(listing_id, source_version, type, price)` | Локальная история цены |
| `listing_presence_events` | unique `(listing_id, type, status_after, observed_at)` | Локальная история присутствия |
| `entity_match_candidates` | canonical pair + `candidate_revision_hash` | Версии shortlist |
| `entity_match_edges` | canonical pair + `match_revision_hash` | Версии photo evidence |
| `entity_operations` | unique `idempotency_key` | Идемпотентные операции |
| `entity_events` | unique `(operation_id, event_seq)` | Несколько событий одной операции |
| `entity_overrides` | canonical pair | Последнее ручное решение |
| `canonical_history` | append-only; одна открытая запись на entity | История canonical |
| `entity_aliases` | `old_entity_id` | Merge old → surviving |
| `entity_lineage` | `(operation_id, child_entity_id)` | Происхождение после split |

Ручной API по умолчанию выключен (`ENTITY_MANUAL_WRITES_ENABLED=0`). Split не
является частью обычного online flow и не публикуется отдельным endpoint: он
остаётся аварийной внутренней операцией. При аварийном split alias, созданный
merge, перенаправляется в child только если туда ушли все его прежние members;
неоднозначный alias инвалидируется, а не указывает на неправильную квартиру.

## 9. Canonical и чистый baseline

Сначала рассматриваются только members, у которых:

1. `baseline_eligible=true` (`usable != false`);
2. verdict соответствует текущему text `content_hash`;
3. строка целиком доступна в текущем согласованном snapshot.

Затем:

1. среди активных готовых members выбирается минимальная валидная цена;
2. при равной минимальной цене сохраняется действующий canonical, чтобы не
   создавать лишнее переключение;
3. если готовых активных нет, выбирается самая свежая готовая historical row;
4. новая active строка без актуального verdict не вытесняет проверенную;
5. публикуется целиком выбранная строка, дополненная entity-метаданными.

В baseline также добавляются `entity_id`, исходный `listing_id`, количество
members, число активных members, минимальная и максимальная активная цена,
`first_seen_at`, `last_seen_at`, `relist_count` и lifecycle status.

Lifecycle всегда пересчитывается по истории всех members после позднего merge
или split. Порядок выполнения фоновых jobs не должен менять результат.

## 10. Merge, split и стабильность identity

Merge выбирает surviving `entity_id`, переносит members, закрывает старые
membership intervals, создаёт новые, пишет alias, operation и несколько events.
Повтор с тем же idempotency key не создаёт второй эффект.

Обычный alias корректен только для merge: `old → surviving`. Split может
создать несколько потомков, поэтому alias для него запрещён. Split пишет
lineage и событие с полным составом перемещённых listings. Потребитель обязан
инвалидировать старое решение по родительской entity и обработать каждого
потомка отдельно. Пока потребитель не поддерживает это поведение,
автоматические split и auto-merge включать нельзя.

## 11. Результаты анализа текущего Collector

Read-only анализ live snapshot версии `130375733c39f388` дал 21 550 строк:

- 13 560 active и 7 990 missing;
- 1 587 объявлений со снижением цены;
- 1 400 объявлений с reactivation;
- 5 196 metadata candidate pairs;
- 2 880 пар без metadata conflict, ожидающих фотоанализ;
- 2 316 пар со структурными конфликтами;
- 1 840 active–active пар.

Нашлись пары с одинаковым набором photo URL, но очень разными ценами. Это
подтверждает, что `exact_photo_set` нельзя считать готовым auto-link правилом.
Повторить анализ без OpenAI и без записи в production DB:

```powershell
.venv\Scripts\python.exe entity_check.py
```

## 12. Порядок безопасного внедрения

1. Cleaner работает в shadow и накапливает кандидатов — реализовано.
2. Collector добавляет immutable pre-dedupe candidate snapshot.
3. Cleaner переключается на новый snapshot, старый контракт остаётся fallback.
4. Фотооценка покупки читает review candidates и возвращает evidence.
5. На production-парах формируется ручная truth-выборка.
6. Измеряются precision/recall по типам квартир, ЖК и фото.
7. Порог auto-link принимается только после согласования допустимой ошибки.
8. Потребитель baseline проверяется на `entity_id` и merge; аварийный split
   тестируется отдельно как recovery-сценарий.
9. Включается entity baseline с периодом отката.
10. Только после этого Collector отключает старую дедупликацию.

## 13. Открытые продуктовые решения

- размеченный production-набор и допустимая ошибка auto-link;
- окончательный matcher и пороги по типам evidence;
- срок хранения immutable Collector versions;
- транспорт быстрого source-event потока;
- интерфейс запуска фотооценки покупки;
- точный grace period перед `missing_confirmed`/`off_market`;
- процедура ручного recovery после редкого ошибочного merge.

До закрытия этих решений система остаётся shadow-only: она показывает
подозрительные пары и позволяет явные ручные операции, но не объединяет
объявления автоматически и не отключает dedupe Collector.
