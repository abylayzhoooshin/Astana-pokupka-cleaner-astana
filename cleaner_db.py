"""
cleaner_db.py — хранилище вердиктов и отслеживание batch-заданий.

ДВЕ ТАБЛИЦЫ, ДВЕ РАЗНЫЕ ЗАДАЧИ.

verdicts — по одной строке на listing id. Хранит ПОСЛЕДНИЙ вердикт
    (не историю) + content_hash, по которому решается, нужно ли
    объявление обрабатывать заново.

batches — по одной строке на batch-задание у провайдера. Нужна, чтобы
    пережить рестарт процесса между отправкой batch и получением
    результата (Batch API асинхронный, окно — часы, а не секунды;
    сервис вполне может перезапуститься в промежутке).

ПОЧЕМУ content_hash, А НЕ ПРОСТО "id уже обработан".
Одно и то же id может вернуться на сайт с другим описанием (хозяин
переписал текст) или, в нашем случае, "воскреснуть" из seed с ценой,
подтянутой обходом, но старым описанием месячной давности (известная
особенность collector lifecycle). Хэш берётся от полей, которые реально
влияют на текстовый вердикт (описание и признаки объекта), а не от всей
строки — смена цены сама по себе не повод гонять ИИ заново.

ПОЧЕМУ НЕ ХРАНИМ ИСТОРИЮ ВЕРДИКТОВ.
Задача этого сервиса — сказать "сейчас объявление выглядит так-то", а
не вести журнал мнений модели во времени. Если понадобится история —
это отдельная таблица по аналогии с price_history в коллекторе, но
пока для неё нет потребителя.
"""
import csv
import hashlib
import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import entity_store

log = logging.getLogger("cleaner_db")

