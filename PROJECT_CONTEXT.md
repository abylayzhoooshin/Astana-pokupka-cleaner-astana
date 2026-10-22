# Актуальный контекст: Astana purchase Cleaner

Дата обновления: 2026-10-12.

## Что это

Это AI Cleaner объявлений о продаже квартир в Астане. Он является вторым
сервисом цепочки:

```text
Collector -> immutable candidate snapshot -> Cleaner -> clean baseline
```

`listing_id` — ID отдельного объявления Krisha. `entity_id` — внутренний
стабильный ID физической квартиры. Все исходные listing IDs сохраняются.
Одна квартира может иметь несколько одновременно активных объявлений.

Almaty Main относится к аренде и не является частью этого проекта. Его
`renovation_scorer.py` проверен только как технический пример low-detail vision:
он оценивает общий интерьер объявления, но **не определяет роль каждого фото**
(кухня, санузел и т. п.). В локальном purchase Main готового классификатора
ролей также нет. Поэтому сейчас нет скрытой готовой функции, которую достаточно
просто включить.

Для текущего production-запуска нужны только Collector и Cleaner. Технический
shadow photo worker уже реализован как отключённый профиль/ограниченный CLI.
Он является отдельным процессом и общается с Cleaner по HTTP; его нахождение в
этом репозитории не означает, что Cleaner-сервис должен выполнять vision внутри
своего рабочего цикла. Если позже появится единый purchase photo-evaluation
flow, worker можно подключить рядом с ним без создания отдельного Sales Main
только ради дублей.

Ближайший обязательный шаг перед следующими широкими фото-прогонами — добавить
дешёвую low-detail классификацию роли каждого уникального кадра с кэшем по
`content_sha256`. До этого массовый технический score нельзя использовать даже
как предварительный результат по дублям: он способен сопоставить ванную с
кухней, если после one-to-one matching остались только эти кадры.

## Зафиксированные продуктовые решения

- Задача проекта — очистить ценовой baseline продаж и корректно объединять
  перевыложенные/одновременные дубли. Отдельный Sales Main ради этого не нужен.
- Каждый `listing_id` сохраняется отдельным member. У одной `entity_id` может
  быть два, шесть и больше одновременно активных объявлений.
- В чистый baseline выходит одна цельная готовая строка. Среди проверенных
  активных members выбирается минимальная валидная цена; поля разных объявлений
  не смешиваются.
- `missing` не удаляются. Сохраняются `first_seen_at`, `last_seen_at`,
  `missing_detected_at`, начальная цена, вся доступная история цен, возвраты и
  периоды присутствия. Точная старая presence history до внедрения событий
  невосстановима и помечается incomplete.
- Collector хранит факты источника и отдаёт immutable pre-dedupe snapshot.
  Cleaner решает identity. Legacy dedupe Collector остаётся только для старого
  endpoint до проверенного переключения.
- Координаты являются только положительным сигналом: ошибка на несколько
  километров не запрещает дубль.
- Auto-merge выключен до ручной production-разметки. Split оставлен только как
  аварийное восстановление после ошибочного merge, а не как обычный flow.
- Фото сравниваются в цвете. Серый perceptual fingerprint допустим лишь как
  дешёвый shortlist-сигнал. Устойчивые различия планировки/встроенных объектов
  важнее переставляемых предметов.
- Cleaner не хранит сотни тысяч оригинальных бинарников. Отдельный worker
  хранит компактный кэш уникальных preview/fingerprint; повторный кадр не
  скачивается и не засчитывается несколько раз.
- Telegram review будет добавлен после role classifier и первичной truth-
  выборки, чтобы человеку показывались пограничные случаи, а не тысячи пар.
- Правила аренды и интерьерная оценка Алматы используются только как
  технический пример и не переносятся в продажи Астаны.

## Граница ответственности

Collector:

- хранит каждое объявление отдельно;
- сохраняет active/missing, цены и события присутствия;
- публикует `/baseline/candidates/*` до бизнес-дедупликации;
- временно сохраняет legacy `/baseline/table` с прежним dedupe для отката.

Cleaner:

