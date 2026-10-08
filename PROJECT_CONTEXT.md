# Контекст проекта для продолжения в новом чате

Актуально на: **2026-10-09**.

Этот файл — единственный handoff-контекст. При следующем крупном обновлении его нужно **переписать**, а не накапливать в нём старые планы и журналы обсуждений.

## 1. Что это за проект

Рабочая папка:

`C:\Users\Абылай\PycharmProjects\ai pokupka astana`

Это **AI Cleaner объявлений о продаже квартир в Астане**. Это не аренда и не отдельный новый сервис `Sales Main`.

Задачи Cleaner:

1. Получать baseline объявлений Collector.
2. Отсеивать неподходящие для сравнения объявления и сохранять verdict.
3. Не терять missing-объявления и историю их присутствия/цен.
4. Объединять разные объявления одной физической квартиры в стабильную сущность.
5. Публиковать чистый baseline, пригодный для расчёта рыночной цены.

Термины:

- `listing_id` — исходный ID объявления Krisha.
- `entity_id` — стабильный внутренний ID физической квартиры, присваиваемый Cleaner.
- У одной `entity_id` может одновременно быть 2, 6 и больше активных `listing_id` от разных риелторов.
- Исходные объявления нельзя удалять: они остаются отдельными members сущности со своей историей.

## 2. Границы ответственности

### Collector продажи квартир

Должен собирать и хранить отдельные объявления Krisha и факты наблюдения:

- `first_seen_at`;
- `last_seen_at`;
- `missing_detected_at`;
- `initial_price`;
- полную историю изменения цены;
- события появления, исчезновения и возврата;
- фотографии и стабильный `photo_set_hash`.

В будущем Collector должен отдавать Cleaner immutable snapshot **до бизнес-дедупликации**. Старый `/baseline/table` сохраняется на время миграции.

### AI Cleaner — этот репозиторий

Отвечает за:

- текстовую проверку объявлений о продаже;
- хранение verdict и чистого snapshot;
- модель `listing_id -> entity_id`;
- lifecycle сущности и историю members;
- shadow-кандидатов на дубли;
- проверку и хранение версионированного photo evidence;
- выбор одной целой canonical-строки для публикации.

### Purchase Main

В будущем выполняет тяжёлую обработку фотографий: загрузку/хранение изображений, fingerprints, распознавание комнат и сравнение. Cleaner не должен переносить к себе этот тяжёлый контур; он создаёт задания и принимает evidence по контракту.

Репозиторий Almaty Main относится к **аренде в Алматы** и использовался только как технический пример. Его арендные правила нельзя переносить в продажи.

## 3. Согласованная логика дублей

- Несколько одновременных активных объявлений одной квартиры — нормальный случай.
- Все `listing_id` сохраняются; Cleaner объединяет их под одной `entity_id`.
- Для baseline публикуется одна **цельная** строка одного объявления. Нельзя брать цену из одного объявления, описание из второго и фото из третьего.
- Canonical выбирается только среди members, у которых:
  - объявление допустимо для baseline;
  - есть актуальный verdict для текущего `content_hash`;
  - объявление активно, если есть хотя бы один готовый активный member.
- Среди готовых активных дублей приоритет — **минимальная валидная цена**. При равной цене сохраняется текущий подходящий canonical или выбирается более свежий/качественный источник.
- Непроверенный новый listing не должен вытеснять проверенный canonical.
- Missing members не удаляются и продолжают участвовать в lifecycle и аналитике истории цен.
- Координаты — только положительный/вспомогательный сигнал. Ошибка координат даже на несколько километров не является самостоятельным доказательством, что объявления разные.
- Фото должны сравниваться по реальному интерьеру и устойчивым деталям: геометрия комнаты, расположение кухни/духовки, сантехника и т. п. Переставляемые предметы не считаются структурным конфликтом.
- Один и тот же кадр после crop/resize/watermark нельзя засчитывать несколько раз.
- Фасады, планы, рендеры и типовые фото ЖК имеют слабую доказательную силу; нужно учитывать распространённость одного photo asset среди разных квартир.
- Производственного порога auto-link пока нет. Формула «достаточно двух похожих интерьерных фото» — лишь гипотеза до разметки production-пар.
- Matcher работает только в shadow mode. **Auto-merge выключен**.
- Публичного split API нет. Внутренний emergency split оставлен для исправления ошибок.

