"""
P2P Exchange — Telegram Mini App Backend
Единый файл: FastAPI + aiogram 3 + SQLite + admin broadcast

Запуск:
  pip install aiogram fastapi uvicorn aiosqlite python-dotenv pydantic
  BOT_TOKEN=... ADMIN_IDS=123,456 WEBAPP_URL=https://... python backend.py
"""

import os
import json
import hmac
import hashlib
import asyncio
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, unquote
from contextlib import asynccontextmanager

import aiosqlite
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

from fastapi import FastAPI, APIRouter, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, URLInputFile
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.com")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DATABASE_PATH = os.getenv("DATABASE_PATH", "p2p_exchanger.db")
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

WELCOME_PHOTO_URL = "https://i.postimg.cc/pV9xPWd4/novyj-i-ulucsennyj.jpg"

CRYPTO_CURRENCIES = ["BTC", "ETH", "USDT", "GRAM", "STARS"]
FIAT_CURRENCIES = ["USD", "EUR", "RUB", "UAH", "MDL", "TRY", "CNY", "GBP"]
ALL_CURRENCIES = CRYPTO_CURRENCIES + FIAT_CURRENCIES

# ═══════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════

_db: Optional[aiosqlite.Connection] = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DATABASE_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
    return _db


async def init_db():
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            avatar_url TEXT,
            language TEXT DEFAULT 'en',
            is_admin BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'Beginner',
            uid INTEGER DEFAULT 0,
            total_deals INTEGER DEFAULT 0,
            successful_deals INTEGER DEFAULT 0,
            activity_pct INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            currency TEXT NOT NULL,
            balance REAL DEFAULT 0.0,
            frozen REAL DEFAULT 0.0,
            UNIQUE(user_id, currency)
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            side TEXT NOT NULL,
            crypto_currency TEXT NOT NULL,
            fiat_currency TEXT NOT NULL,
            price REAL NOT NULL,
            amount REAL NOT NULL,
            min_limit REAL,
            max_limit REAL,
            payment_method TEXT,
            terms TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER REFERENCES orders(id),
            buyer_id INTEGER REFERENCES users(id),
            seller_id INTEGER REFERENCES users(id),
            crypto_currency TEXT NOT NULL,
            fiat_currency TEXT NOT NULL,
            price REAL NOT NULL,
            crypto_amount REAL NOT NULL,
            fiat_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            admin_paid BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP,
            completed_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER REFERENCES deals(id),
            sender_id INTEGER REFERENCES users(id),
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER REFERENCES users(id),
            message_text TEXT NOT NULL,
            total_users INTEGER DEFAULT 0,
            sent_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS saved_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            wallet_type TEXT NOT NULL,
            address TEXT NOT NULL,
            UNIQUE(user_id, wallet_type)
        );
    """)
    await db.commit()


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


# ═══════════════════════════════════════════
# DB MODELS (queries)
# ═══════════════════════════════════════════

def generate_uid(telegram_id: int) -> int:
    tid = abs(telegram_id)
    return ((tid * 2654435761) & 0xFFFFFFFF) % 90000000 + 10000000


async def create_user(telegram_id, username=None, first_name=None, language="en", avatar_url=None):
    db = await get_db()
    uid = generate_uid(telegram_id)
    await db.execute(
        """INSERT INTO users (telegram_id, username, first_name, language, avatar_url, uid)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(telegram_id) DO UPDATE SET
             username=excluded.username, first_name=excluded.first_name, avatar_url=excluded.avatar_url""",
        (telegram_id, username, first_name, language, avatar_url, uid))
    await db.commit()
    return await get_user_by_tg(telegram_id)


async def get_user_by_tg(telegram_id):
    db = await get_db()
    cur = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_id(uid):
    db = await get_db()
    cur = await db.execute("SELECT * FROM users WHERE id = ?", (uid,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_user_language(uid, lang):
    db = await get_db()
    await db.execute("UPDATE users SET language = ? WHERE id = ?", (lang, uid))
    await db.commit()


async def get_all_users():
    db = await get_db()
    cur = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
    return [dict(r) for r in await cur.fetchall()]


async def get_or_create_wallet(uid, currency):
    db = await get_db()
    cur = await db.execute("SELECT * FROM wallets WHERE user_id=? AND currency=?", (uid, currency))
    row = await cur.fetchone()
    if row:
        return dict(row)
    await db.execute("INSERT INTO wallets (user_id, currency) VALUES (?, ?)", (uid, currency))
    await db.commit()
    cur = await db.execute("SELECT * FROM wallets WHERE user_id=? AND currency=?", (uid, currency))
    return dict(await cur.fetchone())


async def get_all_balances(uid):
    result = []
    for c in ALL_CURRENCIES:
        w = await get_or_create_wallet(uid, c)
        result.append({"currency": c, "balance": w["balance"], "frozen": w["frozen"]})
    return result


async def get_balance(uid, currency):
    w = await get_or_create_wallet(uid, currency)
    return {"currency": currency, "balance": w["balance"], "frozen": w["frozen"]}


async def update_balance(uid, currency, amount):
    await get_or_create_wallet(uid, currency)
    db = await get_db()
    await db.execute("UPDATE wallets SET balance=balance+? WHERE user_id=? AND currency=?",
                     (amount, uid, currency))
    await db.commit()


async def freeze_balance(uid, currency, amount):
    await get_or_create_wallet(uid, currency)
    db = await get_db()
    await db.execute("UPDATE wallets SET balance=balance-?, frozen=frozen+? WHERE user_id=? AND currency=?",
                     (amount, amount, uid, currency))
    await db.commit()


async def unfreeze_balance(uid, currency, amount):
    db = await get_db()
    await db.execute("UPDATE wallets SET balance=balance+?, frozen=frozen-? WHERE user_id=? AND currency=?",
                     (amount, amount, uid, currency))
    await db.commit()


async def release_frozen(uid, currency, amount):
    db = await get_db()
    await db.execute("UPDATE wallets SET frozen=frozen-? WHERE user_id=? AND currency=?",
                     (amount, uid, currency))
    await db.commit()


async def db_create_order(uid, side, crypto, fiat, price, amount, min_l=None, max_l=None, payment=None, terms=None):
    db = await get_db()
    cur = await db.execute(
        """INSERT INTO orders (user_id, side, crypto_currency, fiat_currency, price, amount,
                               min_limit, max_limit, payment_method, terms) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (uid, side, crypto, fiat, price, amount, min_l, max_l, payment, terms))
    await db.commit()
    return await db_get_order(cur.lastrowid)


