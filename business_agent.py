#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
business_agent.py
=================
Telegram-автоответчик через «Автоматизацию чатов» (Telegram Business Bot API).

Любой пользователь Telegram (БЕЗ Premium и БЕЗ бизнес-аккаунта) подключает бота
в Настройки → Автоматизация чатов, после чего бот отвечает на входящие сообщения
от его имени. Мозги — Groq API (бесплатно, OpenAI-совместимый формат).

Подготовка
----------
1. @BotFather → создать бота → Bot Settings → Business Mode → Enable
2. Ключ Groq: https://console.groq.com (бесплатно, без карты)
3. Юзер подключает бота: Настройки → Автоматизация чатов → @имя_бота

Запуск
------
    pip install -r requirements.txt
    # токен и ключ — в .env рядом со скриптом или в переменных окружения
    python3 business_agent.py
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# ЗАВИСИМОСТИ
# ─────────────────────────────────────────────────────────────────────────────

_MISSING: list[str] = []

try:
    import aiosqlite
except ImportError:
    _MISSING.append("aiosqlite")

try:
    from openai import AsyncOpenAI
except ImportError:
    _MISSING.append("openai")

try:
    from aiogram import Bot, Dispatcher, F
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ChatAction
    from aiogram.filters import Command, CommandObject, CommandStart
    from aiogram.types import (
        BusinessConnection,
        BusinessMessagesDeleted,
        Message,
        PhotoSize,
    )
except ImportError:
    _MISSING.append("'aiogram>=3.10'")

if _MISSING:
    sys.exit(
        "Не хватает зависимостей. Установи:\n"
        "    pip install " + " ".join(_MISSING)
    )

try:  # openai >= 1.0
    from openai import RateLimitError
except Exception:  # pragma: no cover
    RateLimitError = ()  # type: ignore[assignment,misc]


# ─────────────────────────────────────────────────────────────────────────────
# .env (без внешних зависимостей)
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Простейший загрузчик .env: KEY=VALUE, # — комментарий."""
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


_load_env_file(BASE_DIR / ".env")


# ─────────────────────────────────────────────────────────────────────────────
# КОНФИГ
# ─────────────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")            # console.groq.com
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")  # если снята с обслуживания — подберём живую

# Groq регулярно выводит модели из обслуживания, поэтому на старте бот
# спрашивает у API список живых моделей и берёт первую доступную отсюда.
# Если API недоступен — просто идём по списку сверху вниз.
GROQ_MODEL_FALLBACKS = [
    GROQ_MODEL,
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
]

# Vision-модели для разбора скриншотов (первая — из ТЗ, дальше — актуальные).
GROQ_VISION_MODELS = [
    os.getenv("GROQ_VISION_MODEL", "llama-3.2-90b-vision-preview"),
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
]

# Модели не для чата — в автоподбор не берём.
_NON_CHAT_MARKERS = ("whisper", "orpheus", "guard", "tts", "embed")

ADMIN_ID = int(os.getenv("ADMIN_ID", "7729136758"))
MAX_HISTORY = 20
AUTO_REPLY_DELAY = 1.5          # секунд перед ответом
OWNER_TAKEOVER_MIN = 30         # минут молчания после ручного ответа владельца
MAX_MESSAGE_LENGTH = 4000       # лимит Telegram — 4096
CHAT_RATE_LIMIT = 3.0           # не чаще 1 ответа в 3 секунды на chat_id
ALBUM_WAIT = 1.4                # секунд ждём остальные фото из альбома
DB_PATH = os.getenv("AGENT_DB", str(BASE_DIR / "agent.db"))

# Groq free tier: ~30 RPM, 14400 RPD, 6000 TPM и лимит output-токенов в минуту
# (на бесплатном тарифе бывает 1000). Реальный потолок бот узнаёт из ошибки 429
# и сам ужимает max_tokens. Для автоответчика одного аккаунта хватает с запасом.
GROQ_MAX_RETRIES = 4
REPLY_MAX_TOKENS = 900          # ответ собеседнику
VISION_MAX_TOKENS = 1200        # разбор скриншота
ANALYSIS_MAX_TOKENS = 1500      # профиль стиля
MIN_OUTPUT_TOKENS = 256         # ниже не опускаемся при подгонке под лимит
VISION_MAX_IMAGE_BYTES = 3_500_000
ANALYSIS_MAX_CHARS = 12_000     # чтобы не упереться в 6000 TPM

DEFAULT_SYSTEM_PROMPT = """
Ты — личный ассистент владельца этого Telegram аккаунта.
Отвечай кратко, по делу, дружелюбно, на языке собеседника.
Если не знаешь ответ — скажи что владелец ответит позже.
Не говори что ты бот или ИИ. Не используй эмодзи чрезмерно.
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# ПРОМПТЫ ОБУЧЕНИЯ ПО СКРИНШОТАМ
# ─────────────────────────────────────────────────────────────────────────────

VISION_PROMPT = """
Это скриншот переписки в Telegram.
Извлеки ВСЕ сообщения — и входящие (левая сторона) и исходящие
(правая сторона).

Верни JSON массив объектов:
[
  {"side": "owner", "text": "привет бро"},
  {"side": "other", "text": "здарова, чё как"},
  {"side": "owner", "text": "да норм, слушай вопрос есть"},
  {"side": "other", "text": "давай"},
  {"side": "owner", "text": "го завтра в 8?"}
]

side = "owner" для исходящих (правая сторона, зелёные/синие пузыри)
side = "other" для входящих (левая сторона, белые/серые пузыри)

Сохраняй порядок сообщений сверху вниз.
Мат, сленг, ошибки — сохраняй КАК ЕСТЬ, не цензурь, не исправляй.
Только текст, без времени и статусов доставки.
Верни ТОЛЬКО JSON массив, без пояснений.
""".strip()

ANALYSIS_PROMPT = """
Вот полные диалоги одного человека (owner) с разными собеседниками
(other) из Telegram:

{all_dialogues}

Проанализируй СТИЛЬ ОБЩЕНИЯ owner и верни JSON:
{
  "tone": "описание тона в 5-10 слов",
  "language": "какой язык, есть ли миксы",
  "avg_msg_length": "короткие/средние/длинные",
  "common_phrases": ["до 20 самых частых фраз/оборотов"],
  "swear_style": "как использует мат, в каком контексте",
  "emoji_usage": "какие эмодзи, как часто",
  "greeting_style": "как здоровается",
  "refusal_style": "как отказывает",
  "agreement_style": "как соглашается",
  "question_style": "как задаёт вопросы",
  "reaction_to_questions": "как отвечает на вопросы собеседника",
  "reaction_to_offers": "как реагирует на предложения",
  "reaction_to_complaints": "как реагирует на жалобы/негатив",
  "reaction_to_jokes": "как реагирует на шутки",
  "conversation_patterns": [
    "когда спрашивают X — отвечает Y",
    "на приветствие отвечает Z"
  ],
  "sample_exchanges": [
    {"input": "чё как?", "response": "да норм, ты как"},
    {"input": "го завтра?", "response": "давай, во сколько"}
  ],
  "sample_phrases": ["10-15 самых характерных фраз дословно"]
}

ВАЖНО:
- Анализируй ТОЛЬКО стиль owner, не other
- Но УЧИТЫВАЙ контекст — на что owner так отвечает
- sample_exchanges — реальные пары "вопрос → ответ" из диалогов
- conversation_patterns — обобщённые паттерны реакций
- Не цензурь мат. Сохраняй всё как есть.
- Верни ТОЛЬКО валидный JSON-объект.
""".strip()

STYLE_PROMPT_TEMPLATE = """
Ты — владелец этого Telegram аккаунта. Отвечай ТОЧНО в его стиле.

Тон: {tone}
Язык: {language}
Длина сообщений: {avg_msg_length}
Мат: {swear_style}
Эмодзи: {emoji_usage}

Как здороваешься: {greeting_style}
Как соглашаешься: {agreement_style}
Как отказываешь: {refusal_style}
Как спрашиваешь: {question_style}