## 4. Что уже реализовано

### Entity resolution и evidence

- Добавлены entity schema, members, история membership, lifecycle, aliases, events, review jobs и match edges.
- Evidence привязан к конкретной revision кандидата и версиям обоих объявлений.
- Evidence обязан вернуть:
  - `candidate_revision_hash`;
  - `identity_hash` каждой стороны;
  - `photo_set_hash` каждой стороны;
  - версии fingerprint, photo-role model, embedding и matcher.
- Устаревшее evidence отклоняется; API отвечает HTTP `409`.
- Пара объявления канонизируется, поэтому A–B и B–A не образуют две независимые записи.
- Изменение фото или identity данных инвалидирует старое решение и создаёт/возвращает актуальное shadow-задание.
- Candidate revision учитывает identity hashes, причины, conflicts, positive signals, source version и generator version.
- Manual writes по умолчанию выключены через `ENTITY_MANUAL_WRITES_ENABLED=0`.
- Write API fail-closed: если ключ не настроен, ответ `503`; неверный ключ — `401`.

### Canonical и цены

- Canonical выбирается только среди готовых members с актуальным verdict.
- `entity_active_min_price` и `entity_active_max_price` считаются только по готовым активным кандидатам, а не по непроверенным/отклонённым объявлениям.
- Сохраняются отдельно:
  - количество всех активных listings;
  - количество готовых активных listings.

### SQLite и надёжность

- `SCHEMA_VERSION=3` хранится через `PRAGMA user_version`.
- Есть идемпотентные миграции старых verdict, batch и entity таблиц.
- На каждом соединении включается `PRAGMA foreign_keys=ON`.
- После миграции выполняется `foreign_key_check`.
- Review-индекс больше не удаляется и не создаётся при каждом соединении.
- Merge покрыт транзакционным rollback/crash-тестом.
- Split aliases исправлены: однозначный alias переводится на child, неоднозначный помечается `split_ambiguous`, а не указывает на случайную квартиру.

### OpenAI Batch

- Модель по умолчанию: `gpt-6-luna`.
- `REASONING_EFFORT=medium`.
- `MIN_BATCH_SIZE=1`, поэтому хвост из 1–4 объявлений отправляется и relabel способен завершиться.
- Значение `MIN_BATCH_SIZE=999999` по-прежнему используется для dry-run без расходов.
- `MAX_BATCH_SIZE=3000`, `MAX_BATCHES_PER_CYCLE=10`: максимум 30 000 объявлений за цикл.
- В pending batch сохраняются `group_size`, `model` и `policy_version`; ingestion использует настройки именно отправленного batch даже после deploy.

### CSV и snapshot

- CSV заранее и атомарно строится на persistent disk во время публикации snapshot.
- API отдаёт его через `FileResponse`, без двойной копии всего файла в RAM через `StringIO/getvalue()`.
- Для старого snapshot CSV один раз создаётся при первом запросе.
- Хранятся current и previous artifact, более старые очищаются best-effort.
- `built_at` имеет точность до микросекунд.

### Тесты и документация

- Локально прошло **48/48 тестов**.
- Все Python-файлы компилируются.
- `git diff --check` прошёл; были только предупреждения о line endings.
- Обновлены `README.md`, `CLAUDE.md`, `ENTITY_RESOLUTION_DESIGN.md`, `render.yaml` и набор загрузки в `UPLOAD_TO_GITHUB`.

## 5. Важные текущие ограничения