async def db_get_order(oid):
    db = await get_db()
    cur = await db.execute(
        "SELECT o.*, u.username, u.first_name, u.avatar_url FROM orders o JOIN users u ON o.user_id=u.id WHERE o.id=?",
        (oid,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def db_get_active_orders(side=None, crypto=None, fiat=None):
    db = await get_db()
    q = "SELECT o.*, u.username, u.first_name, u.avatar_url FROM orders o JOIN users u ON o.user_id=u.id WHERE o.status='active'"
    p = []
    if side:
        q += " AND o.side=?"
        p.append(side)
    if crypto:
        q += " AND o.crypto_currency=?"
        p.append(crypto)
    if fiat:
        q += " AND o.fiat_currency=?"
        p.append(fiat)
    q += " ORDER BY o.created_at DESC"
    cur = await db.execute(q, p)
    return [dict(r) for r in await cur.fetchall()]


async def db_get_user_orders(uid):
    db = await get_db()
    cur = await db.execute(
        "SELECT o.*, u.username, u.first_name FROM orders o JOIN users u ON o.user_id=u.id WHERE o.user_id=? ORDER BY o.created_at DESC",
        (uid,))
    return [dict(r) for r in await cur.fetchall()]


async def db_cancel_order(oid, uid):
    db = await get_db()
    cur = await db.execute("UPDATE orders SET status='cancelled' WHERE id=? AND user_id=? AND status='active'",
                           (oid, uid))
    await db.commit()
    return cur.rowcount > 0


async def db_create_deal(order_id, buyer_id, seller_id, crypto, fiat, price, c_amount, f_amount):
    db = await get_db()
    cur = await db.execute(
        """INSERT INTO deals (order_id, buyer_id, seller_id, crypto_currency, fiat_currency,
                              price, crypto_amount, fiat_amount) VALUES (?,?,?,?,?,?,?,?)""",
        (order_id, buyer_id, seller_id, crypto, fiat, price, c_amount, f_amount))
    await db.commit()
    return await db_get_deal(cur.lastrowid)


async def db_get_deal(did):
    db = await get_db()
    cur = await db.execute("SELECT * FROM deals WHERE id=?", (did,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def db_get_user_deals(uid):
    db = await get_db()
    cur = await db.execute("SELECT * FROM deals WHERE buyer_id=? OR seller_id=? ORDER BY created_at DESC",
                           (uid, uid))
    return [dict(r) for r in await cur.fetchall()]


async def db_update_deal_status(did, status, admin_paid=False):
    db = await get_db()
    now = datetime.utcnow().isoformat()
    extra, p = "", [status]
    if status == "paid":
        extra, p = ", paid_at=?", [status, now]
    elif status == "completed":
        extra, p = ", completed_at=?", [status, now]
    if admin_paid:
        extra += ", admin_paid=TRUE"
    p.append(did)
    await db.execute(f"UPDATE deals SET status=?{extra} WHERE id=?", p)
    await db.commit()


async def db_get_all_deals():
    db = await get_db()
    cur = await db.execute("SELECT * FROM deals ORDER BY created_at DESC")
    return [dict(r) for r in await cur.fetchall()]


async def db_create_message(deal_id, sender_id, text):
    db = await get_db()
    cur = await db.execute("INSERT INTO messages (deal_id, sender_id, text) VALUES (?,?,?)",
                           (deal_id, sender_id, text))
    await db.commit()
    c2 = await db.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,))
    return dict(await c2.fetchone())


async def db_get_deal_messages(deal_id):
    db = await get_db()
    cur = await db.execute(
        "SELECT m.*, u.username, u.first_name FROM messages m JOIN users u ON m.sender_id=u.id WHERE m.deal_id=? ORDER BY m.created_at ASC",
        (deal_id,))
    return [dict(r) for r in await cur.fetchall()]


async def db_create_broadcast(admin_id, text, total):
    db = await get_db()
    cur = await db.execute("INSERT INTO broadcasts (admin_id, message_text, total_users) VALUES (?,?,?)",
                           (admin_id, text, total))
    await db.commit()
    c2 = await db.execute("SELECT * FROM broadcasts WHERE id=?", (cur.lastrowid,))
    return dict(await c2.fetchone())


async def db_update_broadcast(bid, sent, failed, status):
    db = await get_db()
    completed = datetime.utcnow().isoformat() if status in ("completed", "failed") else None
    await db.execute("UPDATE broadcasts SET sent_count=?, failed_count=?, status=?, completed_at=? WHERE id=?",
                     (sent, failed, status, completed, bid))
    await db.commit()


async def db_get_broadcasts():
    db = await get_db()
    cur = await db.execute("SELECT * FROM broadcasts ORDER BY created_at DESC LIMIT 50")
    return [dict(r) for r in await cur.fetchall()]


# ═══════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════

INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))


