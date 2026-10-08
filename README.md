# rieltor-sales-cleaner-astana

Второй микросервис продажной системы Астаны. Он получает immutable baseline
из `pokupka-rieltor-collector`, определяет, пригодна ли строка для сравнения
цен целых квартир, и публикует собственный согласованный clean snapshot.

```text
pokupka-rieltor-collector  --/baseline/table-->  sales-cleaner  --/baseline/clean-->  consumer
```

Collector и Cleaner разделены намеренно: Krisha и OpenAI имеют разные режимы
отказа. HTTP-запрос потребителя никогда не обращается к Collector.

## Продуктовая политика v1

`usable=true` означает: в объявлении указана сопоставимая цена продажи целой
квартиры. Cleaner не проверяет мошенничество и не решает, выгодна ли цена.
Текущая версия правил и prompt: `sales-astana-v1`; она сохраняется рядом с
каждым вердиктом для аудита.

Стабильные причины исключения:

| `reason_code` | Что означает |
|---|---|
| `partial_property` | Доля, комната или часть квартиры вместо целого объекта |
| `not_apartment` | Дом, офис, склад, паркинг или другой объект вместо квартиры |
| `not_sale` | Аренда/услуга/другое объявление вместо продажи |
| `price_not_full` | Взнос, платёж, цена за м² или рекламная цена вместо полной цены |
| `legal_restriction` | Арест, запрет, отсутствие документов или неузаконенный объект |
| `auction_distress` | Аукцион, торги или принудительная реализация |
| `non_market_terms` | Обмен, перевод долга или встречное обязательство искажает цену |
| `other_red_flag` | Очевидно несопоставимая цена, не покрытая кодами выше |
| `ok` | Явных оснований исключить строку нет |

Политика консервативна: сомнение означает `ok`, а не исключение. Срочность,
торг, старая/черновая отделка, отсутствие мебели, ипотека с обычным погашением,
переуступка, объект от инвестора и строящийся дом сами по себе не являются
причиной отбраковки. Это сохраняет данные до появления размеченной выборки и
отдельного продуктового решения по таким сегментам.

Collector уже выполняет проверку обязательных полей и города, широкие
санитарные границы площади/цены, seller policy и fuzzy-дедуп. Cleaner не
повторяет эти фильтры.

## Цикл обработки

1. Получить одну согласованную версию Collector постранично. При смене
   `version` во время обхода начать чтение заново.
2. Забрать результаты ранее отправленных OpenAI Batch-заданий.
3. Прогнать бесплатные однозначные правила над всеми строками.
4. Сравнить собственный `content_hash` и отправить только новые или
   содержательно изменившиеся объявления.
5. Атомарно заменить stored clean snapshot.

`content_hash` включает только вход текстового классификатора:

```text
title, full_description, rent_renovation, priv_dorm, square_m2, rooms
```

Цена, `status`, timestamps, этаж и фотографии в него не входят. Поэтому
изменение цены или перевод строки в `missing` не оплачивает повторный AI-вызов.
`missing` остаётся в clean baseline как историческое сравнение, если его
публикует Collector.

Правила ретроактивны: каждый цикл они проверяют и известные строки. Изменение
prompt не ретроактивно; для полного перепрогона задаётся новое значение
`CLEANER_RELABEL_GEN`. Пока relabel не завершён, потребителю отдаётся прежний
полный snapshot, а не частичный новый.

## Надёжность

- `in_flight_ids()` предотвращает повторную оплату pending batch.
- `MAX_LLM_ATTEMPTS` ограничивает переотправку битых ответов модели.
- `usable=null` после исчерпания попыток означает «неизвестно» и публикуется;
  техническая ошибка не превращается в отрицательный вердикт.
- completed batch без `output_file_id` считается неуспешным и освобождает
  строки для повторной обработки.
- пустой clean snapshot никогда не заменяет предыдущий.
- публикация и метаданные записываются одной SQLite-транзакцией.
- пагинация вердиктов сортируется по `(processed_at, id)`.
- при недоступном Collector готовые оплаченные batch всё равно забираются, а
  прошлый clean snapshot остаётся доступным.

## Физические квартиры и дубли