- забирает одну закреплённую candidate version;
- сохраняет source price/presence events идемпотентно по `source_event_id`;
- создаёт singleton entity для каждого нового listing ID;
- формирует metadata shortlist возможных дублей;
- выполняет текстовую фильтрацию только для `baseline_eligible=1`;
- хранит ручные решения и версионированное photo evidence;
- выбирает одну целую готовую строку на entity;
- не выполняет auto-merge до проверки размеченной production-выборки.

Missing не удаляются и нужны для исторической цены, скорости исчезновения,
возвратов и снижения цены.

## Реализованный Collector контракт

Cleaner использует:

```text
GET /baseline/candidates/meta
GET /baseline/candidates/listings?version=...&limit=...&offset=...
GET /baseline/candidates/prices?version=...&limit=...&offset=...
GET /baseline/candidates/presence?version=...&limit=...&offset=...
```

Listing endpoint восстанавливает `photo_urls` из immutable `listing_photos`
той же версии. Cleaner проверяет каждую тройку `photo_urls`, `photo_count`,
`photo_set_hash` и сверяет сумму `photo_count` с `photos_total`. Поэтому
880k photo rows не скачиваются второй раз отдельным HTTP-обходом.

При недоступности candidate snapshot рабочий цикл завершается и оставляет
предыдущий clean baseline. Автоматического fallback на legacy baseline нет.

## Изменения Cleaner 2026-10-09

- `collector_client.py`: добавлен version-pinned candidate client и загрузка
  listings, prices и presence.
- `entity_store.py`: добавлены `collector_price_events`,
  `collector_presence_events`, `collector_candidate_ingests`; повторы source
  events не создают второй эффект, а конфликт одного ID с другим содержимым
  прерывает транзакцию.
- `pipeline.py`: entity resolution получает все candidate rows; text AI и
  clean baseline получают только `baseline_eligible=1`.
- `entity_store.py`: lifecycle/relist и entity detail используют точную
  Collector history, с fallback на старую локальную историю для legacy DB.
- `verdicts_api.py`: entity stats показывают source history.
- `entity_matcher.py`: metadata matcher остаётся shadow-only, `auto_merged=0`.
- Merge теперь детерминированно сохраняет entity с большим числом members,
  затем более старую; устранён случайный surviving ID и flaky split-тест.
- Документация и тесты обновлены под candidate contract.

## Проверенный локальный E2E

С production Render через SQLite online backup скачана согласованная копия:

```text
source listings: 61 050
active:          40 579
missing:         20 471
integrity_check: ok
source DB:       239.33 MiB
```

Оригинальная production DB не менялась. Временный `/tmp` backup на Render
после проверки локального SHA-256 удалён.

Миграция на отдельной локальной копии:

```text
listings до/после:       61 050 / 61 050
active до/после:         40 579 / 40 579
missing до/после:        20 471 / 20 471
legacy price history:    72 432 / 72 432
два прохода миграции:    идемпотентны
```

Candidate version `72698201052e2eb0`:

```text
listings:                60 893
photos:                 909 618
price observations:      72 238
presence events:         60 893
technically rejected:       157
HTTP full download:       22.33 s
```

Первый Cleaner dry-run без Krisha и OpenAI:

```text
elapsed:                  44.72 s
observed listings:        60 893
created singleton entity: 60 893
baseline eligible:        37 306
source events stored:    133 131
current metadata pairs:   99 028
metadata conflicts:       33 413
awaiting photo analysis:  65 615
auto merged:                   0
paid submissions:              0
```

Повторный цикл:

```text
new entities:             0
new source events:        0
presence transitions:     0
```

Локальная Cleaner DB сразу после первого полного цикла:

```text
size:                     378.06 MiB
peak working set:         633.57 MiB
integrity_check:          ok
foreign key violations:  0
```

Лимит 512 MiB недостаточен для текущего полного цикла. Для совместного запуска
Collector + Cleaner нужен сервер минимум с 2 GiB RAM; 4 GiB и выше оставляет
нормальный эксплуатационный запас. На планируемом OVH VPS-3 памяти достаточно.

Во время E2E локальные API успешно отвечали:

```text
Collector: http://127.0.0.1:8001
Cleaner:   http://127.0.0.1:8002
```

На 2026-10-12 эти процессы остановлены; перед продолжением тестов их нужно
запустить заново. Файлы E2E сохранены: исходная production-копия занимает
239.33 MiB, мигрированная Collector-копия — 305.74 MiB, текущая Cleaner DB
после записи 100 shadow evidence — 452.20 MiB. Повторная read-only проверка
дала `quick_check=ok`, `foreign_key_check=0`.