def validate_init_data(init_data: str) -> dict:
    parsed = parse_qs(init_data)
    check_hash = parsed.get("hash", [None])[0]
    if not check_hash:
        raise HTTPException(401, "Missing hash")

    auth_date_str = parsed.get("auth_date", [None])[0]
    if not auth_date_str:
        raise HTTPException(401, "Missing auth_date")
    try:
        auth_date = int(auth_date_str)
    except (ValueError, TypeError):
        raise HTTPException(401, "Invalid auth_date")
    now = int(datetime.utcnow().timestamp())
    if now - auth_date > INIT_DATA_MAX_AGE:
        raise HTTPException(401, "Init data expired (replay attack protection)")

    pairs = []
    for key, values in sorted(parsed.items()):
        if key == "hash":
            continue
        pairs.append(f"{key}={unquote(values[0])}")
    data_check_string = "\n".join(pairs)
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, check_hash):
        raise HTTPException(401, "Invalid init data")
    user_data = parsed.get("user", [None])[0]
    if user_data:
        return json.loads(unquote(user_data))
    raise HTTPException(401, "No user data")


async def get_current_user(request: Request) -> dict:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        raise HTTPException(401, "Missing init data header")
    tg_user = validate_init_data(init_data)
    user = await get_user_by_tg(tg_user["id"])
    if not user:
        raise HTTPException(401, "User not found")
    return user


async def require_admin(request: Request) -> dict:
    user = await get_current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    return user


# ═══════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════

class LanguageUpdate(BaseModel):
    language: str

class OrderCreate(BaseModel):
    side: str
    crypto_currency: str
    fiat_currency: str
    price: float
    amount: float
    min_limit: Optional[float] = None
    max_limit: Optional[float] = None
    payment_method: Optional[str] = None
    terms: Optional[str] = None

    @field_validator("side")
    @classmethod
    def validate_side(cls, v):
        if v not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        return v

    @field_validator("price", "amount")
    @classmethod
    def validate_positive(cls, v):
        if v <= 0:
            raise ValueError("Value must be positive")
        return v

    @field_validator("crypto_currency")
    @classmethod
    def validate_crypto(cls, v):
        allowed = {"BTC", "ETH", "USDT", "GRAM", "TON", "STARS", "BNB", "SOL"}
        if v.upper() not in allowed:
            raise ValueError(f"Unsupported crypto: {v}")
        return v.upper()

class DealCreate(BaseModel):
    order_id: int
    crypto_amount: float

    @field_validator("crypto_amount")
    @classmethod
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError("Amount must be positive")
        return v

class MessageCreate(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, v):
        v = v.strip()
        if not v or len(v) > 4096:
            raise ValueError("Message must be 1-4096 characters")
        return v

class BroadcastMessage(BaseModel):
    message: str
    parse_mode: Optional[str] = "HTML"


# ═══════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════

api = APIRouter(prefix="/api")