DB_PATH = os.environ.get("CLEANER_DB") or os.path.join(
    os.environ.get("DATA_DIR", "."), "sales_cleaner_astana.db"
)
SCHEMA_VERSION = 3


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value):
    """ISO-строка из БД -> unix-время (float). Обратная операция к utcnow_iso().

    Метки в БД всегда пишутся с таймзоной (+00:00), но записи, созданные
    более ранними версиями, могли остаться без неё — тогда трактуем как UTC,
    иначе .timestamp() молча взял бы локальную зону сервера и возраст цикла
    в /health сдвинулся бы на часовой пояс.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _create_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS verdicts (
            id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            usable INTEGER,            -- 1 | 0 | NULL(=неизвестно, см. _failed)
            reason_code TEXT,
            confidence TEXT,
            reason TEXT,
            source TEXT,              -- 'rule' | 'llm' | 'llm_failed'
            model TEXT,                -- имя модели, если source='llm'
            policy_version TEXT,       -- версия prompt/rule policy для аудита
            batch_id TEXT,              -- какое batch-задание дало вердикт (если llm)
            processed_at TEXT NOT NULL,
            baseline_version TEXT,     -- версия baseline коллектора на момент обработки
            -- Снимок текста ТОЛЬКО для отбракованных (usable=0/NULL).
            -- Зачем: вердикт сам по себе неперепроверяем. Объявление
            -- живёт на krisha недолго, из baseline коллектора оно уйдёт,
            -- как только пропадёт с сайта — и тогда на вопрос "за что
            -- выкинули эту квартиру" ответить будет нечем. Для usable=1
            -- не храним: это 99% строк, а перепроверять нужно именно
            -- отбраковку.
            snapshot_title TEXT,
            snapshot_desc TEXT,
            snapshot_url TEXT,
            -- Сколько раз ИИ уже спотыкался на этом объявлении.
            -- Нужен потолок: llm_failed намеренно НЕ фиксирует хэш,
            -- чтобы объявление вернулось в очередь, но без счётчика
            -- безнадёжная строка переотправлялась бы вечно, каждый
            -- цикл, за деньги.
            llm_attempts INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY,     -- id от провайдера (OpenAI)
            status TEXT NOT NULL,          -- pending | completed | failed | expired
            listing_ids TEXT NOT NULL,     -- JSON-массив id, вошедших в задание
            submitted_at TEXT NOT NULL,
            completed_at TEXT,
            output_file_id TEXT,
            error TEXT,
            group_size INTEGER,
            model TEXT,
            policy_version TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clean_baseline (
            seq INTEGER PRIMARY KEY,   -- 1..N, порядок выдачи
            id TEXT NOT NULL,
            row_json TEXT NOT NULL     -- строка коллектора как есть, JSON
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS relabel (
            id TEXT PRIMARY KEY    -- объявления, ждущие повторной разметки ИИ
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_verdicts_usable ON verdicts(usable)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_verdicts_code ON verdicts(reason_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status)")
    # Постраничная выдача сортирует по (processed_at, id) — индекс ровно по
    # этой паре, иначе на каждой странице был бы полный скан с сортировкой.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdicts_page ON verdicts(processed_at, id)")

    # Догоняем схему на уже существующей базе: CREATE TABLE IF NOT EXISTS
    # ничего не меняет, если таблица создана более старой версией кода,
    # и без этого добавленные колонки существовали бы только на чистых
    # установках.
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema {current_version} is newer than supported "
            f"schema {SCHEMA_VERSION}"
        )

    # Sequential, idempotent migrations for databases created by older builds.
    # CREATE TABLE IF NOT EXISTS above initializes a new database; these steps
    # deliberately also inspect columns so a legacy DB with user_version=0 can
    # be upgraded safely in one start.
    have = {r[1] for r in conn.execute("PRAGMA table_info(verdicts)")}
    for col in ("snapshot_title", "snapshot_desc", "snapshot_url"):
        if col not in have:
            conn.execute(f"ALTER TABLE verdicts ADD COLUMN {col} TEXT")
    if "llm_attempts" not in have:
        conn.execute("ALTER TABLE verdicts ADD COLUMN llm_attempts INTEGER NOT NULL DEFAULT 0")
    if "policy_version" not in have:
        conn.execute("ALTER TABLE verdicts ADD COLUMN policy_version TEXT")

    batch_columns = {r[1] for r in conn.execute("PRAGMA table_info(batches)")}
    for name, declaration in (
        ("group_size", "INTEGER"),
        ("model", "TEXT"),
        ("policy_version", "TEXT"),
    ):
        if name not in batch_columns:
            conn.execute(f"ALTER TABLE batches ADD COLUMN {name} {declaration}")

    entity_store.create_schema(conn)
    if current_version < SCHEMA_VERSION:
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            sample = [tuple(row) for row in violations[:5]]
            raise RuntimeError(f"foreign key violations before migration: {sample}")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


@contextmanager
def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # SQLite does not enforce declared foreign keys unless every connection
    # explicitly enables them.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    _create_schema(conn)
    # Persist schema migrations even for read-only API requests whose caller
    # never performs its own commit.
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


# ============================== verdicts ==============================

def get_known_hashes(conn, ids):
    """{id: content_hash} для уже обработанных id из списка. Используется
    для диффа: если у входящей строки хэш совпал — пропускаем, не тратим
    ни бесплатный слой, ни тем более ИИ."""
    if not ids:
        return {}
    result = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT id, content_hash FROM verdicts WHERE id IN ({placeholders})", chunk
        ):
            result[row["id"]] = row["content_hash"]
    return result


def llm_attempts_for(conn, listing_id):
    """Сколько раз ИИ уже спотыкался на этом объявлении (0, если записи нет)."""
    row = conn.execute(
        "SELECT llm_attempts FROM verdicts WHERE id = ?", (listing_id,)).fetchone()
    return row["llm_attempts"] if row else 0


def upsert_verdict(conn, listing_id, content_hash, verdict, baseline_version,
                    model=None, batch_id=None, row=None, attempts=0,
                    policy_version=None):
    """row — исходная строка объявления. Если передана И вердикт
    отбраковочный, сохраняем короткий снимок текста для последующей
    ручной проверки (см. комментарий к колонкам snapshot_*).

    attempts — новое значение счётчика llm_attempts. Считает его вызывающий
    (см. pipeline.ingest_completed_batches): там же принимается решение,
    сдаваться ли после MAX_LLM_ATTEMPTS, и оба решения должны опираться на
    одно и то же число. Успешный вердикт передаёт 0 — счётчик обнуляется.
    """
    snap_title = snap_desc = snap_url = None
    if row is not None and verdict.get("usable") is not True:
        snap_title = (row.get("title") or "")[:300]
        snap_desc = (row.get("full_description") or "")[:1000]
        snap_url = row.get("url")

    conn.execute(
        """
        INSERT INTO verdicts
            (id, content_hash, usable, reason_code, confidence, reason, source,
             model, policy_version, batch_id, processed_at, baseline_version,
             snapshot_title, snapshot_desc, snapshot_url, llm_attempts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            content_hash=excluded.content_hash,
            usable=excluded.usable,
            reason_code=excluded.reason_code,
            confidence=excluded.confidence,
            reason=excluded.reason,
            source=excluded.source,
            model=excluded.model,
            policy_version=excluded.policy_version,
            batch_id=excluded.batch_id,
            processed_at=excluded.processed_at,
            baseline_version=excluded.baseline_version,
            snapshot_title=excluded.snapshot_title,
            snapshot_desc=excluded.snapshot_desc,
            snapshot_url=excluded.snapshot_url,
            llm_attempts=excluded.llm_attempts
        """,
        (
            listing_id, content_hash,
            int(verdict["usable"]) if verdict.get("usable") is not None else None,
            verdict.get("reason_code"), verdict.get("confidence"), verdict.get("reason"),
            verdict.get("source"), model, policy_version, batch_id, utcnow_iso(), baseline_version,
            snap_title, snap_desc, snap_url, attempts,
        ),
    )
    if content_hash:
        # Новый вердикт по текущему тексту — повторная разметка выполнена.
        conn.execute("DELETE FROM relabel WHERE id = ?", (listing_id,))


def stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
    by_code = dict(conn.execute(
        "SELECT reason_code, COUNT(*) FROM verdicts GROUP BY reason_code"))
    usable_counts = dict(conn.execute(
        "SELECT usable, COUNT(*) FROM verdicts GROUP BY usable"))
    by_source = dict(conn.execute(
        "SELECT source, COUNT(*) FROM verdicts GROUP BY source"))
    return {
        "total": total,
        "usable_true": usable_counts.get(1, 0),
        "usable_false": usable_counts.get(0, 0),
        "unknown": usable_counts.get(None, 0),
        "by_reason_code": by_code,
        "by_source": by_source,
    }


def verdict_state_map(conn):
    """{id: (usable, content_hash)} по всем вердиктам.

    Вход для решения «публиковать ли объявление» (pipeline.publish_clean_baseline).
    content_hash нужен, чтобы отличить вердикт по ТЕКУЩЕМУ тексту от
    вердикта по старому: изменённое объявление ждёт новой разметки так же,
    как новое.
    """
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, usable, content_hash FROM verdicts")}


def verdict_source_map(conn):
    """{id: (source, usable, reason_code)} — кто и что решил по каждому объявлению."""
    return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT id, source, usable, reason_code FROM verdicts")}


def start_relabel(conn):
    """Ставит ВСЕ ИИ-вердикты на повторную разметку (после смены промпта).
    Хэш сбрасывается, чтобы дифф вернул их в очередь; id запоминаются в
    таблице relabel, пока не получат новый вердикт (см. upsert_verdict).
    Возвращает число поставленных объявлений."""
    conn.execute(
        "INSERT OR IGNORE INTO relabel (id) SELECT id FROM verdicts "
        "WHERE source IN ('llm', 'llm_failed')")
    conn.execute("UPDATE verdicts SET content_hash = '' "
                 "WHERE id IN (SELECT id FROM relabel)")
    return conn.execute("SELECT COUNT(*) FROM relabel").fetchone()[0]


def prune_relabel(conn, current_ids):
    """Убирает из ожидания объявления, пропавшие с сайта: новый вердикт им
    уже не придёт, а без этого перепрогон никогда не считался бы завершённым."""
    current = set(current_ids)
    gone = [(i,) for (i,) in conn.execute("SELECT id FROM relabel") if i not in current]
    conn.executemany("DELETE FROM relabel WHERE id = ?", gone)


def relabel_remaining(conn):
    return conn.execute("SELECT COUNT(*) FROM relabel").fetchone()[0]


def invalidate_hash(conn, listing_id):
    """Сбрасывает content_hash: следующий дифф увидит несовпадение и вернёт
    объявление в очередь на разметку. Сам вердикт остаётся до нового."""
    conn.execute("UPDATE verdicts SET content_hash = '' WHERE id = ?", (listing_id,))


# ========================== чистый baseline ==========================

CLEAN_VERSION_KEY = "clean_baseline_version"
CLEAN_BUILT_AT_KEY = "clean_baseline_built_at"
CLEAN_CSV_PATH_KEY = "clean_baseline_csv_path"


def _write_clean_baseline_csv(rows, version, built_at):
    """Write one immutable CSV artifact without constructing it in RAM."""
    db_path = os.path.abspath(DB_PATH)
    directory = os.path.dirname(db_path) or "."
    os.makedirs(directory, exist_ok=True)
    token = hashlib.sha256(
        f"{version}\0{built_at}".encode("utf-8")
    ).hexdigest()[:16]
    final_path = f"{db_path}.clean.{token}.csv"
    temp_path = f"{final_path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    writer = None
    try:
        with open(temp_path, "w", encoding="utf-8", newline="") as handle:
            for row in rows:
                if writer is None:
                    writer = csv.DictWriter(
                        handle, fieldnames=list(row), extrasaction="ignore"
                    )
                    writer.writeheader()
                writer.writerow({
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict)) else value
                    for key, value in row.items()
                })
        os.replace(temp_path, final_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return final_path


def replace_clean_baseline(conn, rows, version):
    """Целиком заменяет опубликованный снимок. Коммит — на вызывающем.

    Замена и метаданные в одной транзакции: читатель видит либо прежний
    снимок целиком, либо новый целиком, но не смесь.

    seq — сквозной номер 1..N. Пагинация по диапазону seq детерминирована
    и не сканирует пропущенные строки, в отличие от OFFSET.
    """
    # Snapshot cursor must change even if two rebuilds finish in one second.
    built_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    csv_path = _write_clean_baseline_csv(rows, version, built_at)
    conn.execute("DELETE FROM clean_baseline")
    conn.executemany(
        "INSERT INTO clean_baseline (seq, id, row_json) VALUES (?, ?, ?)",
        ((i, r["id"], json.dumps(r, ensure_ascii=False))
         for i, r in enumerate(rows, start=1)),
    )
    set_meta(conn, CLEAN_VERSION_KEY, str(version))
    set_meta(conn, CLEAN_BUILT_AT_KEY, built_at)
    set_meta(conn, CLEAN_CSV_PATH_KEY, csv_path)
    return csv_path


def ensure_clean_baseline_csv(conn):
    """Return the current artifact, rebuilding legacy snapshots on disk once."""
    info = clean_baseline_info(conn)
    if info is None:
        return None
    _total, version, built_at = info
    csv_path, _ = get_meta(conn, CLEAN_CSV_PATH_KEY)
    if csv_path and os.path.isfile(csv_path):
        return csv_path
    csv_path = _write_clean_baseline_csv(
        iter_clean_baseline(conn), version, built_at,
    )
    set_meta(conn, CLEAN_CSV_PATH_KEY, csv_path)
    conn.commit()
    cleanup_obsolete_clean_csv(csv_path)
    return csv_path


def cleanup_obsolete_clean_csv(current_path):
    """Keep current and one previous artifact for in-progress downloads."""
    current = os.path.abspath(current_path)
    db_path = os.path.abspath(DB_PATH)
    directory = os.path.dirname(db_path) or "."
    prefix = os.path.basename(db_path) + ".clean."
    artifacts = []
    for name in os.listdir(directory):
        if name.startswith(prefix) and name.endswith(".csv"):
            candidate = os.path.abspath(os.path.join(directory, name))
            try:
                modified = os.path.getmtime(candidate)
            except FileNotFoundError:
                continue
            artifacts.append((modified, candidate))
    previous = next(
        (path for _modified, path in sorted(artifacts, reverse=True)
         if path != current),
        None,
    )
    for _modified, candidate in artifacts:
        if candidate not in {current, previous}:
            try:
                os.remove(candidate)
            except FileNotFoundError:
                pass
            except OSError:
                # A client may still be downloading the previous artifact
                # (notably on Windows where an open file cannot be deleted).
                # The next publication will retry cleanup; the committed
                # snapshot itself must remain successful.
                log.warning("could not remove obsolete clean CSV %s", candidate)


def clean_baseline_info(conn):
    """(count, collector_version, built_at) или None, если снимок ещё не собирался."""
    version, _ = get_meta(conn, CLEAN_VERSION_KEY)
    built_at, _ = get_meta(conn, CLEAN_BUILT_AT_KEY)
    if built_at is None:
        return None
    count = conn.execute("SELECT COUNT(*) FROM clean_baseline").fetchone()[0]
    return count, version, built_at


def iter_clean_baseline(conn):
    """Все строки снимка по порядку seq. Один SELECT — одно согласованное чтение."""
    for (row_json,) in conn.execute("SELECT row_json FROM clean_baseline ORDER BY seq"):
        yield json.loads(row_json)


def clean_baseline_page(conn, limit, offset):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT row_json FROM clean_baseline WHERE seq > ? AND seq <= ? ORDER BY seq",
        (offset, offset + limit),
    )]


# ================================ meta ================================

def set_meta(conn, key, value):
    """Сохраняет состояние между перезапусками процесса.

    Нужно для /health: раньше результат последнего цикла жил только в
    памяти, и после каждого редеплоя сервис на несколько суток отвечал
    "starting" — то есть healthcheck переставал отличать «только что
    перезапустились» от «цикл сломан и не отрабатывает».
    """
    conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, utcnow_iso()),
    )


def get_meta(conn, key):
    row = conn.execute("SELECT value, updated_at FROM meta WHERE key = ?", (key,)).fetchone()
    return (row["value"], row["updated_at"]) if row else (None, None)


# ============================== batches ==============================

def in_flight_ids(conn):
    """id объявлений, уже отправленных в НЕЗАВЕРШЁННЫХ batch-заданиях.

    Нужна, чтобы не отправить дубль: без этой проверки, если цикл
    запустится второй раз раньше, чем ответит первый batch (окно до
    24ч), diff_against_known увидел бы эти id как "нет вердикта" —
    потому что вердикт появляется только ПОСЛЕ ответа — и отправил бы
    их заново вторым batch-заданием. Реальный сценарий, не гипотетика:
    суточный цикл почти гарантированно застаёт вчерашний batch ещё не
    завершённым.
    """
    import json as _json
    ids = set()
    for b in pending_batches(conn):
        # Битая запись не должна ронять весь цикл: in_flight_ids
        # вызывается в самом начале run_cycle, и исключение здесь
        # означало бы, что сервис не может сделать вообще ничего, пока
        # кто-то руками не почистит таблицу. Пропускаем битую запись,
        # громко жалуемся — хуже всего было бы промолчать: id из этой
        # записи не попадут в in_flight, и объявления могут уйти в
        # повторную отправку (лишние деньги, но не потеря данных).
        try:
            parsed = _json.loads(b["listing_ids"])
        except (ValueError, TypeError):
            log.error(
                "batch %s: listing_ids не читается как JSON — запись пропущена, "
                "её объявления могут быть отправлены повторно", b["batch_id"],
            )
            continue
        if isinstance(parsed, dict):
            ids.update(parsed.keys())
        elif isinstance(parsed, list):
            # Совместимость со старым форматом (до перехода на {id: hash}).
            ids.update(parsed)
    return ids


def create_batch(conn, batch_id, listing_hashes, *, group_size=None,
                 model=None, policy_version=None):
    """listing_hashes — {listing_id: content_hash} НА МОМЕНТ ОТПРАВКИ.

    Хранить именно хэш, а не голый список id, критично. Раньше здесь
    лежал список, и ingest_completed_batches при записи вердикта брал
    хэш через get_known_hashes() — то есть из ещё НЕ СУЩЕСТВУЮЩЕЙ записи,
    получал "" и сохранял пустую строку. Следующий дифф-цикл сравнивал
    реальный хэш строки с "" , видел несовпадение и отправлял то же
    объявление в ИИ заново. Каждые сутки. Вечно.

    Теперь хэш фиксируется в момент отправки и при записи вердикта
    используется именно он: объявление считается обработанным ровно в
    том виде, в каком его видела модель. Если строка успела измениться
    за время ожидания ответа — следующий цикл честно это заметит и
    переотправит, но уже по делу, а не из-за пустого значения.
    """
    import json as _json
    conn.execute(
        "INSERT INTO batches "
        "(batch_id,status,listing_ids,submitted_at,group_size,model,policy_version) "
        "VALUES (?, 'pending', ?, ?, ?, ?, ?)",
        (batch_id, _json.dumps(listing_hashes), utcnow_iso(), group_size,
         model, policy_version),
    )


def pending_batches(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM batches WHERE status = 'pending'")]


def mark_batch(conn, batch_id, status, output_file_id=None, error=None):
    conn.execute(
        "UPDATE batches SET status=?, output_file_id=?, error=?, completed_at=? "
        "WHERE batch_id=?",
        (status, output_file_id, error,
         utcnow_iso() if status != "pending" else None, batch_id),
    )