`listing_id` — идентификатор отдельной публикации Krisha. `entity_id` —
стабильный идентификатор физической квартиры внутри Cleaner. Исходные
`listing_id` не удаляются: они остаются members одной entity вместе с историей
появления, исчезновения, возвращения и наблюдаемой цены.

При нескольких готовых активных объявлениях одной entity в clean baseline
выходит одна целая строка объявления с минимальной корректной активной ценой.
Поля разных объявлений не смешиваются. При равной минимальной цене текущий
canonical сохраняется, чтобы snapshot не переключался без причины. В строку
добавляются `entity_member_count`, `entity_active_listing_count`,
`entity_ready_active_listing_count`, `entity_active_min_price` и
`entity_active_max_price`. Диапазон цен считается только по проверенным active
members: непроверенная дешёвая публикация цену entity не искажает.

Первое наблюдение существующего объявления помечается `history_complete=0`:
точную старую историю задним числом восстановить нельзя. Последующие переходы
и изменения цены, увиденные Cleaner, добавляются неизменяемыми событиями с
`history_complete=1`.

Cleaner не скачивает и не оценивает фотографии. Он создаёт приоритетную
shadow-очередь пар по точному `photo_set_hash`, адресу, ЖК и близким
координатам. Координаты дают только положительный сигнал и никогда не являются
причиной отклонить пару. Фотоанализ будет выполняться в Main покупки и
возвращать версионированные evidence. `auto_merge` сейчас всегда выключен.

Read-only анализ текущего Collector без OpenAI и без записи в production DB:

```powershell
.venv\Scripts\python.exe entity_check.py
```

## API

Если задан `CLEANER_API_KEY`, все endpoints кроме `/health` требуют
`X-API-Key`. Операции записи fail-closed: если ключ не настроен, они отвечают
503, а не становятся публичными. Ручные entity-изменения дополнительно
выключены по умолчанию и требуют `ENTITY_MANUAL_WRITES_ENABLED=1`.

| Endpoint | Назначение |
|---|---|
| `GET /baseline/clean` | Clean snapshot в JSON с `limit`/`offset` |
| `GET /baseline/clean.csv` | Весь snapshot одним CSV |
| `GET /verdicts/table` | Вердикты и фильтры по ним |
| `GET /verdicts/review` | Отбракованные строки с сохранённым текстом |
| `GET /verdicts/meta` | Статистика и состояние циклов |
| `GET /entities/stats` | Число entities, members, событий и shadow-кандидатов |
| `GET /entities/{entity_id}` | Members, история присутствия/цен и canonical history |
| `GET /entity-matches/review` | Приоритетная очередь подозрительных пар; только shadow |
| `POST /entity-matches/evidence` | Сохранить версионированный результат фотооценки покупки; merge не выполняется |
| `POST /entity-matches/manual-decision` | Аварийное ручное решение; по умолчанию выключено |
| `GET /health` | Healthcheck платформы |

До первой непустой публикации `/baseline/clean*` возвращает 503. Поле
`built_at` меняется при каждой пересборке; JSON-потребитель должен начать
пагинацию заново, если оно изменилось между страницами.