@api.get("/user/me")
async def api_get_me(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        raise HTTPException(401, "Missing init data header")
    tg_user = validate_init_data(init_data)
    user = await get_user_by_tg(tg_user["id"])
    if not user:
        lang = tg_user.get("language_code", "en")
        if lang not in ("en", "ru"):
            lang = "en"
        user = await create_user(
            tg_user["id"],
            tg_user.get("username"),
            tg_user.get("first_name"),
            lang
        )
    return user


@api.put("/user/language")
async def api_update_language(request: Request, body: LanguageUpdate):
    user = await get_current_user(request)
    if body.language not in ("en", "ru"):
        raise HTTPException(400, "Unsupported language")
    await update_user_language(user["id"], body.language)
    return {"ok": True}


@api.get("/wallet/balances")
async def api_balances(request: Request):
    user = await get_current_user(request)
    return {"balances": await get_all_balances(user["id"])}


@api.get("/wallet/balance/{currency}")
async def api_balance(request: Request, currency: str):
    user = await get_current_user(request)
    return await get_balance(user["id"], currency.upper())


@api.get("/orders")
async def api_list_orders(request: Request, side: str = None, crypto: str = None, fiat: str = None):
    await get_current_user(request)
    return {"orders": await db_get_active_orders(side, crypto, fiat)}


@api.post("/orders")
async def api_create_order(request: Request, body: OrderCreate):
    user = await get_current_user(request)
    if body.side == "sell":
        bal = await get_balance(user["id"], body.crypto_currency)
        if bal["balance"] < body.amount:
            raise HTTPException(400, "Insufficient balance")
        await freeze_balance(user["id"], body.crypto_currency, body.amount)
    return await db_create_order(
        user["id"], body.side, body.crypto_currency, body.fiat_currency,
        body.price, body.amount, body.min_limit, body.max_limit,
        body.payment_method, body.terms)


@api.delete("/orders/{order_id}")
async def api_cancel_order(request: Request, order_id: int):
    user = await get_current_user(request)
    order = await db_get_order(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if order["user_id"] != user["id"]:
        raise HTTPException(403, "Not your order")
    if order["side"] == "sell" and order["status"] == "active":
        await unfreeze_balance(user["id"], order["crypto_currency"], order["amount"])
    if not await db_cancel_order(order_id, user["id"]):
        raise HTTPException(400, "Cannot cancel")
    return {"ok": True}


@api.get("/orders/my")
async def api_my_orders(request: Request):
    user = await get_current_user(request)
    return {"orders": await db_get_user_orders(user["id"])}


@api.post("/deals")
async def api_create_deal(request: Request, body: DealCreate):
    user = await get_current_user(request)
    order = await db_get_order(body.order_id)
    if not order or order["status"] != "active":
        raise HTTPException(404, "Order not found or inactive")
    fiat_amount = body.crypto_amount * order["price"]
    if order["min_limit"] and fiat_amount < order["min_limit"]:
        raise HTTPException(400, "Below minimum limit")
    if order["max_limit"] and fiat_amount > order["max_limit"]:
        raise HTTPException(400, "Above maximum limit")
    if body.crypto_amount > order["amount"]:
        raise HTTPException(400, "Amount exceeds available")
    if order["side"] == "sell":
        buyer_id, seller_id = user["id"], order["user_id"]
    else:
        seller_id, buyer_id = user["id"], order["user_id"]
        bal = await get_balance(seller_id, order["crypto_currency"])
        if bal["balance"] < body.crypto_amount:
            raise HTTPException(400, "Seller insufficient balance")
        await freeze_balance(seller_id, order["crypto_currency"], body.crypto_amount)
    if buyer_id == seller_id:
        raise HTTPException(400, "Cannot trade with yourself")
    deal = await db_create_deal(body.order_id, buyer_id, seller_id,
                                order["crypto_currency"], order["fiat_currency"],
                                order["price"], body.crypto_amount, fiat_amount)
    buyer = await get_user_by_id(buyer_id)
    seller = await get_user_by_id(seller_id)
    await log_deal_created(deal, buyer, seller)
    return deal


@api.get("/deals/{deal_id}")
async def api_get_deal(request: Request, deal_id: int):
    user = await get_current_user(request)
    deal = await db_get_deal(deal_id)
    if not deal:
        raise HTTPException(404, "Deal not found")
    if deal["buyer_id"] != user["id"] and deal["seller_id"] != user["id"] and not user.get("is_admin"):
        raise HTTPException(403, "Access denied")
    return deal


@api.put("/deals/{deal_id}/pay")
async def api_mark_paid(request: Request, deal_id: int):
    user = await get_current_user(request)
    deal = await db_get_deal(deal_id)
    if not deal:
        raise HTTPException(404)
    if deal["buyer_id"] != user["id"]:
        raise HTTPException(403, "Only buyer can mark as paid")
    if deal["status"] != "pending":
        raise HTTPException(400, "Deal is not pending")
    await db_update_deal_status(deal_id, "paid")
    updated_deal = await db_get_deal(deal_id)
    await log_deal_payment(updated_deal, user)
    return {"ok": True, "status": "paid"}


@api.put("/deals/{deal_id}/confirm")
async def api_confirm_deal(request: Request, deal_id: int):
    user = await get_current_user(request)
    deal = await db_get_deal(deal_id)
    if not deal:
        raise HTTPException(404)
    if deal["seller_id"] != user["id"]:
        raise HTTPException(403, "Only seller can confirm")
    if deal["status"] != "paid":
        raise HTTPException(400, "Not marked as paid")
    await release_frozen(deal["seller_id"], deal["crypto_currency"], deal["crypto_amount"])
    await update_balance(deal["buyer_id"], deal["crypto_currency"], deal["crypto_amount"])
    await db_update_deal_status(deal_id, "completed")
    updated_deal = await db_get_deal(deal_id)
    await log_deal_completed(updated_deal, user)
    return {"ok": True, "status": "completed"}


@api.put("/deals/{deal_id}/cancel")
async def api_cancel_deal(request: Request, deal_id: int):
    user = await get_current_user(request)
    deal = await db_get_deal(deal_id)
    if not deal:
        raise HTTPException(404)
    if deal["buyer_id"] != user["id"] and deal["seller_id"] != user["id"]:
        raise HTTPException(403)
    if deal["status"] != "pending":
        raise HTTPException(400, "Cannot cancel at this stage")
    await unfreeze_balance(deal["seller_id"], deal["crypto_currency"], deal["crypto_amount"])
    await db_update_deal_status(deal_id, "cancelled")
    return {"ok": True, "status": "cancelled"}


@api.get("/users/by-uid/{uid}")
async def api_user_by_uid(request: Request, uid: int):
    await get_current_user(request)
    target = await get_user_by_uid_val(uid)
    if not target:
        raise HTTPException(404, "User not found")
    return {
        "id": target["id"],
        "uid": target["uid"],
        "username": target.get("username", ""),
        "first_name": target.get("first_name", ""),
        "status": target.get("status", "Beginner"),
    }


class WalletSave(BaseModel):
    wallet_type: str
    address: str

    @field_validator("wallet_type")
    @classmethod
    def validate_type(cls, v):
        allowed = {"TON", "BTC", "ETH", "BNB", "SOL"}
        if v.upper() not in allowed:
            raise ValueError(f"Unsupported wallet type: {v}")
        return v.upper()

    @field_validator("address")
    @classmethod
    def validate_address(cls, v):
        v = v.strip()
        if not v or len(v) > 256:
            raise ValueError("Invalid address")
        return v


@api.get("/wallets")
async def api_get_wallets(request: Request):
    user = await get_current_user(request)
    db = await get_db()
    cur = await db.execute(
        "SELECT wallet_type, address FROM saved_wallets WHERE user_id=?", (user["id"],))
    rows = await cur.fetchall()
    return {r["wallet_type"]: r["address"] for r in rows}


@api.post("/wallets")
async def api_save_wallet(request: Request, body: WalletSave):
    user = await get_current_user(request)
    db = await get_db()
    await db.execute(
        "INSERT INTO saved_wallets (user_id, wallet_type, address) VALUES (?,?,?) "
        "ON CONFLICT(user_id, wallet_type) DO UPDATE SET address=excluded.address",
        (user["id"], body.wallet_type, body.address))
    await db.commit()
    return {"ok": True}


@api.delete("/wallets/{wallet_type}")
async def api_delete_wallet(request: Request, wallet_type: str):
    user = await get_current_user(request)
    wt = wallet_type.upper()
    db = await get_db()
    await db.execute("DELETE FROM saved_wallets WHERE user_id=? AND wallet_type=?", (user["id"], wt))
    await db.commit()
    return {"ok": True}


@api.get("/deals/my/list")
async def api_my_deals(request: Request):
    user = await get_current_user(request)
    return {"deals": await db_get_user_deals(user["id"])}


@api.get("/deals/{deal_id}/messages")
async def api_get_messages(request: Request, deal_id: int):
    user = await get_current_user(request)
    deal = await db_get_deal(deal_id)
    if not deal:
        raise HTTPException(404)
    if deal["buyer_id"] != user["id"] and deal["seller_id"] != user["id"] and not user.get("is_admin"):
        raise HTTPException(403)
    return {"messages": await db_get_deal_messages(deal_id)}


@api.post("/deals/{deal_id}/messages")
async def api_send_message(request: Request, deal_id: int, body: MessageCreate):
    user = await get_current_user(request)
    deal = await db_get_deal(deal_id)
    if not deal:
        raise HTTPException(404)
    if deal["buyer_id"] != user["id"] and deal["seller_id"] != user["id"]:
        raise HTTPException(403)
    if not body.text.strip():
        raise HTTPException(400, "Empty message")
    return await db_create_message(deal_id, user["id"], body.text.strip())


# ─── Admin Routes ───

admin_api = APIRouter(prefix="/api/admin")


@admin_api.get("/deals")
async def adm_all_deals(request: Request):
    await require_admin(request)
    return {"deals": await db_get_all_deals()}


@admin_api.put("/deals/{deal_id}/pay")
async def adm_silent_pay(request: Request, deal_id: int):
    """Тихая оплата — НЕ отправляет уведомления участникам!"""
    await require_admin(request)
    deal = await db_get_deal(deal_id)
    if not deal:
        raise HTTPException(404)
    if deal["status"] != "pending":
        raise HTTPException(400, "Deal is not pending")
    await db_update_deal_status(deal_id, "paid", admin_paid=True)
    return {"ok": True, "status": "paid", "admin_paid": True}


@admin_api.put("/deals/{deal_id}/complete")
async def adm_force_complete(request: Request, deal_id: int):
    await require_admin(request)
    deal = await db_get_deal(deal_id)
    if not deal:
        raise HTTPException(404)
    if deal["status"] not in ("pending", "paid"):
        raise HTTPException(400, "Cannot complete this deal")
    await release_frozen(deal["seller_id"], deal["crypto_currency"], deal["crypto_amount"])
    await update_balance(deal["buyer_id"], deal["crypto_currency"], deal["crypto_amount"])
    await db_update_deal_status(deal_id, "completed")
    return {"ok": True, "status": "completed"}


@admin_api.get("/users")
async def adm_list_users(request: Request):
    await require_admin(request)
    users = await get_all_users()
    return {"users": users, "total": len(users)}


@admin_api.post("/broadcast")
async def adm_broadcast(request: Request, body: BroadcastMessage):
    """Рассылка сообщения всем пользователям через бота"""
    admin_user = await require_admin(request)
    if not body.message.strip():
        raise HTTPException(400, "Empty message")
    users = await get_all_users()
    if not users:
        return {"ok": True, "sent": 0, "failed": 0, "total": 0}
    broadcast = await db_create_broadcast(admin_user["id"], body.message, len(users))
    sent, failed = 0, 0
    for u in users:
        try:
            await bot.send_message(chat_id=u["telegram_id"], text=body.message, parse_mode=body.parse_mode)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await db_update_broadcast(broadcast["id"], sent, failed, "completed")
    return {"ok": True, "broadcast_id": broadcast["id"], "total": len(users), "sent": sent, "failed": failed}


@admin_api.get("/broadcasts")
async def adm_broadcast_history(request: Request):
    await require_admin(request)
    return {"broadcasts": await db_get_broadcasts()}


# ═══════════════════════════════════════════
# TELEGRAM BOT (aiogram 3)
# ═══════════════════════════════════════════

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
bot_router = Router()


@bot_router.message(Command("start"))
async def cmd_start(message: types.Message):
    tg = message.from_user
    avatar_url = None
    try:
        photos = await bot.get_user_profile_photos(tg.id, limit=1)
        if photos.total_count > 0:
            file = await bot.get_file(photos.photos[0][-1].file_id)
            avatar_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    except Exception:
        pass
    await create_user(tg.id, tg.username, tg.first_name, tg.language_code or "en", avatar_url)
    if tg.id in ADMIN_IDS:
        db = await get_db()
        await db.execute("UPDATE users SET is_admin=TRUE WHERE telegram_id=?", (tg.id,))
        await db.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Market", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    caption = (
        f"👋 <b>Добро пожаловать, {tg.first_name}!</b>\n\n"
        "<blockquote>P2P Exchange — безопасная платформа для обмена "
        "криптовалют напрямую между пользователями с эскроу-защитой</blockquote>\n\n"
        "💰 <b>Доступные активы:</b>\n"
        "├ Bitcoin (BTC)\n"
        "├ Ethereum (ETH)\n"
        "├ Tether (USDT)\n"
        "├ Gram / TON\n"
        "├ BNB\n"
        "├ Solana (SOL)\n"
        "└ Telegram Stars ⭐\n\n"
        "🛡 <b>Безопасность:</b>\n"
        "<blockquote>Каждая сделка защищена 4-этапной эскроу-системой. "
        "Средства блокируются до подтверждения обеими сторонами.</blockquote>"
    )
    try:
        photo = URLInputFile(WELCOME_PHOTO_URL)
        await message.answer_photo(photo=photo, caption=caption, reply_markup=kb)
    except Exception:
        await message.answer(caption, reply_markup=kb)


@bot_router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа к админ-панели.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Все сделки", callback_data="adm_deals")],
        [InlineKeyboardButton(text="👥 Все пользователи", callback_data="adm_users")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_bc")],
        [InlineKeyboardButton(text="📋 История рассылок", callback_data="adm_bc_hist")],
    ])
    await message.answer("🔧 <b>Админ-панель</b>\n\nВыберите действие:", reply_markup=kb)