## Текущее состояние baseline

Dry-run использует `MIN_BATCH_SIZE=999999`, поэтому OpenAI не вызывался.
37 306 `baseline_eligible` строк пока ожидают текстового verdict. На холодной
БД clean baseline не публикуется, что правильно: пустой snapshot не должен
заменять отсутствующий/предыдущий полноценный результат.

Для платного этапа сначала нужно прогнать `prompt_check.py` на небольшой
размеченной выборке и только затем отправлять полный Batch. Ключ OpenAI должен
храниться только локально/в secrets manager. Ранее показанные в диагностике
ключи требуется отозвать и заменить.

## Важный результат по дублям

99 028 — не количество подтверждённых дублей, а широкий metadata shortlist.
65 615 пар нельзя сразу отправлять на дорогой photo matcher или ручной review.
Фактический состав shortlist:

```text
оба baseline_eligible:   88 052
только один eligible:    7 851
оба ineligible:          3 125
active-active:            28 117
active-missing:           40 352
missing-missing:          30 559
```

Анализ очереди выполнен и сохранён в `PHOTO_QUEUE_ANALYSIS.md`.
Подтверждено, что 266 старых exact-photo пар были созданы служебной
картинкой `photo-moderation-big.png`; matcher v4 их игнорирует.
Первый фото-пилот ограничен:

```text
both active + both eligible + 3 metadata reasons
6 383 пары / 4 701 listing / 89 589 фото
```

Реализован `photo_worker.py`: HTTPS/CDN allowlist, лимиты байтов и
пикселей, EXIF orientation, RGB preview, content SHA, dHash+RGB fingerprint,
intra-listing dedupe, one-to-one matching и версионированная запись evidence.
По умолчанию worker — dry-run; `--submit` нужен явно. Комнатных ролей и
калиброванного порога ещё нет, поэтому решение всегда `uncertain`.

Локальный write-E2E сохранил 2 evidence revision; после повторного
полного Cleaner-цикла оба остались `photo_evidence_ready`, merge=0.
Дополнительный dry-run 20 пар занял 14.88 s, повтор из кэша — 1.41 s;
4 photo URL вернули 404, но все 20 пар были проанализированы.

Затем выполнен расширенный локальный прогон 100 пар: 98 успешно, 2 без
пригодного фото, 17 отдельных ошибок загрузки на 945 URL. Median score
`0.705173`, 20 пар имели score ≥ `0.98`. Четыре верхние пары визуально
подтверждены как одна квартира с разными ракурсами. Все 98 результатов
сохранены только в локальный Cleaner как `uncertain`: всего current evidence
стало 100, manual/auto merge — 0, integrity/FK — без ошибок. Отфильтрованная
очередь первого слоя после этого содержит 6 283 пары.

CLI worker теперь печатает компактную сводку и умеет атомарно сохранять полный
отчёт через `--report-json`, чтобы массовый лог не обрезался.

Добавлен `--selection stratified`: 500 пар распределяются между девятью
уровнями `both_active`/`active_missing`/`both_missing` × 3/2/1 metadata-признак.
API поддерживает точный `reason_count` и воспроизводимый порядок по
`candidate_revision_hash`. Локальный dry-run обработал 489/500 пар, 11 были
без пригодного фото. Median технического score `0.987284` и остаётся высокой
даже на слабых уровнях, поэтому score признан некалиброванным сигналом, а не
вероятностью дубля. Эти 489 результатов в Cleaner не записывались.

После массового dry-run вручную проверен маленький диагностический набор:
три пары кухонь одной квартиры, три пары санузлов одной квартиры и контрольные
пары тех же типов из разных квартир. Для одинаковых помещений техническая
схожесть составила `0.986888–1.0`; для разных квартир — `0.509147–0.666420`.
Эти шесть положительных примеров подтверждают, что fingerprints полезны как
дешёвый сигнал, но выборка слишком мала для порога. Главное обнаруженное
ограничение: без роли кадра алгоритм иногда сопоставляет санузел с кухней.
Следовательно, классификация ролей должна выполняться **до** pair matching, а
не после него.

Даже эта очередь остаётся shadow-only:

