"""
Телеграм-бот для рассылки рекламных сообщений (крипто-обмен).

Зависимости:
    pip install aiogram telethon SQLAlchemy aiosqlite cryptography python-dotenv

Переменные окружения (.env):
    BOT_TOKEN=токен_бота
    ADMIN_USER_ID=123456789
    DATABASE_URL=sqlite+aiosqlite:///bot.db
    ENCRYPTION_KEY=<ключ fernet>
    LOG_FILE=bot.log

Сгенерировать ENCRYPTION_KEY:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import asyncio
import base64
import csv
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from itertools import cycle
from typing import Any, Awaitable, Callable

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
    text as sqlalchemy_text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityCustomEmoji,
    MessageEntityItalic,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
)

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

# ══════════════════════════════════════════════
# Конфигурация
# ══════════════════════════════════════════════

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_USER_ID: int = int(os.environ["ADMIN_USER_ID"])
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")
ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")
LOG_FILE: str = os.getenv("LOG_FILE", "bot.log")
MIN_INTERVAL_SECONDS: int = 5
DUPLICATE_CHECK_DAYS: int = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("broadcast_bot")

# ══════════════════════════════════════════════
# Шифрование
# ══════════════════════════════════════════════

_fernet_key: bytes | None = None


def _get_key() -> bytes:
    global _fernet_key
    if _fernet_key is not None:
        return _fernet_key
    if ENCRYPTION_KEY:
        _fernet_key = ENCRYPTION_KEY.encode()
    else:
        _fernet_key = Fernet.generate_key()
        logger.warning(
            "ENCRYPTION_KEY не задан. Сгенерирован временный ключ: %s",
            _fernet_key.decode(),
        )
    return _fernet_key


def encrypt(plaintext: str) -> str:
    return base64.urlsafe_b64encode(
        Fernet(_get_key()).encrypt(plaintext.encode())
    ).decode()


def decrypt(ciphertext: str) -> str:
    return Fernet(_get_key()).decrypt(
        base64.urlsafe_b64decode(ciphertext.encode())
    ).decode()


# ══════════════════════════════════════════════
# Утилиты
# ══════════════════════════════════════════════

_TG_LINK_RE = re.compile(r"https?://t\.me/([A-Za-z0-9_]{5,})")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_recipients(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for link_match in _TG_LINK_RE.finditer(text):
        val = link_match.group(1).lower()
        if val not in seen:
            seen.add(val)
            result.append(link_match.group(1))

    cleaned = _TG_LINK_RE.sub(" ", text)

    for line in cleaned.splitlines():
        for part in re.split(r"[,;\s]+", line):
            value = part.strip().lstrip("@")
            if not value:
                continue
            lower = value.lower()
            if lower not in seen:
                seen.add(lower)
                result.append(value)

    return result


def export_logs_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def serialize_entities(entities: list | None) -> str | None:
    if not entities:
        return None
    result = []
    for e in entities:
        entry = {"type": e.type, "offset": e.offset, "length": e.length}
        if getattr(e, "url", None):
            entry["url"] = e.url
        if getattr(e, "language", None):
            entry["language"] = e.language
        if getattr(e, "custom_emoji_id", None):
            entry["custom_emoji_id"] = str(e.custom_emoji_id)
        result.append(entry)
    return json.dumps(result, ensure_ascii=False)


_ENTITY_TYPE_MAP = {
    "bold": MessageEntityBold,
    "italic": MessageEntityItalic,
    "underline": MessageEntityUnderline,
    "strikethrough": MessageEntityStrike,
    "code": MessageEntityCode,
    "spoiler": MessageEntitySpoiler,
    "blockquote": MessageEntityBlockquote,
}


def deserialize_to_telethon_entities(json_str: str | None) -> list | None:
    if not json_str:
        return None
    data = json.loads(json_str)
    entities = []
    for e in data:
        t = e["type"]
        offset = e["offset"]
        length = e["length"]
        cls = _ENTITY_TYPE_MAP.get(t)
        if cls:
            entities.append(cls(offset=offset, length=length))
        elif t == "pre":
            entities.append(
                MessageEntityPre(offset=offset, length=length, language=e.get("language", ""))
            )
        elif t == "text_link":
            entities.append(
                MessageEntityTextUrl(offset=offset, length=length, url=e.get("url", ""))
            )
        elif t == "custom_emoji":
            doc_id = int(e.get("custom_emoji_id", 0))
            if doc_id:
                entities.append(
                    MessageEntityCustomEmoji(offset=offset, length=length, document_id=doc_id)
                )
    return entities if entities else None


# ══════════════════════════════════════════════
# Модели базы данных
# ══════════════════════════════════════════════

engine = create_async_engine(DATABASE_URL, echo=False)
async_session_factory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_db() -> AsyncSession:
    return async_session_factory()


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_id: Mapped[str] = mapped_column(String(256), nullable=False)
    api_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    session_string: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class CampaignAccount(Base):
    __tablename__ = "campaign_accounts"
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_entities: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accounts: Mapped[list[Account]] = relationship(
        "Account", secondary="campaign_accounts", lazy="selectin"
    )
    recipients: Mapped[list["Recipient"]] = relationship(
        "Recipient", back_populates="campaign", lazy="selectin"
    )


class Recipient(Base):
    __tablename__ = "recipients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    campaign: Mapped[Campaign] = relationship("Campaign", back_populates="recipients")


class SentLog(Base):
    __tablename__ = "sent_log"
    __table_args__ = (
        Index("ix_sent_log_dedup", "account_id", "target", "campaign_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


async def init_db() -> None:
    async with engine.begin() as conn:
        try:
            result = await conn.execute(
                sqlalchemy_text("SELECT sql FROM sqlite_master WHERE name='sent_log'")
            )
            row = result.fetchone()
            if row and row[0] and "BIGINT" in row[0].upper():
                await conn.execute(sqlalchemy_text("DROP TABLE sent_log"))
                logger.info("Миграция: пересоздана таблица sent_log (BIGINT -> INTEGER)")
        except Exception:
            pass
        await conn.run_sync(Base.metadata.create_all)
        for col, col_type in [
            ("text_entities", "TEXT"),
            ("photo_file_id", "VARCHAR(512)"),
        ]:
            try:
                await conn.execute(
                    sqlalchemy_text(
                        f"ALTER TABLE campaigns ADD COLUMN {col} {col_type}"
                    )
                )
                logger.info("Миграция: добавлена колонка %s", col)
            except Exception:
                pass


# ══════════════════════════════════════════════
# Управление аккаунтами
# ══════════════════════════════════════════════


async def add_account_to_db(
    api_id: int, api_hash: str, phone: str, session_string: str
) -> Account:
    async with await get_db() as session:
        account = Account(
            api_id=encrypt(str(api_id)),
            api_hash=encrypt(api_hash),
            phone=phone,
            session_string=encrypt(session_string),
            is_active=True,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
        logger.info("Аккаунт добавлен: id=%d phone=%s", account.id, phone)
        return account


async def list_accounts_db() -> list[Account]:
    async with await get_db() as session:
        result = await session.execute(select(Account).order_by(Account.id))
        return list(result.scalars().all())


async def remove_account_db(account_id: int) -> bool:
    async with await get_db() as session:
        account = await session.get(Account, account_id)
        if not account:
            return False
        await session.delete(account)
        await session.commit()
        logger.info("Аккаунт удален: id=%d", account_id)
        return True


def make_telethon_client(account: Account) -> TelegramClient:
    api_id = int(decrypt(account.api_id))
    api_hash = decrypt(account.api_hash)
    session_str = decrypt(account.session_string)
    return TelegramClient(StringSession(session_str), api_id, api_hash)


# ══════════════════════════════════════════════
# Отправка сообщений + проверка дублей
# ══════════════════════════════════════════════


async def is_duplicate(account_id: int, target: str, campaign_id: int) -> bool:
    cutoff = now_utc() - timedelta(days=DUPLICATE_CHECK_DAYS)
    async with await get_db() as session:
        stmt = (
            select(SentLog.id)
            .where(
                SentLog.account_id == account_id,
                SentLog.target == target,
                SentLog.campaign_id == campaign_id,
                SentLog.sent_at >= cutoff,
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar() is not None


async def send_one_message(
    account: Account,
    recipient: Recipient,
    text: str | None,
    campaign_id: int,
    text_entities: str | None = None,
    photo_file_id: str | None = None,
) -> bool:
    if not text and not photo_file_id:
        logger.error("Кампания %d: пустое сообщение (нет текста и фото)", campaign_id)
        return False

    if await is_duplicate(account.id, recipient.target, campaign_id):
        logger.info(
            "Дубль пропущен: аккаунт=%d получатель=%s кампания=%d",
            account.id,
            recipient.target,
            campaign_id,
        )
        async with await get_db() as session:
            r = await session.get(Recipient, recipient.id)
            if r:
                r.status = "duplicate"
                await session.commit()
        return False

    msg_text = text or ""
    client = make_telethon_client(account)
    try:
        await client.connect()
        target: str | int = recipient.target
        if recipient.target.isdigit():
            target = int(recipient.target)
        telethon_ents = deserialize_to_telethon_entities(text_entities)
        if photo_file_id and _bot_instance:
            try:
                photo_io = await _bot_instance.download(photo_file_id)
            except Exception as dl_err:
                logger.warning("Не удалось скачать фото: %s", dl_err)
                photo_io = None
            if photo_io:
                photo_bytes = photo_io.read()
                await client.send_file(
                    target, photo_bytes,
                    caption=msg_text or None,
                    formatting_entities=telethon_ents,
                )
            elif msg_text:
                if telethon_ents:
                    await client.send_message(target, msg_text, formatting_entities=telethon_ents)
                else:
                    await client.send_message(target, msg_text)
            else:
                logger.error("Фото не скачалось и текст пустой, пропуск")
                return False
        elif msg_text:
            if telethon_ents:
                await client.send_message(target, msg_text, formatting_entities=telethon_ents)
            else:
                await client.send_message(target, msg_text)
        else:
            logger.error("Нечего отправлять: нет текста и фото")
            return False

        ts = now_utc()
        async with await get_db() as session:
            r = await session.get(Recipient, recipient.id)
            if r:
                r.status = "sent"
                r.sent_at = ts
            log_entry = SentLog(
                account_id=account.id,
                recipient_id=recipient.id,
                campaign_id=campaign_id,
                target=recipient.target,
                sent_at=ts,
            )
            session.add(log_entry)
            await session.commit()

        logger.info(
            "Отправлено: аккаунт=%d -> %s (кампания=%d)",
            account.id,
            recipient.target,
            campaign_id,
        )
        return True

    except Exception as exc:
        logger.error(
            "Ошибка отправки: аккаунт=%d получатель=%s ошибка=%s",
            account.id,
            recipient.target,
            exc,
        )
        async with await get_db() as session:
            r = await session.get(Recipient, recipient.id)
            if r:
                r.status = "error"
                r.error_message = str(exc)[:500]
                await session.commit()
        return False
    finally:
        await client.disconnect()


# ══════════════════════════════════════════════
# Управление кампаниями
# ══════════════════════════════════════════════

_running_tasks: dict[int, asyncio.Task] = {}
_stop_events: dict[int, asyncio.Event] = {}
_bot_instance: Bot | None = None


async def create_campaign_db(
    name: str,
    text: str,
    interval_seconds: int,
    account_ids: list[int],
    targets: list[str],
    text_entities: str | None = None,
    photo_file_id: str | None = None,
) -> Campaign:
    async with await get_db() as session:
        campaign = Campaign(
            name=name, text=text, text_entities=text_entities,
            photo_file_id=photo_file_id,
            interval_seconds=interval_seconds, status="draft",
        )
        session.add(campaign)
        await session.flush()
        for aid in account_ids:
            session.add(CampaignAccount(campaign_id=campaign.id, account_id=aid))
        for t in targets:
            session.add(Recipient(campaign_id=campaign.id, target=t, status="pending"))
        await session.commit()
        await session.refresh(campaign)
        logger.info("Кампания создана: id=%d название=%s", campaign.id, name)
        return campaign


async def list_campaigns_db() -> list[Campaign]:
    async with await get_db() as session:
        result = await session.execute(select(Campaign).order_by(Campaign.id.desc()))
        return list(result.scalars().all())


async def get_campaign_db(campaign_id: int) -> Campaign | None:
    async with await get_db() as session:
        return await session.get(Campaign, campaign_id)


async def update_campaign_db(campaign_id: int, **kwargs: Any) -> bool:
    async with await get_db() as session:
        campaign = await session.get(Campaign, campaign_id)
        if not campaign or campaign.status not in ("draft", "paused"):
            return False
        for key, value in kwargs.items():
            if hasattr(campaign, key):
                setattr(campaign, key, value)
        await session.commit()
        return True


async def add_recipients_to_campaign(campaign_id: int, targets: list[str]) -> int:
    async with await get_db() as session:
        campaign = await session.get(Campaign, campaign_id)
        if not campaign:
            return 0
        existing_stmt = select(Recipient.target).where(
            Recipient.campaign_id == campaign_id
        )
        existing_result = await session.execute(existing_stmt)
        existing_targets = {r.lower() for r in existing_result.scalars()}
        added = 0
        for t in targets:
            if t.lower() not in existing_targets:
                session.add(
                    Recipient(campaign_id=campaign_id, target=t, status="pending")
                )
                existing_targets.add(t.lower())
                added += 1
        await session.commit()
        return added


async def get_campaign_accounts_db(campaign_id: int) -> list[Account]:
    async with await get_db() as session:
        stmt = (
            select(Account)
            .join(CampaignAccount, CampaignAccount.account_id == Account.id)
            .where(
                CampaignAccount.campaign_id == campaign_id,
                Account.is_active.is_(True),
            )
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_pending_recipients_db(campaign_id: int) -> list[Recipient]:
    async with await get_db() as session:
        stmt = (
            select(Recipient)
            .where(Recipient.campaign_id == campaign_id, Recipient.status == "pending")
            .order_by(Recipient.id)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def campaign_stats_db(campaign_id: int) -> dict:
    async with await get_db() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(Recipient)
            .where(Recipient.campaign_id == campaign_id)
        )
        sent = await session.scalar(
            select(func.count())
            .select_from(Recipient)
            .where(Recipient.campaign_id == campaign_id, Recipient.status == "sent")
        )
        errors = await session.scalar(
            select(func.count())
            .select_from(Recipient)
            .where(Recipient.campaign_id == campaign_id, Recipient.status == "error")
        )
        duplicates = await session.scalar(
            select(func.count())
            .select_from(Recipient)
            .where(
                Recipient.campaign_id == campaign_id, Recipient.status == "duplicate"
            )
        )
        pending = await session.scalar(
            select(func.count())
            .select_from(Recipient)
            .where(Recipient.campaign_id == campaign_id, Recipient.status == "pending")
        )
        recent_stmt = (
            select(Recipient)
            .where(Recipient.campaign_id == campaign_id, Recipient.status == "sent")
            .order_by(Recipient.sent_at.desc())
            .limit(10)
        )
        recent = await session.execute(recent_stmt)
        recent_list = [
            {"target": r.target, "sent_at": str(r.sent_at)} for r in recent.scalars()
        ]
    return {
        "total": total or 0,
        "sent": sent or 0,
        "errors": errors or 0,
        "duplicates": duplicates or 0,
        "pending": pending or 0,
        "recent": recent_list,
    }


async def export_campaign_logs_db(campaign_id: int) -> list[dict]:
    async with await get_db() as session:
        stmt = (
            select(SentLog)
            .where(SentLog.campaign_id == campaign_id)
            .order_by(SentLog.sent_at.desc())
        )
        result = await session.execute(stmt)
        return [
            {
                "account_id": row.account_id,
                "target": row.target,
                "sent_at": str(row.sent_at),
            }
            for row in result.scalars()
        ]


async def _notify_admin(text: str) -> None:
    if _bot_instance:
        try:
            await _bot_instance.send_message(ADMIN_USER_ID, text)
        except Exception:
            logger.error("Не удалось отправить уведомление админу")


async def _run_campaign_loop(campaign_id: int) -> None:
    stop_event = _stop_events.get(campaign_id)
    if not stop_event:
        return

    campaign = await get_campaign_db(campaign_id)
    if not campaign:
        return

    if not campaign.text:
        await _notify_admin(
            f"Рассылка [{campaign_id}]: текст сообщения пустой!\n"
            "Отредактируй текст: /edit_campaign " + str(campaign_id)
        )
        async with await get_db() as session:
            c = await session.get(Campaign, campaign_id)
            if c:
                c.status = "paused"
                await session.commit()
        _stop_events.pop(campaign_id, None)
        _running_tasks.pop(campaign_id, None)
        return

    accounts = await get_campaign_accounts_db(campaign_id)
    if not accounts:
        logger.error("Кампания %d: нет активных аккаунтов", campaign_id)
        await _notify_admin(f"Рассылка [{campaign_id}]: нет активных аккаунтов!")
        async with await get_db() as session:
            c = await session.get(Campaign, campaign_id)
            if c:
                c.status = "paused"
                await session.commit()
        _stop_events.pop(campaign_id, None)
        _running_tasks.pop(campaign_id, None)
        return

    account_cycle = cycle(accounts)
    consecutive_errors = 0
    max_consecutive_errors = 10

    try:
        while not stop_event.is_set():
            pending = await get_pending_recipients_db(campaign_id)
            if not pending:
                break

            recipient = pending[0]
            account = next(account_cycle)
            try:
                success = await send_one_message(
                    account, recipient, campaign.text, campaign_id,
                    campaign.text_entities, campaign.photo_file_id,
                )
                if success:
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
            except Exception as exc:
                consecutive_errors += 1
                logger.error(
                    "Ошибка в цикле рассылки %d: %s", campaign_id, exc
                )
                async with await get_db() as session:
                    r = await session.get(Recipient, recipient.id)
                    if r and r.status == "pending":
                        r.status = "error"
                        r.error_message = str(exc)[:500]
                        await session.commit()

            if consecutive_errors >= max_consecutive_errors:
                await _notify_admin(
                    f"Рассылка [{campaign_id}]: {max_consecutive_errors} ошибок подряд.\n"
                    f"Рассылка приостановлена. Проверь аккаунты и логи."
                )
                break

            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=campaign.interval_seconds
                )
                break
            except asyncio.TimeoutError:
                pass

    except Exception as exc:
        logger.error("Критическая ошибка в рассылке %d: %s", campaign_id, exc)
        await _notify_admin(
            f"Рассылка [{campaign_id}] упала с ошибкой:\n{exc}"
        )

    async with await get_db() as session:
        c = await session.get(Campaign, campaign_id)
        if c:
            remaining = await get_pending_recipients_db(campaign_id)
            if not remaining:
                c.status = "finished"
            elif stop_event.is_set():
                c.status = "paused"
            else:
                c.status = "paused"
            await session.commit()

    _stop_events.pop(campaign_id, None)
    _running_tasks.pop(campaign_id, None)
    logger.info("Кампания %d завершена", campaign_id)


async def start_campaign_action(campaign_id: int) -> str | None:
    if campaign_id in _running_tasks:
        return "Кампания уже запущена."
    campaign = await get_campaign_db(campaign_id)
    if not campaign:
        return "Кампания не найдена."
    if campaign.status == "finished":
        return "Кампания уже завершена."
    accounts = await get_campaign_accounts_db(campaign_id)
    if not accounts:
        return "К кампании не привязаны активные аккаунты."
    pending = await get_pending_recipients_db(campaign_id)
    if not pending:
        return "Нет получателей в ожидании."

    async with await get_db() as session:
        c = await session.get(Campaign, campaign_id)
        if c:
            c.status = "active"
            c.started_at = now_utc()
            await session.commit()

    stop_event = asyncio.Event()
    _stop_events[campaign_id] = stop_event
    task = asyncio.create_task(_run_campaign_loop(campaign_id))
    _running_tasks[campaign_id] = task
    return None


async def stop_campaign_action(campaign_id: int) -> str | None:
    if campaign_id not in _running_tasks:
        return "Кампания не запущена."
    _stop_events[campaign_id].set()
    try:
        await asyncio.wait_for(_running_tasks[campaign_id], timeout=30)
    except asyncio.TimeoutError:
        _running_tasks[campaign_id].cancel()
    return None


# ══════════════════════════════════════════════
# Мидлварь: только администратор
# ══════════════════════════════════════════════


class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id
        if user_id is None or user_id != ADMIN_USER_ID:
            return
        return await handler(event, data)


# ══════════════════════════════════════════════
# Состояния FSM
# ══════════════════════════════════════════════


class AddAccountStates(StatesGroup):
    api_id = State()
    api_hash = State()
    phone = State()
    code = State()
    password = State()


class CreateCampaignStates(StatesGroup):
    name = State()
    accounts = State()
    text = State()
    interval = State()
    recipients = State()
    confirm = State()


class EditCampaignStates(StatesGroup):
    field = State()
    value = State()


class LoadRecipientsStates(StatesGroup):
    campaign_id = State()
    file = State()


# ══════════════════════════════════════════════
# Обработчики команд
# ══════════════════════════════════════════════

router = Router()
_pending_clients: dict[int, dict] = {}

STATUS_MAP = {
    "draft": "черновик",
    "active": "активна",
    "paused": "на паузе",
    "finished": "завершена",
}


def status_ru(status: str) -> str:
    return STATUS_MAP.get(status, status)


# --- Главное меню ---


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Аккаунты", callback_data="menu:accounts"
                ),
                InlineKeyboardButton(
                    text="Рассылки", callback_data="menu:campaigns"
                ),
            ],
            [InlineKeyboardButton(text="Помощь", callback_data="menu:help")],
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Добро пожаловать в бот рассылки!\n\n"
        "Управляй аккаунтами и рассылками отсюда.",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(lambda c: c.data == "menu:help")
async def cb_help(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Команды:\n"
        "/add_account - Добавить аккаунт Telegram\n"
        "/list_accounts - Список аккаунтов\n"
        "/remove_account <id> - Удалить аккаунт\n"
        "/create_campaign - Создать рассылку\n"
        "/list_campaigns - Список рассылок\n"
        "/start_campaign <id> - Запустить рассылку\n"
        "/stop_campaign <id> - Остановить рассылку\n"
        "/campaign_stats <id> - Статистика рассылки\n"
        "/export_logs <id> - Экспорт логов в CSV\n"
        "/edit_campaign <id> - Редактировать рассылку\n"
        "/load_recipients <id> - Загрузить получателей из файла\n"
        "/cancel - Отменить текущее действие",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Главное меню", reply_markup=main_menu_kb())
    await callback.answer()


# --- Меню аккаунтов ---


@router.callback_query(lambda c: c.data == "menu:accounts")
async def cb_accounts_menu(callback: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Добавить аккаунт", callback_data="acc:add"
                ),
                InlineKeyboardButton(
                    text="Список аккаунтов", callback_data="acc:list"
                ),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
        ]
    )
    await callback.message.edit_text("Управление аккаунтами", reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data == "acc:add")
async def cb_add_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text(
        "Введи api_id (получить на https://my.telegram.org):"
    )
    await state.set_state(AddAccountStates.api_id)
    await callback.answer()


@router.message(Command("add_account"))
async def cmd_add_account(message: Message, state: FSMContext) -> None:
    await message.answer("Введи api_id (получить на https://my.telegram.org):")
    await state.set_state(AddAccountStates.api_id)


@router.message(AddAccountStates.api_id)
async def process_api_id(message: Message, state: FSMContext) -> None:
    try:
        api_id = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("api_id должен быть числом. Попробуй ещё раз:")
        return
    await state.update_data(api_id=api_id)
    await message.answer("Введи api_hash:")
    await state.set_state(AddAccountStates.api_hash)


@router.message(AddAccountStates.api_hash)
async def process_api_hash(message: Message, state: FSMContext) -> None:
    await state.update_data(api_hash=message.text.strip())
    await message.answer("Введи номер телефона (например +79001234567):")
    await state.set_state(AddAccountStates.phone)


@router.message(AddAccountStates.phone)
async def process_phone(message: Message, state: FSMContext) -> None:
    phone = message.text.strip()
    await state.update_data(phone=phone)
    data = await state.get_data()

    client = TelegramClient(StringSession(), data["api_id"], data["api_hash"])
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        _pending_clients[message.from_user.id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "api_id": data["api_id"],
            "api_hash": data["api_hash"],
        }
        await message.answer("Код отправлен. Введи код подтверждения:")
        await state.set_state(AddAccountStates.code)
    except Exception as exc:
        logger.error("Ошибка отправки кода: %s", exc)
        await message.answer(f"Ошибка отправки кода: {exc}")
        await client.disconnect()
        await state.clear()


@router.message(AddAccountStates.code)
async def process_code(message: Message, state: FSMContext) -> None:
    info = _pending_clients.get(message.from_user.id)
    if not info:
        await message.answer("Сессия истекла. Начни заново /add_account")
        await state.clear()
        return

    client: TelegramClient = info["client"]
    code = message.text.strip()

    try:
        await client.sign_in(
            info["phone"], code, phone_code_hash=info["phone_code_hash"]
        )
    except Exception as exc:
        error_name = type(exc).__name__
        error_str = str(exc).lower()
        if "SessionPasswordNeeded" in error_name or "password" in error_str:
            await message.answer("Включена двухфакторная аутентификация. Введи пароль:")
            await state.set_state(AddAccountStates.password)
            return
        if "expired" in error_str or "PhoneCodeExpired" in error_name:
            try:
                sent = await client.send_code_request(info["phone"])
                info["phone_code_hash"] = sent.phone_code_hash
                await message.answer(
                    "Код истёк. Новый код отправлен.\n"
                    "Введи код быстрее (действует ~2 минуты):"
                )
                return
            except Exception as resend_err:
                logger.error("Ошибка повторной отправки кода: %s", resend_err)
                await message.answer(f"Не удалось отправить новый код: {resend_err}")
                await client.disconnect()
                _pending_clients.pop(message.from_user.id, None)
                await state.clear()
                return
        logger.error("Ошибка входа: %s", exc)
        await message.answer(f"Ошибка входа: {exc}")
        await client.disconnect()
        _pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return

    session_string = client.session.save()
    await client.disconnect()
    _pending_clients.pop(message.from_user.id, None)

    account = await add_account_to_db(
        info["api_id"], info["api_hash"], info["phone"], session_string
    )
    await message.answer(
        f"Аккаунт добавлен! (id={account.id}, телефон={account.phone})"
    )
    await state.clear()


@router.message(AddAccountStates.password)
async def process_password(message: Message, state: FSMContext) -> None:
    info = _pending_clients.get(message.from_user.id)
    if not info:
        await message.answer("Сессия истекла. Начни заново /add_account")
        await state.clear()
        return

    client: TelegramClient = info["client"]
    try:
        await client.sign_in(password=message.text.strip())
    except Exception as exc:
        logger.error("Ошибка 2FA: %s", exc)
        await message.answer(f"Ошибка 2FA: {exc}")
        await client.disconnect()
        _pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return

    session_string = client.session.save()
    await client.disconnect()
    _pending_clients.pop(message.from_user.id, None)

    account = await add_account_to_db(
        info["api_id"], info["api_hash"], info["phone"], session_string
    )
    await message.answer(
        f"Аккаунт добавлен! (id={account.id}, телефон={account.phone})"
    )
    await state.clear()


# --- Список / удаление аккаунтов ---


@router.message(Command("list_accounts"))
async def cmd_list_accounts(message: Message) -> None:
    await _show_accounts(message)


@router.callback_query(lambda c: c.data == "acc:list")
async def cb_list_accounts(callback: CallbackQuery) -> None:
    await _show_accounts(callback.message, edit=True)
    await callback.answer()


async def _show_accounts(message: Message, edit: bool = False) -> None:
    accounts = await list_accounts_db()
    if not accounts:
        text = "Аккаунтов пока нет."
    else:
        lines = []
        for a in accounts:
            status = "активен" if a.is_active else "неактивен"
            lines.append(f"[{a.id}] {a.phone} -- {status}")
        text = "Аккаунты:\n" + "\n".join(lines)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="menu:accounts")]
        ]
    )
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.message(Command("remove_account"))
async def cmd_remove_account(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /remove_account <id>")
        return
    try:
        account_id = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить", callback_data=f"acc:rm:{account_id}"
                ),
                InlineKeyboardButton(text="Отмена", callback_data="menu:accounts"),
            ],
        ]
    )
    await message.answer(f"Удалить аккаунт {account_id}?", reply_markup=kb)


@router.callback_query(F.data.startswith("acc:rm:"))
async def cb_confirm_remove(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[2])
    removed = await remove_account_db(account_id)
    if removed:
        await callback.message.edit_text(f"Аккаунт {account_id} удален.")
    else:
        await callback.message.edit_text("Аккаунт не найден.")
    await callback.answer()


# --- Меню рассылок ---


@router.callback_query(lambda c: c.data == "menu:campaigns")
async def cb_campaigns_menu(callback: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создать рассылку", callback_data="cmp:create"
                ),
                InlineKeyboardButton(
                    text="Список рассылок", callback_data="cmp:list"
                ),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
        ]
    )
    await callback.message.edit_text("Управление рассылками", reply_markup=kb)
    await callback.answer()


# --- Создание рассылки ---


@router.callback_query(lambda c: c.data == "cmp:create")
async def cb_create_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Введи название рассылки:")
    await state.set_state(CreateCampaignStates.name)
    await callback.answer()


@router.message(Command("create_campaign"))
async def cmd_create_campaign(message: Message, state: FSMContext) -> None:
    await message.answer("Введи название рассылки:")
    await state.set_state(CreateCampaignStates.name)


@router.message(CreateCampaignStates.name)
async def process_campaign_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    accounts = await list_accounts_db()
    if not accounts:
        await message.answer("Нет доступных аккаунтов. Сначала добавь аккаунт.")
        await state.clear()
        return
    await state.update_data(selected_accounts=[])
    kb = _build_account_select_kb(accounts, [])
    await message.answer(
        "Выбери аккаунты для этой рассылки (нажимай для выбора):", reply_markup=kb
    )
    await state.set_state(CreateCampaignStates.accounts)


def _build_account_select_kb(
    accounts: list, selected: list[int]
) -> InlineKeyboardMarkup:
    rows = []
    for a in accounts:
        mark = "[x]" if a.id in selected else "[ ]"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {a.phone}", callback_data=f"cmp:sel:{a.id}"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Готово", callback_data="cmp:sel:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(CreateCampaignStates.accounts, F.data.startswith("cmp:sel:"))
async def cb_select_account(callback: CallbackQuery, state: FSMContext) -> None:
    part = callback.data.split(":")[2]
    if part == "done":
        data = await state.get_data()
        selected = data.get("selected_accounts", [])
        if not selected:
            await callback.answer("Выбери хотя бы один аккаунт.")
            return
        await callback.message.edit_text(
            "Отправь сообщение для рассылки.\n\n"
            "Можно:\n"
            "- просто текст\n"
            "- фото с подписью (фото + текст в одном сообщении)\n"
            "- премиум-эмодзи, жирный, курсив"
        )
        await state.set_state(CreateCampaignStates.text)
        await callback.answer()
        return

    account_id = int(part)
    data = await state.get_data()
    selected = data.get("selected_accounts", [])
    if account_id in selected:
        selected.remove(account_id)
    else:
        selected.append(account_id)
    await state.update_data(selected_accounts=selected)
    accounts = await list_accounts_db()
    kb = _build_account_select_kb(accounts, selected)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


@router.message(CreateCampaignStates.text)
async def process_campaign_text(message: Message, state: FSMContext) -> None:
    text = message.text or message.caption
    entities = message.entities or message.caption_entities
    if not text:
        await message.answer(
            "Сообщение не содержит текста. Отправь текст или фото с подписью:"
        )
        return
    photo_file_id: str | None = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    await state.update_data(
        text=text,
        text_entities=serialize_entities(entities),
        photo_file_id=photo_file_id,
    )
    await message.answer(
        f"Введи интервал между сообщениями в секундах "
        f"(минимум {MIN_INTERVAL_SECONDS}):"
    )
    await state.set_state(CreateCampaignStates.interval)


@router.message(CreateCampaignStates.interval)
async def process_campaign_interval(message: Message, state: FSMContext) -> None:
    try:
        interval = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Должно быть число. Попробуй ещё раз:")
        return
    if interval < MIN_INTERVAL_SECONDS:
        await message.answer(
            f"Минимальный интервал {MIN_INTERVAL_SECONDS} сек. Попробуй ещё раз:"
        )
        return
    await state.update_data(interval=interval)
    await message.answer(
        "Введи получателей (юзернеймы или user ID).\n\n"
        "Поддерживаемые форматы:\n"
        "- через запятую: user1, user2, user3\n"
        "- по одному на строку\n"
        "- с @: @user1 @user2\n"
        "- ссылки: https://t.me/username\n"
        "- или отправь .txt файл с юзернеймами"
    )
    await state.set_state(CreateCampaignStates.recipients)


@router.message(CreateCampaignStates.recipients)
async def process_campaign_recipients(message: Message, state: FSMContext) -> None:
    targets: list[str] = []

    if message.document:
        fname = message.document.file_name or ""
        if fname.endswith(".txt") or fname.endswith(".csv"):
            file = await message.bot.download(message.document)
            if file:
                content = file.read().decode("utf-8", errors="replace")
                targets = parse_recipients(content)
        else:
            await message.answer(
                "Неподдерживаемый тип файла. Отправь .txt или .csv файл, "
                "или введи юзернеймы текстом."
            )
            return
    elif message.text:
        targets = parse_recipients(message.text)

    if not targets:
        await message.answer("Не найдено ни одного получателя. Попробуй ещё раз:")
        return

    await state.update_data(targets=targets)
    data = await state.get_data()

    has_photo = "да" if data.get("photo_file_id") else "нет"
    preview = (
        f"Рассылка: {data['name']}\n"
        f"Аккаунтов: {len(data['selected_accounts'])}\n"
        f"Интервал: {data['interval']} сек.\n"
        f"Получателей: {len(targets)}\n"
        f"Фото: {has_photo}\n\n"
        f"Текст сообщения:\n{data.get('text') or '(не задан)'}"
    )

    if len(targets) <= 20:
        preview += "\n\nСписок получателей:\n" + ", ".join(targets)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить", callback_data="cmp:confirm"
                ),
                InlineKeyboardButton(text="Отмена", callback_data="cmp:cancel"),
            ],
        ]
    )
    await message.answer(preview, reply_markup=kb)
    await state.set_state(CreateCampaignStates.confirm)


@router.callback_query(CreateCampaignStates.confirm, F.data == "cmp:confirm")
async def cb_confirm_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    campaign = await create_campaign_db(
        name=data["name"],
        text=data["text"],
        text_entities=data.get("text_entities"),
        photo_file_id=data.get("photo_file_id"),
        interval_seconds=data["interval"],
        account_ids=data["selected_accounts"],
        targets=data["targets"],
    )
    await callback.message.edit_text(
        f"Рассылка создана (id={campaign.id}).\n"
        f"Для запуска используй /start_campaign {campaign.id}"
    )
    await state.clear()
    await callback.answer()


@router.callback_query(CreateCampaignStates.confirm, F.data == "cmp:cancel")
async def cb_cancel_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Создание рассылки отменено.")
    await state.clear()
    await callback.answer()


# --- Список рассылок ---


@router.message(Command("list_campaigns"))
async def cmd_list_campaigns(message: Message) -> None:
    await _show_campaigns(message)


@router.callback_query(lambda c: c.data == "cmp:list")
async def cb_list_campaigns(callback: CallbackQuery) -> None:
    await _show_campaigns(callback.message, edit=True)
    await callback.answer()


async def _show_campaigns(message: Message, edit: bool = False) -> None:
    campaigns = await list_campaigns_db()
    if not campaigns:
        text = "Рассылок пока нет."
    else:
        lines = [
            f"[{c.id}] {c.name} -- {status_ru(c.status)}" for c in campaigns
        ]
        text = "Рассылки:\n" + "\n".join(lines)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="menu:campaigns")]
        ]
    )
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


# --- Запуск / остановка / статистика ---


@router.message(Command("start_campaign"))
async def cmd_start_campaign(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /start_campaign <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    campaign = await get_campaign_db(cid)
    if not campaign:
        await message.answer("Рассылка не найдена.")
        return

    stats = await campaign_stats_db(cid)
    preview = (
        f'Запустить рассылку [{cid}] "{campaign.name}"?\n'
        f"Получателей: {stats['total']} (ожидают: {stats['pending']})\n\n"
        f"Текст сообщения:\n{campaign.text}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Запустить", callback_data=f"cmp:start:{cid}"
                ),
                InlineKeyboardButton(text="Отмена", callback_data="menu:campaigns"),
            ],
        ]
    )
    await message.answer(preview, reply_markup=kb)


@router.callback_query(F.data.startswith("cmp:start:"))
async def cb_start_campaign(callback: CallbackQuery) -> None:
    cid = int(callback.data.split(":")[2])
    err = await start_campaign_action(cid)
    if err:
        await callback.message.edit_text(f"Ошибка: {err}")
    else:
        await callback.message.edit_text(f"Рассылка {cid} запущена!")
    await callback.answer()


@router.message(Command("stop_campaign"))
async def cmd_stop_campaign(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /stop_campaign <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
    err = await stop_campaign_action(cid)
    if err:
        await message.answer(f"Ошибка: {err}")
    else:
        await message.answer(f"Рассылка {cid} остановлена.")


@router.message(Command("campaign_stats"))
async def cmd_campaign_stats(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /campaign_stats <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    campaign = await get_campaign_db(cid)
    if not campaign:
        await message.answer("Рассылка не найдена.")
        return

    stats = await campaign_stats_db(cid)
    text = (
        f'Рассылка [{cid}] "{campaign.name}" ({status_ru(campaign.status)})\n\n'
        f"Всего: {stats['total']}\n"
        f"Отправлено: {stats['sent']}\n"
        f"Ошибок: {stats['errors']}\n"
        f"Дублей: {stats['duplicates']}\n"
        f"Ожидают: {stats['pending']}\n"
    )
    if stats["recent"]:
        text += "\nПоследние отправки:\n"
        for r in stats["recent"]:
            text += f"  {r['target']} в {r['sent_at']}\n"
    await message.answer(text)


# --- Экспорт логов ---


@router.message(Command("export_logs"))
async def cmd_export_logs(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /export_logs <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    rows = await export_campaign_logs_db(cid)
    if not rows:
        await message.answer("Нет логов для этой рассылки.")
        return

    csv_text = export_logs_csv(rows)
    doc = BufferedInputFile(
        csv_text.encode("utf-8"), filename=f"rassilka_{cid}_logi.csv"
    )
    await message.answer_document(doc, caption=f"Логи рассылки {cid}")


# --- Редактирование рассылки ---


@router.message(Command("edit_campaign"))
async def cmd_edit_campaign(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /edit_campaign <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    campaign = await get_campaign_db(cid)
    if not campaign:
        await message.answer("Рассылка не найдена.")
        return
    if campaign.status not in ("draft", "paused"):
        await message.answer(
            "Редактировать можно только черновики или приостановленные рассылки."
        )
        return

    await state.update_data(edit_campaign_id=cid)
    has_photo = "да" if campaign.photo_file_id else "нет"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Изменить текст", callback_data="cmp:edit:text"
                ),
                InlineKeyboardButton(
                    text="Изменить интервал", callback_data="cmp:edit:interval"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Изменить фото", callback_data="cmp:edit:photo"
                ),
                InlineKeyboardButton(
                    text="Убрать фото", callback_data="cmp:edit:rmphoto"
                ),
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="menu:campaigns")],
        ]
    )
    await message.answer(
        f'Редактирование рассылки [{cid}] "{campaign.name}".\n'
        f"Текущий интервал: {campaign.interval_seconds} сек.\n"
        f"Фото: {has_photo}\n"
        f"Текущий текст:\n{campaign.text}",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("cmp:edit:"))
async def cb_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":")[2]
    if field == "rmphoto":
        data = await state.get_data()
        cid = data["edit_campaign_id"]
        ok = await update_campaign_db(cid, photo_file_id=None)
        if ok:
            await callback.message.edit_text("Фото удалено из рассылки.")
        else:
            await callback.message.edit_text("Не удалось обновить.")
        await state.clear()
        await callback.answer()
        return
    await state.update_data(edit_field=field)
    if field == "text":
        await callback.message.edit_text(
            "Отправь новое сообщение (текст или фото с подписью):"
        )
    elif field == "photo":
        await callback.message.edit_text("Отправь новое фото:")
    else:
        await callback.message.edit_text(
            f"Введи новый интервал в секундах (мин. {MIN_INTERVAL_SECONDS}):"
        )
    await state.set_state(EditCampaignStates.value)
    await callback.answer()


@router.message(EditCampaignStates.value)
async def process_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cid = data["edit_campaign_id"]
    field = data["edit_field"]

    if field == "interval":
        try:
            val = int(message.text.strip())
        except (ValueError, AttributeError):
            await message.answer("Должно быть число.")
            return
        if val < MIN_INTERVAL_SECONDS:
            await message.answer(f"Минимум {MIN_INTERVAL_SECONDS} сек.")
            return
        ok = await update_campaign_db(cid, interval_seconds=val)
    elif field == "photo":
        if not message.photo:
            await message.answer("Отправь фото. Попробуй ещё раз:")
            return
        photo_fid = message.photo[-1].file_id
        ok = await update_campaign_db(cid, photo_file_id=photo_fid)
    else:
        text = message.text or message.caption
        entities = message.entities or message.caption_entities
        if not text:
            await message.answer("Сообщение не содержит текста. Попробуй ещё раз:")
            return
        ents = serialize_entities(entities)
        photo_fid = message.photo[-1].file_id if message.photo else None
        ok = await update_campaign_db(cid, text=text, text_entities=ents, photo_file_id=photo_fid)

    if ok:
        await message.answer(f"Рассылка {cid} обновлена.")
    else:
        await message.answer("Не удалось обновить (рассылка может быть активна).")
    await state.clear()


# --- Загрузка получателей из файла ---


@router.message(Command("load_recipients"))
async def cmd_load_recipients(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /load_recipients <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    campaign = await get_campaign_db(cid)
    if not campaign:
        await message.answer("Рассылка не найдена.")
        return

    await state.update_data(load_campaign_id=cid)
    await message.answer(
        f'Отправь .txt файл с юзернеймами для рассылки [{cid}] "{campaign.name}".\n\n'
        "Поддерживаемые форматы:\n"
        "- по одному юзернейму на строку\n"
        "- с @: @user1\n"
        "- ссылки: https://t.me/username\n"
        "- через запятую или точку с запятой\n"
        "- или просто введи текстом"
    )
    await state.set_state(LoadRecipientsStates.file)


@router.message(LoadRecipientsStates.file)
async def process_load_recipients_file(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cid = data["load_campaign_id"]
    targets: list[str] = []

    if message.document:
        fname = message.document.file_name or ""
        if fname.endswith(".txt") or fname.endswith(".csv"):
            file = await message.bot.download(message.document)
            if file:
                content = file.read().decode("utf-8", errors="replace")
                targets = parse_recipients(content)
        else:
            await message.answer(
                "Отправь .txt или .csv файл, или введи юзернеймы текстом."
            )
            return
    elif message.text:
        targets = parse_recipients(message.text)

    if not targets:
        await message.answer(
            "Не найдено ни одного получателя. Попробуй ещё раз или /cancel"
        )
        return

    added = await add_recipients_to_campaign(cid, targets)
    await message.answer(
        f"Распознано юзернеймов: {len(targets)}\n"
        f"Добавлено новых: {added}\n"
        f"(дубли пропущены)"
    )
    await state.clear()


# --- Отмена ---


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_kb())


# ══════════════════════════════════════════════
# Запуск бота
# ══════════════════════════════════════════════


async def main() -> None:
    logger.info("Инициализация базы данных...")
    await init_db()

    global _bot_instance
    bot = Bot(token=BOT_TOKEN)
    _bot_instance = bot
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(AdminOnlyMiddleware())
    dp.callback_query.middleware(AdminOnlyMiddleware())

    dp.include_router(router)

    logger.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