1. Cleaner всё ещё читает старый дедуплицированный Collector endpoint `/baseline/table`. Поэтому он физически не может найти объявления, которые Collector удалил до передачи.
2. Новый immutable pre-dedupe candidate snapshot в Collector ещё не реализован/не подключён.
3. Production-интеграции с Purchase Main для photo evidence ещё нет.
4. Shadow matcher пока создаёт metadata-кандидатов; результаты на production-фотографиях ещё не размечены и не оценены.
5. Auto-merge не включён и старый dedupe Collector не отключён.
6. Локального `OPENAI_API_KEY` во время последней проверки не было, поэтому платный prompt regression на Luna не запускался.
7. Live dry-run с авторизованным Collector после последних изменений не выполнен.

Нельзя заявлять, что entity resolution полностью работает в production, пока пункты 1–4 не закрыты.

## 6. Следующий порядок работ

### Шаг 1 — проверить sales prompt на Luna

С реальным ключом выполнить:

```powershell
$env:OPENAI_MODEL="gpt-6-luna"
$env:REASONING_EFFORT="medium"
.venv\Scripts\python.exe prompt_check.py
```

Если есть ошибки: сравнить `none`/`low`/`medium`, исправить prompt или кейсы и добавить каждый реальный miss в `prompt_cases.json`. Не deploy до успешной проверки.

### Шаг 2 — live dry-run Cleaner без расходов OpenAI

Подключить Collector credentials только через environment, установить:

```powershell
$env:MIN_BATCH_SIZE="999999"
```

Проверить получение baseline, diff, rules, snapshot и entity shadow jobs. Не записывать API-ключи в репозиторий, логи или этот файл.

### Шаг 3 — доработать Collector продажи

Добавить version-consistent immutable snapshot до dedupe, в котором одна версия включает:

- listings;
- `photo_urls`, `photo_count`, `photo_set_hash`;
- price history;
- presence events;
- стабильные `version`, `total` и pointer.

Входное событие Cleaner сначала надёжно сохраняет и создаёт jobs, затем продвигает cursor. Cursor не должен ждать скачивания фото или ручного review. Повтор source event не должен давать второй эффект.

Точную старую presence history задним числом восстановить невозможно: для старых строк доступны только текущее состояние и имеющиеся агрегаты. Достоверная последовательная история начинается после внедрения событий.

### Шаг 4 — интегрировать существующий Purchase Main

Main должен:

- получать `/entity-matches/review`;
- загружать/хранить компактные фото и fingerprints;
- делать perceptual dedupe внутри каждого listing;
- определять photo role/комнаты;
- сравнивать интерьер и структурные конфликты;
- отправлять в Cleaner полное version-consistent evidence по контракту из `ENTITY_RESOLUTION_DESIGN.md`.

Не переносить в Main sales baseline и не создавать новый отдельный сервис без необходимости.

### Шаг 5 — оценить shadow matcher

- Накопить реальные пары.
- Разметить production truth sample вручную.
- Посчитать precision/recall отдельно для link, no-link и review.
- Исследовать ложные совпадения по типовым фото ЖК и похожим планировкам.
- Только после этого согласовать production thresholds и отдельно решить вопрос auto-merge.

### Шаг 6 — миграция без одномоментного переключения

- Сначала включить новый Collector snapshot параллельно старому.
- Проверить Cleaner и Main end-to-end.
- Сохранить период отката.
- Только после приёмки отключать старую дедупликацию Collector.

## 7. Production-инфраструктура и стоимость

Принято решение перейти с Render на стандартный европейский **OVH VPS-3**:

- Ubuntu 24.04;
- 6 vCPU, 12 GB RAM, 100 GB NVMe;
- Collector, Cleaner и будущий purchase Main в отдельных Docker-контейнерах;
- общий приватный Compose network, но отдельные bind-mounted data directories;
- порты API доступны на хосте только через `127.0.0.1`/SSH tunnel;
- ежедневный согласованный SQLite backup плюс зашифрованная внешняя копия;
- существующий Aeza SWE-2 можно использовать как restic backup target/cold reserve.

Файлы deployment находятся в `deploy/ovh/`. Первый запуск безопасный:
`COLLECTOR_PAUSED=1`, `MIN_BATCH_SIZE=999999`, purchase Main выключен профилем.
Render хранится как rollback до нескольких полных циклов и проверенного restore.