Как реагируешь на вопросы: {reaction_to_questions}
Как реагируешь на предложения: {reaction_to_offers}
Как реагируешь на негатив: {reaction_to_complaints}

Паттерны твоих ответов:
{conversation_patterns}

Примеры твоих реальных диалогов (повторяй этот стиль):
{sample_exchanges}

Твои типичные фразы:
{common_phrases}

ПРАВИЛА:
- Пиши ТОЧНО как этот человек — с его матами, сленгом, длиной
- НЕ говори что ты бот/ИИ
- НЕ будь вежливее чем он
- Если он матерится — ты тоже
- Если он пишет коротко — ты тоже коротко
- Используй его паттерны ответов из примеров
- Когда тебе пишут похожее на примеры — отвечай похоже
""".strip()

STYLE_KEYS = (
    "tone",
    "language",
    "avg_msg_length",
    "swear_style",
    "emoji_usage",
    "greeting_style",
    "agreement_style",
    "refusal_style",
    "question_style",
    "reaction_to_questions",
    "reaction_to_offers",
    "reaction_to_complaints",
    "reaction_to_jokes",
    "conversation_patterns",
    "sample_exchanges",
    "common_phrases",
    "sample_phrases",
)


# ─────────────────────────────────────────────────────────────────────────────
# ЛОГИ
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("business-agent")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────────────────────────
# БД (aiosqlite) — agent.db
# ─────────────────────────────────────────────────────────────────────────────

_db: Optional["aiosqlite.Connection"] = None
_db_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    connection_id  TEXT    PRIMARY KEY,
    owner_id       INTEGER NOT NULL,
    owner_chat_id  INTEGER,
    can_reply      INTEGER NOT NULL DEFAULT 0,
    is_disabled    INTEGER NOT NULL DEFAULT 0,
    connected_at   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conn_owner ON connections(owner_id);

CREATE TABLE IF NOT EXISTS chat_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT    NOT NULL,
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER,
    role          TEXT    NOT NULL,
    content       TEXT    NOT NULL,
    timestamp     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_chat ON chat_history(connection_id, chat_id, id);
CREATE INDEX IF NOT EXISTS idx_hist_msg  ON chat_history(connection_id, chat_id, message_id);

CREATE TABLE IF NOT EXISTS takeover (
    connection_id TEXT    NOT NULL,
    chat_id       INTEGER NOT NULL,
    paused_until  REAL    NOT NULL,
    PRIMARY KEY (connection_id, chat_id)
);

CREATE TABLE IF NOT EXISTS custom_prompts (
    owner_id      INTEGER PRIMARY KEY,
    system_prompt TEXT    NOT NULL,
    updated_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS owner_settings (
    owner_id   INTEGER PRIMARY KEY,
    paused     INTEGER NOT NULL DEFAULT 0,
    updated_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS style_data (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     INTEGER NOT NULL,
    raw_dialogue TEXT    NOT NULL,
    analysis     TEXT,
    created_at   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_style_owner ON style_data(owner_id);

CREATE TABLE IF NOT EXISTS style_profile (
    owner_id     INTEGER PRIMARY KEY,
    profile_json TEXT    NOT NULL,
    updated_at   REAL    NOT NULL
);
"""


async def db_init() -> None:
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA synchronous=NORMAL")
    await _db.executescript(SCHEMA)
    # мягкая миграция для старых баз
    for table, column, ddl in (
        ("connections", "owner_chat_id", "INTEGER"),
        ("chat_history", "message_id", "INTEGER"),
        ("style_data", "analysis", "TEXT"),
    ):
        cur = await _db.execute(f"PRAGMA table_info({table})")
        cols = {row["name"] for row in await cur.fetchall()}
        await cur.close()
        if column not in cols:
            await _db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    await _db.commit()
    log.info("БД готова: %s", DB_PATH)


async def db_close() -> None:
    if _db is not None:
        with contextlib.suppress(Exception):
            await _db.close()


async def _exec(sql: str, params: tuple = ()) -> None:
    assert _db is not None
    async with _db_lock:
        await _db.execute(sql, params)
        await _db.commit()


async def _fetchone(sql: str, params: tuple = ()) -> Optional["aiosqlite.Row"]:
    assert _db is not None
    async with _db_lock:
        cur = await _db.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
    return row


async def _fetchall(sql: str, params: tuple = ()) -> list["aiosqlite.Row"]:
    assert _db is not None
    async with _db_lock:
        cur = await _db.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
    return list(rows)


# ── connections ──────────────────────────────────────────────────────────────

async def save_connection(
    connection_id: str,
    owner_id: int,
    owner_chat_id: int,
    can_reply: bool,
    is_disabled: bool,
) -> None:
    await _exec(
        """
        INSERT INTO connections
            (connection_id, owner_id, owner_chat_id, can_reply, is_disabled, connected_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(connection_id) DO UPDATE SET
            owner_id      = excluded.owner_id,
            owner_chat_id = excluded.owner_chat_id,
            can_reply     = excluded.can_reply,
            is_disabled   = excluded.is_disabled
        """,
        (connection_id, owner_id, owner_chat_id, int(can_reply), int(is_disabled), time.time()),
    )


async def get_connection(connection_id: str) -> Optional[dict]:
    row = await _fetchone(
        "SELECT * FROM connections WHERE connection_id = ?", (connection_id,)
    )
    return dict(row) if row else None


async def get_owner_connections(owner_id: int) -> list[dict]:
    rows = await _fetchall(
        "SELECT * FROM connections WHERE owner_id = ? ORDER BY connected_at DESC",
        (owner_id,),
    )
    return [dict(r) for r in rows]


async def is_connected_owner(owner_id: int) -> bool:
    row = await _fetchone(
        "SELECT 1 FROM connections WHERE owner_id = ? LIMIT 1", (owner_id,)
    )
    return row is not None


# ── chat_history ─────────────────────────────────────────────────────────────