1. точные photo hashes и самые сильные структурные совпадения;
2. active-active и active-missing отдельно;
3. hard conflicts не отправлять как обычные кандидаты;
4. Telegram показывает только пограничные/противоречивые случаи и контрольную
   выборку, а не десятки тысяч пар;
5. auto-merge остаётся выключен до production-разметки.

## Что осталось

сделай файл о том как работает поиск дублей и оценка горячих мест. и что еще мы новое вводили? ### Отложенный обязательный аудит Collector

Перед загрузкой/деплоем локального `rieltorcollector -pokupka` обязательно
вернуться к результатам проверки 2026-10-12:

1. Исправить нестабильные bootstrap presence events: при миграции одной и той
   же исходной БД в разные дни сохраняется тот же `source_event_id`, но другое
   `observed_at`. Повторная сборка дала 60 893 конфликтующих событий против
   существующей Cleaner DB. ID и payload должны строиться из стабильных данных
   исходной строки; нужен regression-тест двух независимо мигрированных копий.
2. Исправить время price observations: `upsert_price_only()` принимает
   `observed_at_by_id`, но `record_price_changes()` сохраняет общее время
   обработки `now`. История должна отражать фактическое время наблюдения.
3. Обновить устаревшие `PROJECT_CONTEXT.md` и
   `LOCAL_CLEANER_E2E_RUNBOOK.md` Collector: Cleaner уже читает candidate API.
4. Добавить `candidate_versions/` и `first_full_cycle_done.marker` в
   `.gitignore`/`.dockerignore`.
5. Текущий GitHub Collector остаётся на commit `86c587a` и ещё не содержит
   candidate-реализацию. Не загружать `UPLOAD_TO_GITHUB` до исправления пунктов
   выше. При этом локально 45/45 тестов проходят, candidate build на
   production-копии успешен, а 34 staging-файла совпадают с рабочими.

### Дальнейшие задачи Cleaner

1. Следующий шаг: добавить в photo worker дешёвую low-detail классификацию
   каждого уникального цветного preview: `kitchen`, `bathroom`, `bedroom`,
   `living_room`, `hallway`, `balcony`, `facade`, `plan`, `render`, `other`,
   `unknown`. Результат кэшируется по `content_sha256 + photo_role_model_version`.
   Сравниваются только совместимые роли; фасад, план и рендер не считаются
   сильным доказательством квартиры. Текущий технический слой оставляет все
   роли `unknown`, поэтому новые широкие прогоны до этой доработки не нужны.
2. Добавить учёт распространённости content fingerprint между разными
   квартирами, чтобы типовые фото ЖК не становились доказательством дубля.
3. Создать truth-выборку, после чего добавить Telegram review поверх
   `/entity-matches/review` и manual decision API.
4. Прогнать sales prompt на новой временной OpenAI key сначала на regression
   set, затем на ограниченной выборке.
5. После успешного parallel run решить, когда выключить legacy dedupe Collector.
6. Проверить уже подготовленные OVH Compose и backup/restore на чистом
   тестовом VPS до production-cutover.

## Основные файлы

- `collector_client.py` — candidate HTTP contract.
- `pipeline.py` — полный цикл и разделение all rows / baseline eligible.
- `entity_store.py` — entities, source histories, membership, lifecycle,
  canonical, merge/split и evidence.
- `entity_matcher.py` — shadow metadata shortlist.
- `photo_queue_report.py` — повторяемый read-only замер photo queue.
- `photo_worker.py` — ограниченный shadow photo pipeline с RGB-preview и evidence API.
- `PHOTO_QUEUE_ANALYSIS.md` — результаты и план photo-пилота.
- `cleaner_db.py` — SQLite verdicts, batches и clean snapshot.
- `verdicts_api.py` — clean/entity/review API.
- `test_sales_cleaner.py` — 61 локальный тест.
- `ENTITY_RESOLUTION_DESIGN.md` — подробная архитектура.

## Безопасность

- Не коммитить `.db`, `.env`, `env.ps1`, API keys и SSH private keys.
- Не запускать локальный Collector scraper против Krisha: для E2E используется
  только `uvicorn baseline_api:app` и локальная копия DB.
- Не включать auto-merge.
- Не удалять missing или исходные listing IDs.
- Не переносить арендные правила Алматы в продажи Астаны.