Важное ограничение: локальная папка `rieltor pokupka kvart` фактически содержит
арендный Main (арендный prompt, thresholds и baseline). Его Dockerfile можно
использовать технически, но профиль `purchase-main` нельзя включать, пока Main
не адаптирован и не проверен именно для продажи квартир.

Последняя публичная цена OVH VPS-3 была около **€12.4/месяц с НДС**; цену в
кабинете нужно перепроверить перед заказом. SLA VPS — 99.9%, поэтому один VPS
остаётся single point of failure; внешний backup обязателен.

### Объём данных

Последний замер live baseline (может уже измениться):

- версия: `2ff4565311c777ad`;
- 21 551 строка;
- JSON около 79.43 MiB;
- 332 791 ссылок на фото;
- 332 422 уникальных URL;
- в среднем 15.44 фото на listing;
- 5 196 metadata-кандидатов: 2 880 awaiting photo и 2 316 conflicts;
- entity-only SQLite около 105 MiB;
- ожидаемый полный DB: примерно 200–300 MiB.

Оценка прикладных расходов:

- полный relabel 21 551 объявления через Luna Batch при прежнем объёме токенов: ориентировочно **$0.88**, но reasoning может увеличить расход;
- OVH VPS-3: ориентировочно **€12.4/месяц**, плюс небольшой объём внешнего backup;
- 332 тыс. миниатюр по 3–5 KB: около **0.93–1.55 GiB**. Хранить их следует в photo-контуре Main, не в Cleaner; полные оригиналы постоянно кэшировать не нужно.

Цена Luna подтверждалась по официальной документации OpenAI: стандартно $0.10/M input и $0.50/M output; Batch/Flex — 50% стандартной цены. Перед финансовым решением перепроверить актуальную страницу модели: https://developers.openai.com/api/docs/models/gpt-6-luna

## 8. Основные файлы

- `heuristics.py` — только консервативные бесплатные правила продажи: явная доля/часть объекта и явное нежилое помещение. Не добавлять арендные правила.
- `openai_batch.py` — sales prompt и OpenAI Batch.
- `collector_client.py` — текущий старый `/baseline/table`, согласованная пагинация и проверка photo hash.
- `pipeline.py` — порядок цикла, relabel, batches и публикация.
- `cleaner_db.py` — SQLite, migrations, verdicts, batches, clean snapshot и CSV.
- `entity_store.py` — entity, membership/history, lifecycle, canonical, merge, emergency split и evidence.
- `entity_matcher.py` — shadow metadata candidate generation.
- `verdicts_api.py` — clean baseline, review/entity/evidence API.
- `test_sales_cleaner.py` — 48 локальных тестов.
- `ENTITY_RESOLUTION_DESIGN.md` — детальная архитектура и контракты.
- `README.md` — эксплуатация и конфигурация.
- `deploy/ovh/` — основной Docker Compose, backup/restore и runbook миграции.
- `render.yaml` — только временный rollback reference на период миграции.
- `UPLOAD_TO_GITHUB/` — механическая копия файлов для ручной загрузки пользователем; после изменений её нужно синхронизировать и сверять hashes.

## 9. Команды проверки

```powershell
.venv\Scripts\python.exe -m unittest -v test_sales_cleaner.py
.venv\Scripts\python.exe -m compileall .
git diff --check
```

`prompt_check.py` вызывает реальную модель и требует ключ; это отдельная платная проверка.

## 10. Что нельзя делать без отдельного согласования

- Не переносить правила аренды в продажи.
- Не удалять missing listings и source `listing_id`.
- Не включать auto-merge до production-разметки.
- Не считать два похожих фото готовым production-порогом.
- Не отключать dedupe Collector до параллельной миграции и end-to-end приёмки.
- Не смешивать поля нескольких listings в одну опубликованную строку.
- Не делать координаты hard conflict.
- Не хранить секреты в tracked-файлах.
- Не изменять Almaty rental Main как часть этой задачи.
- Не выполнять `git reset --hard` и не стирать текущую рабочую директорию: в ней много подготовленных/незакоммиченных файлов.