async def add_history(
    connection_id: str, chat_id: int, message_id: Optional[int], role: str, content: str
) -> None:
    await _exec(
        """
        INSERT INTO chat_history
            (connection_id, chat_id, message_id, role, content, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (connection_id, chat_id, message_id, role, content, time.time()),
    )


async def get_history(connection_id: str, chat_id: int, limit: int = MAX_HISTORY) -> list[dict]:
    rows = await _fetchall(
        """
        SELECT role, content FROM (
            SELECT id, role, content FROM chat_history
            WHERE connection_id = ? AND chat_id = ?
            ORDER BY id DESC LIMIT ?
        ) ORDER BY id ASC
        """,
        (connection_id, chat_id, limit),
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def last_user_message_id(connection_id: str, chat_id: int) -> Optional[int]:
    row = await _fetchone(
        """
        SELECT message_id FROM chat_history
        WHERE connection_id = ? AND chat_id = ? AND role = 'user'
        ORDER BY id DESC LIMIT 1
        """,
        (connection_id, chat_id),
    )
    return row["message_id"] if row else None


async def history_has_message(connection_id: str, chat_id: int, message_id: int) -> bool:
    row = await _fetchone(
        """
        SELECT 1 FROM chat_history
        WHERE connection_id = ? AND chat_id = ? AND message_id = ? LIMIT 1
        """,
        (connection_id, chat_id, message_id),
    )
    return row is not None


async def update_history_message(
    connection_id: str, chat_id: int, message_id: int, content: str
) -> bool:
    assert _db is not None
    async with _db_lock:
        cur = await _db.execute(
            """
            UPDATE chat_history SET content = ?
            WHERE connection_id = ? AND chat_id = ? AND message_id = ?
            """,
            (content, connection_id, chat_id, message_id),
        )
        changed = cur.rowcount
        await cur.close()
        await _db.commit()
    return bool(changed)


async def delete_history_messages(
    connection_id: str, chat_id: int, message_ids: list[int]
) -> int:
    if not message_ids:
        return 0
    placeholders = ",".join("?" for _ in message_ids)
    assert _db is not None
    async with _db_lock:
        cur = await _db.execute(
            f"""
            DELETE FROM chat_history
            WHERE connection_id = ? AND chat_id = ? AND message_id IN ({placeholders})
            """,
            (connection_id, chat_id, *message_ids),
        )
        removed = cur.rowcount
        await cur.close()
        await _db.commit()
    return removed or 0


async def clear_owner_history(owner_id: int) -> int:
    assert _db is not None
    async with _db_lock:
        cur = await _db.execute(
            """
            DELETE FROM chat_history WHERE connection_id IN
                (SELECT connection_id FROM connections WHERE owner_id = ?)
            """,
            (owner_id,),
        )
        removed = cur.rowcount
        await cur.close()
        await _db.execute(
            """
            DELETE FROM takeover WHERE connection_id IN
                (SELECT connection_id FROM connections WHERE owner_id = ?)
            """,
            (owner_id,),
        )
        await _db.commit()
    return removed or 0


async def owner_history_stats(owner_id: int) -> tuple[int, int]:
    row = await _fetchone(
        """
        SELECT COUNT(*) AS msgs, COUNT(DISTINCT chat_id) AS chats
        FROM chat_history WHERE connection_id IN
            (SELECT connection_id FROM connections WHERE owner_id = ?)
        """,
        (owner_id,),
    )
    if not row:
        return 0, 0
    return int(row["msgs"] or 0), int(row["chats"] or 0)


# ── takeover ─────────────────────────────────────────────────────────────────

async def set_takeover(connection_id: str, chat_id: int, paused_until: float) -> None:
    await _exec(
        """
        INSERT INTO takeover (connection_id, chat_id, paused_until)
        VALUES (?, ?, ?)
        ON CONFLICT(connection_id, chat_id) DO UPDATE SET
            paused_until = excluded.paused_until
        """,
        (connection_id, chat_id, paused_until),
    )


async def is_taken_over(connection_id: str, chat_id: int) -> bool:
    row = await _fetchone(
        "SELECT paused_until FROM takeover WHERE connection_id = ? AND chat_id = ?",
        (connection_id, chat_id),
    )
    return bool(row) and float(row["paused_until"]) > time.time()


# ── настройки владельца / промпты ────────────────────────────────────────────

async def set_custom_prompt(owner_id: int, prompt: str) -> None:
    await _exec(
        """
        INSERT INTO custom_prompts (owner_id, system_prompt, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(owner_id) DO UPDATE SET
            system_prompt = excluded.system_prompt,
            updated_at    = excluded.updated_at
        """,
        (owner_id, prompt, time.time()),
    )


async def get_custom_prompt(owner_id: int) -> Optional[str]:
    row = await _fetchone(
        "SELECT system_prompt FROM custom_prompts WHERE owner_id = ?", (owner_id,)
    )
    return row["system_prompt"] if row else None


async def set_paused(owner_id: int, paused: bool) -> None:
    await _exec(
        """
        INSERT INTO owner_settings (owner_id, paused, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(owner_id) DO UPDATE SET
            paused     = excluded.paused,
            updated_at = excluded.updated_at
        """,
        (owner_id, int(paused), time.time()),
    )


async def is_paused(owner_id: int) -> bool:
    row = await _fetchone(
        "SELECT paused FROM owner_settings WHERE owner_id = ?", (owner_id,)
    )
    return bool(row and row["paused"])


# ── style_data / style_profile ───────────────────────────────────────────────

async def save_raw_dialogue(owner_id: int, dialogue: list[dict]) -> None:
    await _exec(
        """
        INSERT INTO style_data (owner_id, raw_dialogue, analysis, created_at)
        VALUES (?, ?, NULL, ?)
        """,
        (owner_id, json.dumps(dialogue, ensure_ascii=False), time.time()),
    )


async def get_all_dialogues(owner_id: int) -> list[list[dict]]:
    rows = await _fetchall(
        "SELECT raw_dialogue FROM style_data WHERE owner_id = ? ORDER BY id ASC",
        (owner_id,),
    )
    out: list[list[dict]] = []
    for row in rows:
        try:
            data = json.loads(row["raw_dialogue"])
        except (TypeError, ValueError):
            continue
        if isinstance(data, list) and data:
            out.append(data)
    return out


async def attach_last_analysis(owner_id: int, analysis: dict) -> None:
    await _exec(
        """
        UPDATE style_data SET analysis = ?
        WHERE id = (SELECT MAX(id) FROM style_data WHERE owner_id = ?)
        """,
        (json.dumps(analysis, ensure_ascii=False), owner_id),
    )


async def save_style_profile(owner_id: int, profile: dict) -> None:
    await _exec(
        """
        INSERT INTO style_profile (owner_id, profile_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(owner_id) DO UPDATE SET
            profile_json = excluded.profile_json,
            updated_at   = excluded.updated_at
        """,
        (owner_id, json.dumps(profile, ensure_ascii=False), time.time()),
    )


async def get_style_profile(owner_id: int) -> Optional[dict]:
    row = await _fetchone(
        "SELECT profile_json FROM style_profile WHERE owner_id = ?", (owner_id,)
    )
    if not row:
        return None
    try:
        data = json.loads(row["profile_json"])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def reset_style(owner_id: int) -> None:
    await _exec("DELETE FROM style_data WHERE owner_id = ?", (owner_id,))
    await _exec("DELETE FROM style_profile WHERE owner_id = ?", (owner_id,))


async def style_stats(owner_id: int) -> tuple[int, int, int, Optional[float]]:
    """(скриншотов, фраз владельца, фраз собеседников, когда обновлён профиль)"""
    dialogues = await get_all_dialogues(owner_id)
    own = sum(1 for d in dialogues for m in d if m.get("side") == "owner")
    other = sum(1 for d in dialogues for m in d if m.get("side") != "owner")
    row = await _fetchone(
        "SELECT updated_at FROM style_profile WHERE owner_id = ?", (owner_id,)
    )
    return len(dialogues), own, other, (row["updated_at"] if row else None)


# ─────────────────────────────────────────────────────────────────────────────
# GROQ API КЛИЕНТ (OpenAI-совместимый)
# ─────────────────────────────────────────────────────────────────────────────

groq_client = AsyncOpenAI(
    api_key=GROQ_API_KEY or "missing",
    base_url=GROQ_BASE_URL,
    max_retries=0,          # ретраи делаем сами, с учётом Retry-After
    timeout=90.0,
)

_active_text_model = GROQ_MODEL
_active_vision_model: Optional[str] = None
_last_ok_model: Optional[str] = None
# free tier ограничивает output tokens per minute — потолок узнаём из ошибки 429
_output_cap: Optional[int] = None
# модели, которые не понимают параметр reasoning_effort
_no_reasoning_param: set[str] = set()


def _parse_duration(value: str) -> Optional[float]:
    """'7.66s' / '2m59.56s' / '1.5' → секунды."""
    value = str(value).strip()
    if not value:
        return None
    with contextlib.suppress(ValueError):
        return float(value)
    total = 0.0
    found = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)", value):
        found = True
        amount = float(amount)
        total += amount * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    return total if found else None


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    for key in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = headers.get(key)
        if raw:
            seconds = _parse_duration(raw)
            if seconds is not None:
                return min(max(seconds, 0.5), 60.0)
    return None


def _is_model_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "model_not_found",
            "does not exist",
            "decommissioned",
            "model_terminated",
            "has been deprecated",
            "not supported",
        )
    )


def _reasoning_effort(model: str) -> Optional[str]:
    """
    Рассуждающие модели тратят output-лимит на «размышления» и не успевают
    дописать JSON. Просим думать поменьше — ответы быстрее и дешевле.
    """
    if model in _no_reasoning_param:
        return None
    name = model.lower()
    if "gpt-oss" in name:
        return "low"
    if "qwen" in name:
        return "none"
    return None


def _too_large_limit(exc: Exception) -> Optional[int]:
    """Groq: 'Request too large ... (OTPM): Limit 1000, Requested 2048' → 1000."""
    text = str(exc)
    if "too large" not in text.lower() and "reduce max_tokens" not in text.lower():
        return None
    match = re.search(r"[Ll]imit (\d+)", text)
    return int(match.group(1)) if match else 0


def _is_rate_limit(exc: Exception) -> bool:
    if RateLimitError and isinstance(exc, RateLimitError):
        return True
    return getattr(getattr(exc, "response", None), "status_code", None) == 429


async def groq_chat(
    messages: list[dict],
    *,
    models: list[str],
    max_tokens: int = 1024,
    temperature: float = 0.7,
    json_mode: bool = False,
) -> Optional[str]:
    """Запрос к Groq с ретраями по 429 и подменой снятой с обслуживания модели."""
    global _last_ok_model, _output_cap
    if _output_cap is not None:
        max_tokens = min(max_tokens, _output_cap)
    tried: list[str] = []
    for model in models:
        if not model or model in tried:
            continue
        tried.append(model)
        for attempt in range(1, GROQ_MAX_RETRIES + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                effort = _reasoning_effort(model)
                if effort:
                    kwargs["reasoning_effort"] = effort
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                response = await groq_client.chat.completions.create(**kwargs)
                choice = response.choices[0]
                content = (choice.message.content or "").strip()
                if getattr(choice, "finish_reason", None) == "length":
                    log.warning("[groq] ответ обрезан по max_tokens (%s)", max_tokens)
                _last_ok_model = model
                return content or None
            except Exception as exc:  # noqa: BLE001 — логируем и решаем что делать
                # проверять раньше _is_model_error: «... is not supported» про параметр,
                # а не про модель
                if "reasoning_effort" in str(exc) and _reasoning_effort(model):
                    _no_reasoning_param.add(model)
                    log.warning("[groq] %s не понимает reasoning_effort — повторяю без него", model)
                    continue
                if json_mode and "json_validate" in str(exc):
                    log.warning("[groq] строгий JSON-режим не сработал — повторяю без него")
                    json_mode = False
                    continue
                if _is_model_error(exc):
                    log.warning("[groq] модель %s недоступна, пробую следующую", model)
                    break
                limit = _too_large_limit(exc)
                if limit is not None and attempt < GROQ_MAX_RETRIES:
                    reduced = max(MIN_OUTPUT_TOKENS, (limit - 24) if limit else max_tokens // 2)
                    if reduced < max_tokens:
                        log.warning(
                            "[groq] лимит output-токенов аккаунта: max_tokens %s → %s",
                            max_tokens, reduced,
                        )
                        max_tokens = reduced
                        _output_cap = reduced
                        continue
                if _is_rate_limit(exc) and attempt < GROQ_MAX_RETRIES:
                    delay = _retry_after_seconds(exc) or (2.0 * attempt)
                    log.warning("[groq] 429 rate limit, жду %.1fs", delay)
                    await asyncio.sleep(delay)
                    continue
                if attempt < GROQ_MAX_RETRIES:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status is None or status >= 500:
                        await asyncio.sleep(1.5 * attempt)
                        continue
                log.error("[groq] ошибка: %s: %s", type(exc).__name__, exc)
                return None
    return None


async def resolve_models() -> None:
    """Спросить у Groq живые модели и выбрать текстовую + vision."""
    global _active_text_model, _active_vision_model
    try:
        listing = await groq_client.models.list()
    except Exception as exc:
        log.warning("[groq] список моделей недоступен (%s) — иду по списку из конфига", exc)
        return

    available: dict[str, Any] = {m.id: m for m in getattr(listing, "data", []) or []}
    if not available:
        return

    def modalities(model_id: str) -> list[str]:
        raw = getattr(available.get(model_id), "input_modalities", None) or []
        return [str(x).lower() for x in raw]

    def is_chat(model_id: str) -> bool:
        if any(marker in model_id.lower() for marker in _NON_CHAT_MARKERS):
            return False
        mods = modalities(model_id)
        return not mods or "text" in mods

    text_model = next((m for m in GROQ_MODEL_FALLBACKS if m in available and is_chat(m)), None)
    if text_model is None:
        text_model = next((m for m in available if is_chat(m)), None)
    if text_model:
        _active_text_model = text_model

    vision_model = next(
        (m for m in GROQ_VISION_MODELS if m in available and (not modalities(m) or "image" in modalities(m))),
        None,
    )
    if vision_model is None:
        vision_model = next((m for m in available if "image" in modalities(m)), None)
    _active_vision_model = vision_model

    log.info("[groq] модель для ответов: %s", _active_text_model)
    log.info(
        "[groq] модель для скриншотов: %s",
        _active_vision_model or "нет vision-модели — обучение по скринам не сработает",
    )


async def get_ai_response(history: list[dict], system_prompt: str) -> Optional[str]:
    """Ответ собеседнику по истории переписки."""
    global _active_text_model
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for msg in history[-MAX_HISTORY:]:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    if len(messages) == 1:
        return None

    models = [_active_text_model] + [m for m in GROQ_MODEL_FALLBACKS if m != _active_text_model]
    answer = await groq_chat(messages, models=models, max_tokens=REPLY_MAX_TOKENS, temperature=0.7)
    if answer and _last_ok_model and _last_ok_model != _active_text_model:
        log.info("[groq] переключился на модель %s", _last_ok_model)
        _active_text_model = _last_ok_model
    return answer


# ── vision: разбор скриншота ─────────────────────────────────────────────────

async def extract_dialogue_from_screenshot(
    base64_img: str, mime: str = "image/jpeg"
) -> list[dict]:
    global _active_vision_model
    models = GROQ_VISION_MODELS[:]
    if _active_vision_model:
        models = [_active_vision_model] + [m for m in models if m != _active_vision_model]

    payload = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{base64_img}"},
                },
            ],
        }
    ]

    for model in models:
        text = await groq_chat(
            payload, models=[model], max_tokens=VISION_MAX_TOKENS, temperature=0.1
        )
        if text:
            _active_vision_model = model
            dialogue = normalize_dialogue(parse_json_array(text))
            if dialogue:
                return dialogue
    return []


# ── анализ стиля ─────────────────────────────────────────────────────────────

def dialogues_to_text(dialogues: list[list[dict]], max_chars: int = ANALYSIS_MAX_CHARS) -> str:
    """Свежие диалоги первыми, обрезаем по лимиту символов (бережём TPM)."""
    blocks: list[str] = []
    used = 0
    for index, dialogue in enumerate(reversed(dialogues), start=1):
        lines = [
            f"{'owner' if m.get('side') == 'owner' else 'other'}: {m.get('text', '')}"
            for m in dialogue
            if m.get("text")
        ]
        if not lines:
            continue
        block = f"--- Диалог {len(dialogues) - index + 1} ---\n" + "\n".join(lines)
        if used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)
    blocks.reverse()
    return "\n\n".join(blocks)


async def analyze_style(dialogues: list[list[dict]]) -> Optional[dict]:
    text = dialogues_to_text(dialogues)
    if not text.strip():
        return None
    prompt = ANALYSIS_PROMPT.replace("{all_dialogues}", text)
    models = [_active_text_model] + [m for m in GROQ_MODEL_FALLBACKS if m != _active_text_model]
    raw = await groq_chat(
        [
            {
                "role": "system",
                "content": (
                    "Ты — аналитик стиля общения. Отвечаешь ТОЛЬКО валидным JSON. "
                    "Не цензуришь мат и сленг, сохраняешь всё дословно."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        models=models,
        max_tokens=ANALYSIS_MAX_TOKENS,
        temperature=0.3,
        json_mode=True,
    )
    if not raw:
        return None
    profile = parse_json_object(raw)
    return profile or None


# ─────────────────────────────────────────────────────────────────────────────
# ПАРСИНГ JSON ИЗ ОТВЕТА МОДЕЛИ
# ─────────────────────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    out = text.strip()
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", out)
        if out.endswith("```"):
            out = out[:-3]
    return out.strip()


def _repair_json(text: str, opener: str) -> Any:
    """Достроить JSON, который модель обрезала по лимиту токенов."""
    closer = "}" if opener == "{" else "]"
    start = text.find(opener)
    if start < 0:
        return None
    body = text[start:]
    depth = 0
    in_string = False
    escaped = False
    last_safe: Optional[int] = None

    for index, char in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                with contextlib.suppress(ValueError):
                    return json.loads(body[: index + 1])
                return None
            if depth == 1:
                last_safe = index + 1
        elif char == "," and depth == 1:
            last_safe = index

    if last_safe is None:
        return None
    candidate = body[:last_safe].rstrip().rstrip(",") + closer
    try:
        return json.loads(candidate)
    except ValueError:
        return None


def parse_json_array(text: str) -> list:
    body = _strip_fences(text or "")
    data: Any = None
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        match = re.search(r"\[.*\]", body, re.S)
        if match:
            with contextlib.suppress(ValueError):
                data = json.loads(match.group(0))
    if data is None:
        data = _repair_json(body, "[")
    if isinstance(data, dict):
        for key in ("messages", "dialogue", "dialog", "items", "result", "data", "phrases"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    return data if isinstance(data, list) else []


def parse_json_object(text: str) -> Optional[dict]:
    body = _strip_fences(text or "")
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        match = re.search(r"\{.*\}", body, re.S)
        data = None
        if match:
            with contextlib.suppress(ValueError):
                data = json.loads(match.group(0))
        if data is None:
            data = _repair_json(body, "{")   # модель могла обрезать ответ
    return data if isinstance(data, dict) else None


def normalize_dialogue(raw: list) -> list[dict]:
    owner_aliases = {"owner", "me", "self", "out", "outgoing", "right", "я", "мои", "sent"}
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"side": "owner", "text": text[:1000]})
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("message") or item.get("content") or "").strip()
        if not text:
            continue
        side = str(item.get("side") or item.get("from") or item.get("author") or "owner")
        side = "owner" if side.strip().lower() in owner_aliases else "other"
        out.append({"side": side, "text": text[:1000]})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# СБОРКА SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_scalar(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v) or "—"
    text = str(value).strip()
    return text or "—"


def _fmt_list(value: Any, limit: int = 20, bullet: bool = True) -> str:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return "—"
    items = [str(v).strip() for v in value if str(v).strip()][:limit]
    if not items:
        return "—"
    if bullet:
        return "\n".join(f"— {i}" for i in items)
    # фразы сами могут содержать запятые — берём в кавычки, чтобы не слипались
    return ", ".join(f"«{i}»" for i in items)


def _fmt_exchanges(value: Any, limit: int = 10) -> str:
    if not isinstance(value, (list, tuple)):
        return "—"
    lines: list[str] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            question = str(item.get("input") or item.get("question") or "").strip()
            answer = str(item.get("response") or item.get("output") or "").strip()
            if question or answer:
                lines.append(f"— «{question}» → «{answer}»")
        elif isinstance(item, str) and item.strip():
            lines.append(f"— {item.strip()}")
    return "\n".join(lines) or "—"


def render_style_prompt(profile: dict) -> str:
    phrases = list(profile.get("common_phrases") or [])
    for extra in profile.get("sample_phrases") or []:
        if extra not in phrases:
            phrases.append(extra)
    values = {
        "tone": _fmt_scalar(profile.get("tone")),
        "language": _fmt_scalar(profile.get("language")),
        "avg_msg_length": _fmt_scalar(profile.get("avg_msg_length")),
        "swear_style": _fmt_scalar(profile.get("swear_style")),
        "emoji_usage": _fmt_scalar(profile.get("emoji_usage")),
        "greeting_style": _fmt_scalar(profile.get("greeting_style")),
        "agreement_style": _fmt_scalar(profile.get("agreement_style")),
        "refusal_style": _fmt_scalar(profile.get("refusal_style")),
        "question_style": _fmt_scalar(profile.get("question_style")),
        "reaction_to_questions": _fmt_scalar(profile.get("reaction_to_questions")),
        "reaction_to_offers": _fmt_scalar(profile.get("reaction_to_offers")),
        "reaction_to_complaints": _fmt_scalar(profile.get("reaction_to_complaints")),
        "conversation_patterns": _fmt_list(profile.get("conversation_patterns"), 12),
        "sample_exchanges": _fmt_exchanges(profile.get("sample_exchanges")),
        "common_phrases": _fmt_list(phrases, 25, bullet=False),
    }
    prompt = STYLE_PROMPT_TEMPLATE.format(**values)
    jokes = _fmt_scalar(profile.get("reaction_to_jokes"))
    if jokes != "—":
        prompt += f"\n\nКак реагируешь на шутки: {jokes}"
    return prompt


async def build_system_prompt(owner_id: int) -> str:
    """
    Профиль стиля (если есть) ВСЕГДА важнее DEFAULT_SYSTEM_PROMPT.
    Кастомный /prompt имеет приоритет и ДОПОЛНЯЕТ профиль стиля.
    """
    profile = await get_style_profile(owner_id)
    custom = await get_custom_prompt(owner_id)

    if profile:
        prompt = render_style_prompt(profile)
        if custom:
            prompt += (
                "\n\nПРИОРИТЕТНЫЕ ИНСТРУКЦИИ ВЛАДЕЛЬЦА "
                "(важнее всего остального, но стиль сохраняй):\n" + custom.strip()
            )
        return prompt

    if custom:
        return custom.strip()

    return DEFAULT_SYSTEM_PROMPT


# ─────────────────────────────────────────────────────────────────────────────
# БОТ
# ─────────────────────────────────────────────────────────────────────────────

def _check_config() -> None:
    problems: list[str] = []
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_ТОКЕН" or ":" not in BOT_TOKEN:
        problems.append(
            "BOT_TOKEN не задан. Создай бота у @BotFather, включи\n"
            "  Bot Settings → Business Mode → Enable, затем положи токен\n"
            "  в .env рядом со скриптом:  BOT_TOKEN=123456:AA..."
        )
    if not GROQ_API_KEY:
        problems.append(
            "GROQ_API_KEY не задан. Возьми бесплатный ключ на console.groq.com\n"
            "  и положи в .env:  GROQ_API_KEY=gsk_..."
        )
    if problems:
        sys.exit("\n".join("❌ " + p for p in problems))


_check_config()

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher()

BOT_ID: int = 0
BOT_USERNAME: str = ""

# антидубли: id сообщений, отправленных самим ботом от имени владельца
_own_messages: dict[tuple[str, int], float] = {}
# по одному «мыслительному процессу» на чат
_chat_locks: dict[tuple[str, int], asyncio.Lock] = {}
# время последнего отправленного ответа на чат (rate limit)
_last_reply_at: dict[tuple[str, int], float] = {}
# троттлинг уведомлений владельцу об ошибках Groq
_last_error_notice: dict[int, float] = {}


def _chat_lock(connection_id: str, chat_id: int) -> asyncio.Lock:
    key = (connection_id, chat_id)
    lock = _chat_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[key] = lock
    return lock


def _remember_own_message(connection_id: str, message_id: int) -> None:
    _own_messages[(connection_id, message_id)] = time.time()
    if len(_own_messages) > 5000:
        cutoff = time.time() - 3600
        for key, stamp in list(_own_messages.items()):
            if stamp < cutoff:
                _own_messages.pop(key, None)


def _is_own_outgoing(message: Message) -> bool:
    """Сообщение отправлено этим же ботом от имени владельца — не реагируем."""
    sender_bot = getattr(message, "sender_business_bot", None)
    if sender_bot is not None and getattr(sender_bot, "id", None) == BOT_ID:
        return True
    return (message.business_connection_id or "", message.message_id) in _own_messages


def extract_can_reply(connection: BusinessConnection) -> bool:
    """Bot API 9.0 заменил can_reply на rights.can_reply — поддерживаем оба."""
    rights = getattr(connection, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(connection, "can_reply", False))


def trim_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    text = (text or "").strip()
    # модель иногда оборачивает ответ в кавычки — снимаем
    if len(text) > 1 and text[0] in "\"«“'" and text[-1] in "\"»”'":
        inner = text[1:-1].strip()
        if inner and '"' not in inner and "«" not in inner:
            text = inner
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if boundary > limit * 0.6:
        cut = cut[: boundary + 1]
    return cut.rstrip()


def split_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        boundary = rest.rfind("\n", 0, limit)
        if boundary < limit * 0.5:
            boundary = limit
        chunks.append(rest[:boundary])
        rest = rest[boundary:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


async def notify_owner(owner_chat_id: Optional[int], text: str) -> None:
    if not owner_chat_id:
        return
    try:
        await bot.send_message(chat_id=owner_chat_id, text=text)
    except Exception as exc:  # владелец не нажимал /start у бота — это норма
        log.debug("не смог написать владельцу %s: %s", owner_chat_id, exc)


async def notify_owner_error(owner_chat_id: Optional[int], owner_id: int, text: str) -> None:
    now = time.time()
    if now - _last_error_notice.get(owner_id, 0.0) < 600:
        return
    _last_error_notice[owner_id] = now
    await notify_owner(owner_chat_id, text)


async def refresh_connection(connection_id: str) -> Optional[dict]:
    """Подтянуть коннект через API, если его нет в БД (например, после сброса базы)."""
    try:
        connection = await bot.get_business_connection(business_connection_id=connection_id)
    except Exception as exc:
        log.warning("getBusinessConnection(%s) не удался: %s", connection_id, exc)
        return None
    owner = connection.user
    owner_chat_id = getattr(connection, "user_chat_id", None) or owner.id
    is_enabled = bool(getattr(connection, "is_enabled", True))
    await save_connection(
        connection.id, owner.id, owner_chat_id, extract_can_reply(connection), not is_enabled
    )
    return await get_connection(connection.id)


# ─────────────────────────────────────────────────────────────────────────────
# 1. ПОДКЛЮЧЕНИЕ / ОТКЛЮЧЕНИЕ БИЗНЕС-АККАУНТА
# ─────────────────────────────────────────────────────────────────────────────

@dp.business_connection()
async def on_business_connection(connection: BusinessConnection) -> None:
    owner = connection.user
    owner_chat_id = getattr(connection, "user_chat_id", None) or owner.id
    can_reply = extract_can_reply(connection)
    is_enabled = bool(getattr(connection, "is_enabled", True))

    await save_connection(connection.id, owner.id, owner_chat_id, can_reply, not is_enabled)
    log.info(
        "business_connection %s | owner=%s enabled=%s can_reply=%s",
        connection.id, owner.id, is_enabled, can_reply,
    )

    if not is_enabled:
        await notify_owner(owner_chat_id, "❌ Автоответчик отключён.\n\nВключить: Настройки → Автоматизация чатов.")
        return

    if not can_reply:
        await notify_owner(
            owner_chat_id,
            "⚠️ Автоответчик подключён, но БЕЗ права отвечать.\n\n"
            "Открой Настройки → Автоматизация чатов → выбери бота и разреши "
            "«Отвечать на сообщения» — иначе я буду только слушать.",
        )
        return

    profile = await get_style_profile(owner.id)
    hint = (
        "🎭 Профиль стиля уже загружен — отвечаю в твоей манере."
        if profile
        else "🎓 Скинь мне сюда скриншоты своих переписок — научусь писать как ты."
    )
    await notify_owner(
        owner_chat_id,
        "✅ Автоответчик подключён.\n\n"
        "Теперь я отвечаю на входящие сообщения от твоего имени.\n"
        f"{hint}\n\n"
        "Команды: /status /prompt /style /pause /resume /clear",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2-3. СООБЩЕНИЯ В БИЗНЕС-ЧАТАХ
# ─────────────────────────────────────────────────────────────────────────────

@dp.business_message()
async def on_business_message(message: Message) -> None:
    connection_id = message.business_connection_id or ""
    if not connection_id:
        return

    connection = await get_connection(connection_id)
    if connection is None:
        connection = await refresh_connection(connection_id)
    if connection is None:
        return

    owner_id = int(connection["owner_id"])
    owner_chat_id = connection.get("owner_chat_id") or owner_id
    chat_id = message.chat.id
    text = (message.text or message.caption or "").strip()

    # наш собственный ответ прилетел эхом — игнор
    if _is_own_outgoing(message):
        return

    from_id = message.from_user.id if message.from_user else None

    # ── 3. написал сам ВЛАДЕЛЕЦ → takeover, Groq не трогаем ──────────────────
    if from_id == owner_id:
        if chat_id == owner_id:      # «Избранное» — не наша история
            return
        if await history_has_message(connection_id, chat_id, message.message_id):
            return
        paused_until = time.time() + OWNER_TAKEOVER_MIN * 60
        await set_takeover(connection_id, chat_id, paused_until)
        if text:
            # ручные ответы владельца попадают в историю — модель учится стилю
            await add_history(connection_id, chat_id, message.message_id, "assistant", text)
        log.info(
            "takeover: владелец ответил сам в чате %s, молчу %s мин",
            chat_id, OWNER_TAKEOVER_MIN,
        )
        return

    # ── 2. входящее от СОБЕСЕДНИКА ──────────────────────────────────────────
    # 6. игнорируем стикеры/голосовые/видео/фото без подписи
    if not text:
        log.debug("нетекстовое сообщение в чате %s — пропускаю", chat_id)
        return

    await add_history(connection_id, chat_id, message.message_id, "user", text)

    if connection["is_disabled"]:
        return
    # 2. can_reply обязателен: нет права — не пытаемся отвечать
    if not connection["can_reply"]:
        log.debug("can_reply=0 для %s — молчу", connection_id)
        return
    if await is_paused(owner_id):
        return
    if await is_taken_over(connection_id, chat_id):
        log.debug("чат %s на паузе после ручного ответа владельца", chat_id)
        return

    key = (connection_id, chat_id)
    lock = _chat_lock(connection_id, chat_id)
    async with lock:
        # пока ждали — пришло сообщение свежее: отвечать будет оно
        if await last_user_message_id(connection_id, chat_id) != message.message_id:
            return

        # 9. rate limit: не чаще одного ответа в CHAT_RATE_LIMIT секунд на чат
        elapsed = time.time() - _last_reply_at.get(key, 0.0)
        if elapsed < CHAT_RATE_LIMIT:
            await asyncio.sleep(CHAT_RATE_LIMIT - elapsed)

        # 4. typing + задержка, чтобы не выглядеть мгновенным ботом
        with contextlib.suppress(Exception):
            await bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING,
                business_connection_id=connection_id,
            )
        await asyncio.sleep(AUTO_REPLY_DELAY)

        # владелец мог влезть, пока мы «печатали»
        if await is_taken_over(connection_id, chat_id) or await is_paused(owner_id):
            return
        if await last_user_message_id(connection_id, chat_id) != message.message_id:
            return

        history = await get_history(connection_id, chat_id, MAX_HISTORY)
        system_prompt = await build_system_prompt(owner_id)
        answer = await get_ai_response(history, system_prompt)

        # 7. ошибку Groq собеседнику НЕ показываем — молчим и пишем владельцу
        if not answer:
            log.warning("нет ответа Groq для чата %s", chat_id)
            await notify_owner_error(
                owner_chat_id,
                owner_id,
                "⚠️ Groq не ответил — сообщение осталось без автоответа. "
                "Проверь ключ и лимиты на console.groq.com.",
            )
            return

        answer = trim_text(answer, MAX_MESSAGE_LENGTH)  # 5. лимит Telegram
        if not answer:
            return

        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=answer,
                business_connection_id=connection_id,
            )
        except Exception as exc:
            log.error("не смог отправить ответ в чат %s: %s", chat_id, exc)
            await notify_owner_error(
                owner_chat_id, owner_id, f"⚠️ Не смог отправить автоответ: {exc}"
            )
            return

        _remember_own_message(connection_id, sent.message_id)
        _last_reply_at[key] = time.time()
        await add_history(connection_id, chat_id, sent.message_id, "assistant", answer)
        log.info("ответил в чате %s (%s символов)", chat_id, len(answer))


# ─────────────────────────────────────────────────────────────────────────────
# 4-5. РЕДАКТИРОВАНИЕ И УДАЛЕНИЕ
# ─────────────────────────────────────────────────────────────────────────────

@dp.edited_business_message()
async def on_edited_business_message(message: Message) -> None:
    connection_id = message.business_connection_id or ""
    text = (message.text or message.caption or "").strip()
    if not connection_id or not text:
        return
    updated = await update_history_message(
        connection_id, message.chat.id, message.message_id, text
    )
    if updated:
        log.info("история обновлена: чат %s, сообщение %s", message.chat.id, message.message_id)


@dp.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted) -> None:
    removed = await delete_history_messages(
        event.business_connection_id, event.chat.id, list(event.message_ids or [])
    )
    if removed:
        log.info("удалено из истории: %s сообщений (чат %s)", removed, event.chat.id)


# ─────────────────────────────────────────────────────────────────────────────
# КОМАНДЫ В ЛС БОТА (от владельца)
# ─────────────────────────────────────────────────────────────────────────────

PRIVATE = F.chat.type == "private"

START_TEXT = (
    "👋 Я — автоответчик для твоего Telegram.\n\n"
    "Чтобы подключить автоответчик:\n"
    "Настройки → Автоматизация чатов → введи {username}\n"
    "Premium НЕ нужен!\n\n"
    "Не забудь разрешить боту «Отвечать на сообщения».\n\n"
    "После подключения я сам отвечаю на входящие от твоего имени.\n"
    "Как только ты ответишь в чате сам — замолкаю на "
    f"{OWNER_TAKEOVER_MIN} мин в этом чате.\n\n"
    "🎓 ОБУЧЕНИЕ СТИЛЮ\n"
    "Скинь мне сюда скриншоты своих переписок — я разберу их и буду писать\n"
    "как ты: та же манера, те же словечки. Чем больше скринов, тем точнее\n"
    "(минимум 5-10).\n\n"
    "КОМАНДЫ\n"
    "/prompt <текст> — свой системный промпт\n"
    "/prompt — показать текущий\n"
    "/status — статус и статистика\n"
    "/style — профиль стиля\n"
    "/style_stats — сколько скринов обработано\n"
    "/style_reset — сбросить обучение\n"
    "/pause — замолчать во всех чатах\n"
    "/resume — снова отвечать\n"
    "/clear — очистить всю историю переписок"
)


async def _guard(message: Message) -> Optional[int]:
    """Пускаем только владельцев, подключивших бота (и админа)."""
    user = message.from_user
    if user is None:
        return None
    if user.id == ADMIN_ID or await is_connected_owner(user.id):
        return user.id
    await message.answer(
        "Сначала подключи автоответчик:\n"
        f"Настройки → Автоматизация чатов → {BOT_USERNAME or '@бот'}\n"
        "Premium не нужен."
    )
    return None


async def _answer_long(message: Message, text: str) -> None:
    for chunk in split_text(text):
        await message.answer(chunk)


@dp.message(CommandStart(), PRIVATE)
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT.format(username=BOT_USERNAME or "@имя_бота"))


@dp.message(Command("help"), PRIVATE)
async def cmd_help(message: Message) -> None:
    await message.answer(START_TEXT.format(username=BOT_USERNAME or "@имя_бота"))


@dp.message(Command("prompt"), PRIVATE)
async def cmd_prompt(message: Message, command: CommandObject) -> None:
    owner_id = await _guard(message)
    if owner_id is None:
        return

    text = (command.args or "").strip()
    if not text:
        current = await get_custom_prompt(owner_id)
        profile = await get_style_profile(owner_id)
        if current:
            await _answer_long(message, "📝 Текущий кастомный промпт:\n\n" + current)
        else:
            await message.answer(
                "📝 Кастомный промпт не задан.\n\n"
                + ("Использую профиль стиля из скриншотов." if profile
                   else "Использую промпт по умолчанию.")
                + "\n\nЗадать: /prompt Ты продавец NFT подарков. Отвечай на русском."
            )
        return

    await set_custom_prompt(owner_id, text)
    profile = await get_style_profile(owner_id)
    note = " Он дополняет профиль стиля." if profile else ""
    await message.answer(f"✅ Промпт сохранён.{note}\n\n{trim_text(text, 500)}")


@dp.message(Command("status"), PRIVATE)
async def cmd_status(message: Message) -> None:
    owner_id = await _guard(message)
    if owner_id is None:
        return

    connections = await get_owner_connections(owner_id)
    messages_total, chats_total = await owner_history_stats(owner_id)
    paused = await is_paused(owner_id)
    custom = await get_custom_prompt(owner_id)
    profile = await get_style_profile(owner_id)
    screenshots, own_phrases, _, _ = await style_stats(owner_id)

    if connections:
        active = [c for c in connections if not c["is_disabled"]]
        can_reply = any(c["can_reply"] for c in active)
        conn_line = (
            f"🔌 Подключений: {len(active)} активных из {len(connections)}\n"
            f"✍️ Право отвечать: {'да' if can_reply else 'НЕТ — включи в настройках'}"
        )
    else:
        conn_line = "🔌 Подключений нет — Настройки → Автоматизация чатов"

    if custom:
        prompt_line = "📝 Промпт (кастомный): " + trim_text(custom, 100)
    elif profile:
        prompt_line = "📝 Промпт: профиль стиля — " + _fmt_scalar(profile.get("tone"))[:100]
    else:
        prompt_line = "📝 Промпт: по умолчанию"

    await _answer_long(
        message,
        "📊 СТАТУС\n\n"
        f"{conn_line}\n"
        f"{'⏸ На паузе — молчу во всех чатах' if paused else '▶️ Работаю'}\n\n"
        f"💬 Чатов обработано: {chats_total}\n"
        f"✉️ Сообщений в истории: {messages_total}\n"
        f"🎓 Скриншотов изучено: {screenshots} ({own_phrases} твоих фраз)\n\n"
        f"{prompt_line}\n\n"
        f"🤖 Модель: {_active_text_model}",
    )


@dp.message(Command("pause"), PRIVATE)
async def cmd_pause(message: Message) -> None:
    owner_id = await _guard(message)
    if owner_id is None:
        return
    await set_paused(owner_id, True)
    await message.answer("⏸ Пауза. Молчу во всех чатах.\nВключить обратно: /resume")


@dp.message(Command("resume"), PRIVATE)
async def cmd_resume(message: Message) -> None:
    owner_id = await _guard(message)
    if owner_id is None:
        return
    await set_paused(owner_id, False)
    await message.answer("▶️ Снова отвечаю на входящие.")


@dp.message(Command("clear"), PRIVATE)
async def cmd_clear(message: Message) -> None:
    owner_id = await _guard(message)
    if owner_id is None:
        return
    removed = await clear_owner_history(owner_id)
    await message.answer(
        f"🧹 История очищена: удалено {removed} сообщений.\n"
        "Профиль стиля не тронут (сбросить: /style_reset)."
    )


@dp.message(Command("style"), PRIVATE)
async def cmd_style(message: Message) -> None:
    owner_id = await _guard(message)
    if owner_id is None:
        return

    profile = await get_style_profile(owner_id)
    if not profile:
        await message.answer(
            "🎭 Профиль стиля пуст.\n\n"
            "Скинь мне скриншоты своих переписок — я разберу их и научусь\n"
            "писать как ты. Минимум 5-10 скринов для точности."
        )
        return

    lines = ["🎭 ПРОФИЛЬ СТИЛЯ\n"]
    labels = {
        "tone": "Тон",
        "language": "Язык",
        "avg_msg_length": "Длина сообщений",
        "swear_style": "Мат",
        "emoji_usage": "Эмодзи",
        "greeting_style": "Приветствие",
        "agreement_style": "Согласие",
        "refusal_style": "Отказ",
        "question_style": "Вопросы",
        "reaction_to_questions": "На вопросы",
        "reaction_to_offers": "На предложения",
        "reaction_to_complaints": "На негатив",
        "reaction_to_jokes": "На шутки",
    }
    for key, label in labels.items():
        value = _fmt_scalar(profile.get(key))
        if value != "—":
            lines.append(f"• {label}: {value}")

    phrases = _fmt_list(profile.get("common_phrases"), 15, bullet=False)
    if phrases != "—":
        lines.append(f"\n🗣 Типичные фразы:\n{phrases}")

    patterns = _fmt_list(profile.get("conversation_patterns"), 8)
    if patterns != "—":
        lines.append(f"\n🔁 Паттерны:\n{patterns}")

    exchanges = _fmt_exchanges(profile.get("sample_exchanges"), 6)
    if exchanges != "—":
        lines.append(f"\n💬 Примеры диалогов:\n{exchanges}")

    await _answer_long(message, "\n".join(lines))


@dp.message(Command("style_stats"), PRIVATE)
async def cmd_style_stats(message: Message) -> None:
    owner_id = await _guard(message)
    if owner_id is None:
        return
    screenshots, own_phrases, other_phrases, updated_at = await style_stats(owner_id)
    if not screenshots:
        await message.answer("📈 Скриншотов пока нет. Скинь первый — начну учиться.")
        return
    when = (
        time.strftime("%d.%m.%Y %H:%M", time.localtime(updated_at)) if updated_at else "—"
    )
    quality = "🔥 отличная" if screenshots >= 10 else ("👍 неплохая" if screenshots >= 5 else "🌱 слабая, кидай ещё")
    await message.answer(
        "📈 СТАТИСТИКА ОБУЧЕНИЯ\n\n"
        f"🖼 Скриншотов обработано: {screenshots}\n"
        f"🗣 Твоих фраз: {own_phrases}\n"
        f"👤 Фраз собеседников: {other_phrases}\n"
        f"🕐 Профиль обновлён: {when}\n\n"
        f"Точность: {quality}"
    )


@dp.message(Command("style_reset"), PRIVATE)
async def cmd_style_reset(message: Message) -> None:
    owner_id = await _guard(message)
    if owner_id is None:
        return
    await reset_style(owner_id)
    await message.answer("🗑 Профиль стиля и все распознанные фразы удалены.")


# ─────────────────────────────────────────────────────────────────────────────
# ОБУЧЕНИЕ ПО СКРИНШОТАМ
# ─────────────────────────────────────────────────────────────────────────────

_albums: dict[tuple[int, str], list[Message]] = {}
_album_tasks: dict[tuple[int, str], asyncio.Task] = {}


def _pick_photo(sizes: list[PhotoSize]) -> PhotoSize:
    """Самое большое разрешение, которое влезает в лимит vision API."""
    ordered = sorted(sizes, key=lambda p: (p.width or 0) * (p.height or 0))
    for photo in reversed(ordered):
        if (photo.file_size or 0) <= VISION_MAX_IMAGE_BYTES:
            return photo
    return ordered[0]


async def download_image(message: Message) -> Optional[tuple[str, str]]:
    """→ (base64, mime) либо None."""
    mime = "image/jpeg"
    if message.photo:
        file_id = _pick_photo(list(message.photo)).file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        if (message.document.file_size or 0) > VISION_MAX_IMAGE_BYTES:
            return None
        file_id = message.document.file_id
        mime = message.document.mime_type or mime
    else:
        return None

    try:
        file = await bot.get_file(file_id)
        buffer = await bot.download_file(file.file_path)
        data = buffer.read()
    except Exception as exc:
        log.error("не смог скачать изображение: %s", exc)
        return None

    if not data or len(data) > VISION_MAX_IMAGE_BYTES:
        return None
    return base64.b64encode(data).decode(), mime


async def _edit_or_answer(status: Optional[Message], anchor: Message, text: str) -> None:
    if status is not None:
        try:
            await status.edit_text(text)
            return
        except Exception:  # сообщение удалили / текст не изменился
            pass
    with contextlib.suppress(Exception):
        await anchor.answer(text)


async def process_training_images(anchor: Message, messages: list[Message]) -> None:
    owner_id = anchor.from_user.id
    status: Optional[Message] = None
    with contextlib.suppress(Exception):
        status = await anchor.answer(
            "🔍 Анализирую переписку…"
            if len(messages) == 1
            else f"🔍 Анализирую переписку ({len(messages)} скриншотов)…"
        )

    recognized = 0
    new_lines = 0
    for item in messages:
        image = await download_image(item)
        if image is None:
            continue
        base64_image, mime = image
        dialogue = await extract_dialogue_from_screenshot(base64_image, mime)
        if not dialogue:
            continue
        await save_raw_dialogue(owner_id, dialogue)
        recognized += 1
        new_lines += len(dialogue)

    if not recognized:
        await _edit_or_answer(
            status,
            anchor,
            "❌ Не удалось распознать сообщения.\n"
            "Пришли скриншот покрупнее, где видно текст переписки.",
        )
        return

    await _edit_or_answer(status, anchor, f"✅ Распознано {new_lines} сообщений. Разбираю стиль…")

    dialogues = await get_all_dialogues(owner_id)
    profile = await analyze_style(dialogues)
    if not profile:
        await _edit_or_answer(
            status,
            anchor,
            f"⚠️ Распознал {new_lines} сообщений, но анализ стиля не удался "
            "(Groq не ответил). Фразы сохранены — попробуй прислать ещё скрин "
            "или повтори позже.",
        )
        return

    await save_style_profile(owner_id, profile)
    await attach_last_analysis(owner_id, profile)

    screenshots, own_phrases, _, _ = await style_stats(owner_id)
    phrases_preview = ", ".join(
        str(p) for p in (profile.get("common_phrases") or [])[:5] if str(p).strip()
    ) or "—"
    tail = (
        "Скинь ещё скриншотов для точности!"
        if screenshots < 10
        else "Профиль насыщенный — можно пользоваться."
    )
    await _edit_or_answer(
        status,
        anchor,
        f"✅ Извлечено {new_lines} сообщений (скриншотов всего: {screenshots}, "
        f"твоих фраз: {own_phrases})\n\n"
        f"📝 Тон: {_fmt_scalar(profile.get('tone'))}\n"
        f"🗣 Фразы: {phrases_preview}\n\n"
        f"{tail}",
    )


async def _flush_album(key: tuple[int, str]) -> None:
    try:
        await asyncio.sleep(ALBUM_WAIT)
    except asyncio.CancelledError:
        return
    batch = _albums.pop(key, [])
    _album_tasks.pop(key, None)
    if batch:
        await process_training_images(batch[0], batch)


@dp.message(F.photo | F.document, PRIVATE)
async def handle_training_photo(message: Message) -> None:
    owner_id = await _guard(message)
    if owner_id is None:
        return

    if not message.photo and not (message.document and (message.document.mime_type or "").startswith("image/")):
        await message.answer("Пришли именно скриншот переписки — фото или файл-картинку.")
        return

    group_id = message.media_group_id
    if group_id:
        key = (owner_id, group_id)
        _albums.setdefault(key, []).append(message)
        task = _album_tasks.get(key)
        if task is not None:
            task.cancel()
        _album_tasks[key] = asyncio.create_task(_flush_album(key))
        return

    await process_training_images(message, [message])


@dp.message(PRIVATE)
async def handle_other_private(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    if user.id == ADMIN_ID or await is_connected_owner(user.id):
        await message.answer(
            "Я отвечаю за тебя в бизнес-чатах.\n"
            "Скинь скриншот переписки — научусь твоему стилю.\n"
            "Команды: /status /prompt /style /pause /resume /help"
        )
    else:
        await message.answer(START_TEXT.format(username=BOT_USERNAME or "@имя_бота"))


# ─────────────────────────────────────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    global BOT_ID, BOT_USERNAME

    await db_init()

    await resolve_models()

    me = await bot.get_me()
    BOT_ID = me.id
    BOT_USERNAME = f"@{me.username}" if me.username else ""
    log.info("Запущен как %s (id=%s)", BOT_USERNAME or me.full_name, BOT_ID)
    log.info(
        "Подключение у пользователя: Настройки → Автоматизация чатов → %s",
        BOT_USERNAME or "бот",
    )
    log.info("Не забудь: @BotFather → Bot Settings → Business Mode → Enable")

    with contextlib.suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db_close()
        with contextlib.suppress(Exception):
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлен")