@bot_router.callback_query(F.data == "adm_deals")
async def cb_adm_deals(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    deals = await db_get_all_deals()
    if not deals:
        await cb.message.answer("Сделок пока нет.")
        return await cb.answer()
    txt = "📊 <b>Последние сделки:</b>\n\n"
    for d in deals[:10]:
        txt += f"#{d['id']} | {d['crypto_currency']}/{d['fiat_currency']} | {d['crypto_amount']} @ {d['price']} | {d['status']}\n"
    await cb.message.answer(txt)
    await cb.answer()


@bot_router.callback_query(F.data == "adm_users")
async def cb_adm_users(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    users = await get_all_users()
    txt = f"👥 <b>Пользователи ({len(users)}):</b>\n\n"
    for u in users[:20]:
        badge = " 👑" if u.get("is_admin") else ""
        txt += f"• {u.get('first_name', 'N/A')} (@{u.get('username', 'N/A')}) — ID: {u['telegram_id']}{badge}\n"
    await cb.message.answer(txt)
    await cb.answer()


@bot_router.callback_query(F.data == "adm_bc")
async def cb_adm_bc(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.answer(
        "📢 <b>Рассылка</b>\n\nОтправьте текст сообщения для рассылки:\n"
        "<code>/broadcast Ваше сообщение</code>")
    await cb.answer()


@bot_router.callback_query(F.data == "adm_bc_hist")
async def cb_adm_bc_hist(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    bcs = await db_get_broadcasts()
    if not bcs:
        await cb.message.answer("История рассылок пуста.")
        return await cb.answer()
    txt = "📋 <b>История рассылок:</b>\n\n"
    for b in bcs[:10]:
        txt += f"#{b['id']} | ✅ {b['sent_count']}/{b['total_users']} | ❌ {b['failed_count']} | {b['status']}\n"
    await cb.message.answer(txt)
    await cb.answer()


@bot_router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("⛔ У вас нет доступа.")
    text = message.text.replace("/broadcast", "", 1).strip()
    if not text:
        return await message.answer("❌ Укажите текст: <code>/broadcast Ваше сообщение</code>")
    users = await get_all_users()
    if not users:
        return await message.answer("❌ Нет пользователей для рассылки.")
    status_msg = await message.answer(f"📢 Начинаю рассылку для <b>{len(users)}</b> пользователей...")
    admin_user = await get_user_by_tg(message.from_user.id)
    bc = await db_create_broadcast(admin_user["id"], text, len(users))
    sent, failed = 0, 0
    for u in users:
        try:
            await bot.send_message(chat_id=u["telegram_id"], text=text, parse_mode=ParseMode.HTML)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await db_update_broadcast(bc["id"], sent, failed, "completed")
    await status_msg.edit_text(f"✅ <b>Рассылка завершена!</b>\n\n📨 Отправлено: {sent}/{len(users)}\n❌ Ошибок: {failed}")


async def send_log(text: str, reply_markup=None):
    if not LOG_CHANNEL_ID:
        return
    try:
        await bot.send_message(LOG_CHANNEL_ID, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Failed to send log: {e}")


async def log_deal_created(deal, buyer, seller):
    text = (
        "📋 <b>Новая сделка создана</b>\n\n"
        f"🆔 Сделка: #{deal['id']}\n"
        f"👤 Покупатель: {buyer.get('first_name','')} (@{buyer.get('username','—')}) | UID: {buyer.get('uid',0)}\n"
        f"👤 Продавец: {seller.get('first_name','')} (@{seller.get('username','—')}) | UID: {seller.get('uid',0)}\n"
        f"💰 {deal['crypto_amount']} {deal['crypto_currency']} → {deal['fiat_amount']} {deal['fiat_currency']}\n"
        f"📊 Цена: {deal['price']} {deal['fiat_currency']}\n"
        f"🏷 Статус покупателя: {buyer.get('status','Beginner')}\n"
        f"🏷 Статус продавца: {seller.get('status','Beginner')}\n"
        f"📅 {deal.get('created_at','')}"
    )
    await send_log(text)


async def log_deal_payment(deal, payer):
    text = (
        "💳 <b>Оплата подтверждена</b>\n\n"
        f"🆔 Сделка: #{deal['id']}\n"
        f"👤 Оплатил: {payer.get('first_name','')} (@{payer.get('username','—')}) | UID: {payer.get('uid',0)}\n"
        f"💰 {deal['crypto_amount']} {deal['crypto_currency']} → {deal['fiat_amount']} {deal['fiat_currency']}\n"
        f"📅 {deal.get('paid_at','')}"
    )
    await send_log(text)


async def log_deal_completed(deal, confirmer):
    text = (
        "✅ <b>Сделка завершена</b>\n\n"
        f"🆔 Сделка: #{deal['id']}\n"
        f"👤 Подтвердил: {confirmer.get('first_name','')} (@{confirmer.get('username','—')}) | UID: {confirmer.get('uid',0)}\n"
        f"💰 {deal['crypto_amount']} {deal['crypto_currency']} → {deal['fiat_amount']} {deal['fiat_currency']}\n"
        f"📅 {deal.get('completed_at','')}"
    )
    await send_log(text)


# ─── /ggz — Admin request ───

@bot_router.message(Command("ggz"))
async def cmd_ggz(message: types.Message):
    tg = message.from_user
    user_db = await get_user_by_tg(tg.id)
    if not user_db:
        await create_user(tg.id, tg.username, tg.first_name)
        user_db = await get_user_by_tg(tg.id)
    if user_db.get("is_admin"):
        return await message.answer("✅ Вы уже являетесь администратором.")
    if not LOG_CHANNEL_ID:
        return await message.answer("❌ Лог-канал не настроен.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"adm_req_accept:{tg.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_req_reject:{tg.id}")
        ]
    ])
    await bot.send_message(
        LOG_CHANNEL_ID,
        f"🔐 <b>Заявка на админ-права</b>\n\n"
        f"👤 Пользователь: {tg.first_name} (@{tg.username or '—'})\n"
        f"🆔 Telegram ID: <code>{tg.id}</code>\n"
        f"🔑 UID: <code>{user_db.get('uid', 0)}</code>\n\n"
        f"Ожидание решения...",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )
    await message.answer("📨 Ваша заявка на получение админ-прав отправлена. Ожидайте решения.")


@bot_router.callback_query(F.data.startswith("adm_req_accept:"))
async def cb_adm_req_accept(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    target_tg_id = int(cb.data.split(":")[1])
    db = await get_db()
    await db.execute("UPDATE users SET is_admin=TRUE WHERE telegram_id=?", (target_tg_id,))
    await db.commit()
    ADMIN_IDS.append(target_tg_id)
    await cb.message.edit_text(
        cb.message.text + f"\n\n✅ <b>Принято</b> администратором @{cb.from_user.username or cb.from_user.first_name}",
        parse_mode=ParseMode.HTML
    )
    try:
        await bot.send_message(target_tg_id, "🎉 Ваша заявка на админ-права <b>одобрена</b>!\n\nИспользуйте /panel для просмотра команд.", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await cb.answer("Заявка принята")


@bot_router.callback_query(F.data.startswith("adm_req_reject:"))
async def cb_adm_req_reject(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    target_tg_id = int(cb.data.split(":")[1])
    await cb.message.edit_text(
        cb.message.text + f"\n\n❌ <b>Отклонено</b> администратором @{cb.from_user.username or cb.from_user.first_name}",
        parse_mode=ParseMode.HTML
    )
    try:
        await bot.send_message(target_tg_id, "❌ Ваша заявка на админ-права <b>отклонена</b>.", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await cb.answer("Заявка отклонена")


# ─── /panel — Admin command list ───

@bot_router.message(Command("panel"))
async def cmd_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("⛔ У вас нет доступа к админ-панели.")
    await message.answer(
        "🔧 <b>Админ-панель — Команды</b>\n\n"
        "📊 <b>Статистика пользователя:</b>\n"
        "<code>/setdeals [UID] [кол-во]</code> — установить кол-во сделок\n"
        "<code>/setactivity [UID] [%]</code> — установить % активности\n"
        "<code>/settotaldeals [UID] [кол-во]</code> — общее кол-во сделок\n"
        "<code>/setsuccessful [UID] [кол-во]</code> — успешные сделки\n"
        "<code>/setstatus [UID] [Merchant/Beginner]</code> — статус пользователя\n\n"
        "👥 <b>Управление:</b>\n"
        "<code>/admin</code> — панель управления\n"
        "<code>/broadcast [текст]</code> — рассылка всем\n\n"
        "📋 <b>Логи:</b>\n"
        "Все сделки, оплаты и подтверждения отправляются в лог-канал автоматически.",
        parse_mode=ParseMode.HTML
    )


# ─── Admin stat commands ───

async def get_user_by_uid_val(uid_val: int):
    db = await get_db()
    cur = await db.execute("SELECT * FROM users WHERE uid=?", (uid_val,))
    row = await cur.fetchone()
    return dict(row) if row else None


@bot_router.message(Command("setdeals"))
async def cmd_setdeals(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("❌ Формат: <code>/setdeals [UID] [кол-во]</code>", parse_mode=ParseMode.HTML)
    uid_val, count = int(parts[1]), int(parts[2])
    target = await get_user_by_uid_val(uid_val)
    if not target:
        return await message.answer(f"❌ Пользователь с UID {uid_val} не найден.")
    db = await get_db()
    await db.execute("UPDATE users SET total_deals=? WHERE id=?", (count, target["id"]))
    await db.commit()
    await message.answer(f"✅ Установлено сделок: <b>{count}</b> для {target['first_name']} (UID: {uid_val})", parse_mode=ParseMode.HTML)


@bot_router.message(Command("setactivity"))
async def cmd_setactivity(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("❌ Формат: <code>/setactivity [UID] [%]</code>", parse_mode=ParseMode.HTML)
    uid_val, pct = int(parts[1]), int(parts[2])
    target = await get_user_by_uid_val(uid_val)
    if not target:
        return await message.answer(f"❌ Пользователь с UID {uid_val} не найден.")
    db = await get_db()
    await db.execute("UPDATE users SET activity_pct=? WHERE id=?", (pct, target["id"]))
    await db.commit()
    await message.answer(f"✅ Активность: <b>{pct}%</b> для {target['first_name']} (UID: {uid_val})", parse_mode=ParseMode.HTML)


@bot_router.message(Command("settotaldeals"))
async def cmd_settotaldeals(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("❌ Формат: <code>/settotaldeals [UID] [кол-во]</code>", parse_mode=ParseMode.HTML)
    uid_val, count = int(parts[1]), int(parts[2])
    target = await get_user_by_uid_val(uid_val)
    if not target:
        return await message.answer(f"❌ Пользователь с UID {uid_val} не найден.")
    db = await get_db()
    await db.execute("UPDATE users SET total_deals=? WHERE id=?", (count, target["id"]))
    await db.commit()
    await message.answer(f"✅ Общее кол-во сделок: <b>{count}</b> для {target['first_name']} (UID: {uid_val})", parse_mode=ParseMode.HTML)


@bot_router.message(Command("setsuccessful"))
async def cmd_setsuccessful(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("❌ Формат: <code>/setsuccessful [UID] [кол-во]</code>", parse_mode=ParseMode.HTML)
    uid_val, count = int(parts[1]), int(parts[2])
    target = await get_user_by_uid_val(uid_val)
    if not target:
        return await message.answer(f"❌ Пользователь с UID {uid_val} не найден.")
    db = await get_db()
    await db.execute("UPDATE users SET successful_deals=? WHERE id=?", (count, target["id"]))
    await db.commit()
    await message.answer(f"✅ Успешных сделок: <b>{count}</b> для {target['first_name']} (UID: {uid_val})", parse_mode=ParseMode.HTML)


@bot_router.message(Command("setstatus"))
async def cmd_setstatus(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("❌ Формат: <code>/setstatus [UID] [Merchant/Beginner]</code>", parse_mode=ParseMode.HTML)
    uid_val = int(parts[1])
    status = parts[2]
    if status not in ("Merchant", "Beginner"):
        return await message.answer("❌ Статус может быть только <b>Merchant</b> или <b>Beginner</b>.", parse_mode=ParseMode.HTML)
    target = await get_user_by_uid_val(uid_val)
    if not target:
        return await message.answer(f"❌ Пользователь с UID {uid_val} не найден.")
    db = await get_db()
    await db.execute("UPDATE users SET status=? WHERE id=?", (status, target["id"]))
    await db.commit()
    await message.answer(f"✅ Статус <b>{status}</b> установлен для {target['first_name']} (UID: {uid_val})", parse_mode=ParseMode.HTML)


dp.include_router(bot_router)


# ═══════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════

@asynccontextmanager
async def lifespan(application: FastAPI):
    await init_db()
    logger.info("Database initialized")
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}{WEBHOOK_PATH}")
        logger.info(f"Webhook set: {WEBHOOK_URL}{WEBHOOK_PATH}")
    else:
        asyncio.create_task(dp.start_polling(bot))
        logger.info("Polling started")
    yield
    if WEBHOOK_URL:
        await bot.delete_webhook()
    await close_db()
    await bot.session.close()


app = FastAPI(title="P2P Exchange API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(api)
app.include_router(admin_api)


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    from aiogram.types import Update
    body = await request.json()
    update = Update(**body)
    await dp.feed_update(bot=bot, update=update)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


# Отдаём frontend.html по корневому URL
@app.get("/")
async def serve_frontend():
    if os.path.exists("frontend.html"):
        return FileResponse("frontend.html", media_type="text/html")
    return {"message": "Place frontend.html next to backend.py"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend:app", host=API_HOST, port=API_PORT, reload=True)