## Запуск

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
.\run.ps1
```

Только API, без платного цикла:

```powershell
.venv\Scripts\python.exe -m uvicorn verdicts_api:app --port 8002
```

Полный dry-run интеграции без отправки в OpenAI:

```powershell
$env:MIN_BATCH_SIZE = "999999"
.venv\Scripts\python.exe service.py
```

## Production на OVH

Основной production target — обычный европейский OVH VPS с Ubuntu 24.04.
Collector, Cleaner и будущий purchase Main запускаются раздельными
контейнерами через [deploy/ovh/compose.yaml](deploy/ovh/compose.yaml). SQLite
файлы не разделяются между контейнерами: сервисы взаимодействуют только по
внутреннему HTTP.

Полная инструкция, безопасные значения первого запуска, backup/restore и
порядок переноса с Render находятся в [deploy/ovh/README.md](deploy/ovh/README.md)
и [deploy/ovh/MIGRATION_RUNBOOK.md](deploy/ovh/MIGRATION_RUNBOOK.md).

По умолчанию OVH-конфигурация не создаёт внешних запросов и платных batch:

- `COLLECTOR_PAUSED=1`;
- `MIN_BATCH_SIZE=999999`;
- Main выключен Compose-профилем;
- API привязаны только к `127.0.0.1` хоста.

`render.yaml` временно сохранён только для отката. Render нельзя удалять до
проверки нескольких полных OVH-циклов и тестового восстановления из внешнего
backup.

## Проверки

Офлайн-тесты не обращаются к Krisha или OpenAI:

```powershell
.venv\Scripts\python.exe -m unittest discover -v
```

После каждого изменения `SYSTEM_PROMPT` дополнительно запускается платный
регрессионный набор на той же модели, что и production:

```powershell
$env:OPENAI_MODEL = "gpt-6-luna"
$env:REASONING_EFFORT = "medium"
.venv\Scripts\python.exe prompt_check.py
```

## Переменные окружения

| Переменная | Default | Назначение |
|---|---:|---|
| `DATA_DIR` | `.` | Persistent-каталог состояния |
| `CLEANER_DB` | `$DATA_DIR/sales_cleaner_astana.db` | Явный путь SQLite |
| `COLLECTOR_URL` | — | URL продажного Collector |
| `COLLECTOR_API_KEY` | — | Его `BASELINE_API_KEY` |
| `OPENAI_API_KEY` | — | Ключ только для текстовой разметки |
| `OPENAI_MODEL` | `gpt-6-luna` | Модель текстовой классификации |
| `REASONING_EFFORT` | `medium` | Глубина рассуждения; менять только после regression |
| `CLEANER_API_KEY` | пусто | Ключ API Cleaner |
| `CLEANER_CYCLE_INTERVAL_H` | `12` | Интервал полного цикла |
| `CLEANER_INGEST_TICK_S` | `300` | Проверка готовых batch между циклами |
| `CLEANER_FULL_RETRY_S` | `900` | Повтор полного цикла после ошибки |
| `GROUP_SIZE` | `20` | Объявлений в одном model request |
| `MAX_BATCH_SIZE` | `3000` | Объявлений в одном batch |
| `MAX_BATCHES_PER_CYCLE` | `10` | До 30 000 объявлений за цикл при batch 3000 |
| `MIN_BATCH_SIZE` | `1` | Хвост отправляется всегда; `999999` — dry-run без затрат |
| `PILOT_LIMIT` | `0` | Общий лимит AI-разметки; 0 = без лимита |
| `MAX_LLM_ATTEMPTS` | `3` | Повторы невалидного ответа |
| `ENTITY_SHADOW_ENABLED` | `1` | Формировать metadata-кандидатов дублей; никогда не выполняет auto-merge |
| `ENTITY_GRACE_HOURS` | `24` | Ожидание перед переводом entity без active members в `off_market` |
| `ENTITY_MANUAL_WRITES_ENABLED` | `0` | Разрешить аварийные ручные entity-изменения через API |
| `DESCRIPTION_MAX_CHARS` | `600` | Обрезка описания |
| `CLEANER_RELABEL_GEN` | пусто | Новое значение запускает полный relabel |
| `CLEANER_STALE_AFTER_S` | `259200` | Порог stale health |
| `PORT` | `8002` | HTTP-порт |

## Фото и состояние ремонта

Текстовая v1 не анализирует фотографии и не применяет денежную поправку за
ремонт. На границе Collector Cleaner всё же проверяет транспортный инвариант:
`photo_count` равен числу URL, а `photo_set_hash` равен SHA-256 от
отсортированных URL через `|`. Несогласованный снимок целиком не публикуется;
предыдущий clean baseline остаётся доступен.

Фотообработка остаётся в Main покупки и использует ключ:

```text
listing_id + photo_set_hash + evaluator_version
```

Cleaner не скачивает изображения повторно: Main возвращает ему только
версионированные fingerprints/evidence для конкретного набора фото. Ответ Main
обязан повторить `candidate_revision_hash`, `identity_hash` и `photo_set_hash`
обеих сторон. Устаревший ответ получает HTTP 409 и не меняет текущий результат. Смена
фотографий не должна сбрасывать текстовый verdict. До калибровки на продажных
данных interior score можно хранить и показывать, но нельзя молча превращать в
корректировку цены или фильтр. Реализация аренды Алматы используется только как
технический пример очереди и кэша; её продуктовые правила сюда не переносятся.
