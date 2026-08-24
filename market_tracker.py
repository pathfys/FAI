# -*- coding: utf-8 -*-
"""
market_tracker.py — трекер resale-маркета Telegram.

Что делает:
  • обходит каталог лимитированных подарков и собирает новые resale-листинги;
  • фильтрует продавцов по уровню и количеству NFT в профиле;
  • раскладывает лоты по веткам (Topics) супергруппы: чёрный фон / дорогие /
    дешёвые / все;
  • даёт кнопку «Занять лот» — первый нажавший получает лот в ЛС.

БД owners.db — ОБЩАЯ с parser_bot.py (лежит рядом с этим файлом).
Запуск: python market_tracker.py
"""
import json
import asyncio
import random
import re
import time
import collections
import os
import sqlite3

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from pyrogram import Client
from pyrogram.raw import functions
from pyrogram.errors import FloodWait

# ═══════════════════════════════════════════════════════════════════════════
#  КОНФИГ
# ═══════════════════════════════════════════════════════════════════════════
ADMIN_ID = 899438668
BOT_TOKEN = "8931945544:AAGbnXg7BCweHygNqS0AjBLr9HgOmE3DRp0"
API_ID = 32508082
API_HASH = "b5acbc0925f91f9e411f3397ec8b95a5"

# ─── СЕССИИ ДЛЯ ПУЛА (10 шт.) ────────────────────────────────────────────────
POOL_SESSIONS = [
    ("marketsessin1", "+14306007667"),
    ("marketsessin2", "+18455202836"),
    ("marketsessin3", "+17408408947"),
    ("marketsessin4", "+19795304811"),
    ("marketsessin5", "+12185776428"),
    ("marketsessin6", "+14754500869"),
    ("marketsessin7", "+16727680666"),
    ("marketsessin8", "+17407948366"),
    ("marketsessin9", "+17167092896"),
    ("marketsessin10", "+19144917876"),
]
POOL_CONCURRENCY_PER_SESSION = 2

# ─── ГРУППА С ВЕТКАМИ-ТЕМАМИ (Topics/форум) ──────────────────────────────────
GROUP_ID = -1003675750307          # супергруппа с включёнными «Темами»
TOPIC_THREADS = {
    "black": 2,   # ⚫ Чёрный фон
    "dear":  5,   # 💎 Дорогие подарки
    "cheap": 6,   # 🪙 Дешёвые гифты
    "all":   7,   # 🎁 Все подарки
}
MIRROR_ALL = True   # True → каждый лот дублируется в ветку «Все подарки» (7)

# ─── БД (общая с parser_bot.py) ──────────────────────────────────────────────
OWNERS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "owners.db")
GIFT_CACHE_TTL = 86400  # 24 часа


# ═══════════════════════════════════════════════════════════════════════════
#  БД (owners.db) — создаём ВСЕ таблицы, база общая с parser_bot.py
# ═══════════════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(OWNERS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS owners (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            first_name TEXT,
            added_at   REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gift_cache (
            user_id     INTEGER PRIMARY KEY,
            has_gifts   INTEGER,
            gift_count  INTEGER,
            checked_at  REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resale_listings (
            instance_id   INTEGER PRIMARY KEY,
            gift_id       INTEGER,
            title         TEXT,
            slug          TEXT,
            num           INTEGER,
            price_stars   INTEGER,
            price_ton     REAL,
            value_usd     INTEGER,
            offer_min     INTEGER,
            seller_id     INTEGER,
            seller_name   TEXT,
            posted_at     REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            invited_id       INTEGER PRIMARY KEY,
            invited_username TEXT,
            inviter_id       INTEGER,
            confirmed        INTEGER,
            joined_at        REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ref_progress (
            user_id      INTEGER PRIMARY KEY,
            level        INTEGER,
            levels_today INTEGER,
            last_day     TEXT
        )
    """)
    conn.execute("""
            CREATE TABLE IF NOT EXISTS founders (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                first_name   TEXT,
                founder_num  INTEGER,
                joined_at    REAL
            )
        """)
    conn.execute("""
            CREATE TABLE IF NOT EXISTS ref_credits (
                user_id  INTEGER PRIMARY KEY,
                credits  REAL DEFAULT 0
            )
        """)
    conn.execute("""
            CREATE TABLE IF NOT EXISTS claimed_lots (
                instance_id  INTEGER PRIMARY KEY,
                user_id      INTEGER,
                username     TEXT,
                first_name   TEXT,
                claimed_at   INTEGER
            )
        """)
    conn.commit()
    conn.close()


def db_add_owner(user_id: int, username: str, first_name: str):
    conn = sqlite3.connect(OWNERS_DB)
    conn.execute(
        "INSERT OR IGNORE INTO owners (user_id, username, first_name, added_at) VALUES (?, ?, ?, ?)",
        (user_id, username or "", first_name or "", time.time()),
    )
    conn.commit()
    conn.close()


def db_count_owners() -> int:
    conn = sqlite3.connect(OWNERS_DB)
    n = conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
    conn.close()
    return n


def db_get_random_owners(n: int) -> list[tuple]:
    """Возвращает [(user_id, username, first_name), ...] случайных владельцев."""
    conn = sqlite3.connect(OWNERS_DB)
    rows = conn.execute(
        "SELECT user_id, username, first_name FROM owners ORDER BY RANDOM() LIMIT ?", (n,)
    ).fetchall()
    conn.close()
    return rows


def db_get_cache(user_id: int):
    """Возвращает (has_gifts, gift_count) если кэш свежий, иначе None."""
    conn = sqlite3.connect(OWNERS_DB)
    row = conn.execute(
        "SELECT has_gifts, gift_count, checked_at FROM gift_cache WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    has_gifts, gift_count, checked_at = row
    if time.time() - checked_at > GIFT_CACHE_TTL:
        return None
    return has_gifts, gift_count


def db_set_cache(user_id: int, has_gifts: bool, gift_count: int):
    conn = sqlite3.connect(OWNERS_DB)
    conn.execute(
        "INSERT OR REPLACE INTO gift_cache (user_id, has_gifts, gift_count, checked_at) VALUES (?, ?, ?, ?)",
        (user_id, 1 if has_gifts else 0, gift_count, time.time()),
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
#  ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ═══════════════════════════════════════════════════════════════════════════
_PYRO_SEM = asyncio.Semaphore(20)      # 10 сессий × 2 параллельных вызова
_flood_until: float = 0.0
_USER_CACHE: dict[int, tuple[str, str]] = {}

# ─── ФИЛЬТР ПРОДАВЦОВ ────────────────────────────────────────────────────────
MAX_SELLER_LEVEL = 4         # не постить продавцов выше этого уровня
MIN_SELLER_GIFTS = 1         # минимум подарков в профиле
MAX_SELLER_GIFTS = 5         # максимум подарков в профиле
MAX_SELLER_GIFTS_BLACK = 9   # для чёрного фона мягче: NFT до 9
MAX_SELLER_LEVEL_DEAR = 8    # для DEAR_WHITELIST мягче: владельцы редких вещей часто выше уровнем
MAX_SELLER_GIFTS_DEAR = 15   # для DEAR_WHITELIST мягче: у коллекционеров обычно больше NFT

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
# ═══════════════════════════════════════════════════════════════════════════
#  ПУЛ СЕССИЙ PYROGRAM
# ═══════════════════════════════════════════════════════════════════════════
class _PooledSession:
    """Одна сессия пула: клиент + свой флуд-таймер + свой семафор."""
    def __init__(self, name: str, phone: str):
        self.name = name
        self.phone = phone
        self.client = Client(name, api_id=API_ID, api_hash=API_HASH,
                             phone_number=phone, sleep_threshold=2)
        self.sem = asyncio.Semaphore(POOL_CONCURRENCY_PER_SESSION)
        self.flood_until = 0.0
        self.inflight = 0
        self.started = False

    def banned(self) -> bool:
        return self.flood_until > time.monotonic()


class SessionProxy:
    """
    Подменяет одиночный pyro_client. Любой вызов (.invoke / .get_users /
    .resolve_peer) уходит на наименее загруженную НЕ забаненную сессию.
    FloodWait одной сессии не блокирует другие — у каждой свой таймер.
    """
    def __init__(self):
        self.sessions: list[_PooledSession] = []

    def __bool__(self):
        # чтобы `if not pyro_client:` работал как раньше
        return any(s.started for s in self.sessions)

    async def start_all(self) -> int:
        ok = 0
        for name, phone in POOL_SESSIONS:
            if not phone or "НОМЕР" in phone:
                print(f"[pool] сессия '{name}' пропущена — номер не задан")
                continue
            s = _PooledSession(name, phone)
            try:
                await s.client.start()
                s.started = True
                ok += 1
                me = await s.client.get_me()
                print(f"[pool] '{name}' запущена ({me.first_name} @{me.username})")
            except Exception as e:
                print(f"[pool] '{name}' НЕ запущена: {type(e).__name__}: {e}")
                continue
            self.sessions.append(s)
        return ok

    async def stop_all(self):
        for s in self.sessions:
            try:
                await s.client.stop()
            except Exception:
                pass

    def _pick(self) -> _PooledSession | None:
        live = [s for s in self.sessions if s.started]
        if not live:
            return None
        now = time.monotonic()
        free = [s for s in live if s.flood_until <= now]
        if free:
            return min(free, key=lambda s: s.inflight)
        # все в бане — берём ту, что раньше всех разбанится
        return min(live, key=lambda s: s.flood_until)

    async def _run(self, method_name: str, *args, **kwargs):
        """
        Выполняет client.<method_name>(*args) на лучшей сессии.
        FloodWait → ставим таймер этой сессии и пробуем другую.
        Резервируем inflight ДО await, чтобы параллельные вызовы не
        сваливались в одну и ту же сессию.
        """
        last_exc = None
        for _ in range(3):
            s = self._pick()
            if s is None:
                raise RuntimeError("[pool] нет живых сессий")
            # резервируем сессию сразу — следующий _pick() увидит нас загруженными
            s.inflight += 1
            try:
                wait = s.flood_until - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(min(wait, 5.0))
                async with s.sem:
                    method = getattr(s.client, method_name)
                    return await method(*args, **kwargs)
            except FloodWait as e:
                s.flood_until = time.monotonic() + e.value + 1
                last_exc = e
                if e.value >= 5:  # короткие FW не спамят лог
                    print(f"[pool] '{s.name}' FloodWait {e.value}s — переключаюсь")
                continue
            finally:
                s.inflight -= 1
        if last_exc:
            raise last_exc
        return None

    async def invoke(self, *args, **kwargs):
        return await self._run("invoke", *args, **kwargs)

    async def get_users(self, *args, **kwargs):
        return await self._run("get_users", *args, **kwargs)

    async def resolve_peer(self, *args, **kwargs):
        return await self._run("resolve_peer", *args, **kwargs)

    async def get_me(self, *args, **kwargs):
        return await self._run("get_me", *args, **kwargs)


pyro_client: SessionProxy | None = None

# ═══════════════════════════════════════════════════════════════════════════
#  NFT-ДАННЫЕ (тиражи, названия, whitelist дорогих)
# ═══════════════════════════════════════════════════════════════════════════
NFT_DATA = {
    "HeartLocket": 1950, "PlushPepe": 2825, "PreciousPeach": 3005,
    "HeroicHelmet": 3425, "MightyArm": 3818, "IonGem": 4535,
    "PerfumeBottle": 4422, "DurovsCap": 4710, "NailBracelet": 4691,
    "MagicPotion": 4871, "MiniOscar": 4877, "AstralShard": 5674,
    "GemSignet": 6214, "ArtisanBrick": 6291, "GenieLamp": 6567,
    "BondedRing": 7836, "SharpTongue": 8136, "ElectricSkull": 9224,
    "BlingBinky": 9385, "WestsideSign": 11505, "RareBird": 12723,
    "KhabibsPapakha": 28625, "LootBag": 14430, "KissedFrog": 14080,
    "NekoHelmet": 15432, "SignetRing": 16885, "ScaredCat": 19229,
    "MadPumpkin": 19701, "IonicDryer": 20527, "SkullFlower": 21941,
    "SkyStilettos": 51835, "SleighBell": 22723, "LowRider": 23454,
    "LoveCandle": 24243, "FlyingBroom": 24801, "CrystalBall": 26753,
    "TrappedHeart": 25040, "RecordPlayer": 34626, "DiamondRing": 32260,
    "CupidCharm": 30249, "EternalRose": 31748, "VintageCigar": 26624,
    "VoodooDoll": 26810, "SwissWatch": 25923, "LovePotion": 29130,
    "ToyBear": 56088, "TopHat": 34277, "UFCStrike": 58734,
    "ValentineBox": 35206, "BerryBox": 52254, "BowTie": 56028,
    "BunnyMuffin": 53168, "HexPot": 56423, "SnowGlobe": 54363,
    "SnowMittens": 39837, "EternalCandle": 45158, "HangingStar": 46387,
    "MoneyPot": 73208, "EvilEye": 74135, "BigYear": 76751,
    "SantaHat": 70575, "StarNotepad": 72137, "WinterWreath": 75829,
    "HypnoLollipop": 82544, "JoyfulBundle": 82765, "WitchHat": 82685,
    "SpyAgaric": 86834, "HolidayDrink": 87542, "SakuraFlower": 87888,
    "JackInTheBox": 90643, "JingleBells": 90618, "MoonPendant": 102439,
    "RestlessJar": 102171, "SnoopCigar": 117502, "JollyChimp": 117286,
    "LushBouquet": 109276, "SpicedWine": 106033, "TamaGadget": 106146,
    "JellyBunny": 106061, "VictoryMedal": 104614, "PrettyPosy": 128475,
    "InputKey": 133006, "StellarRocket": 137471, "FaithAmulet": 140224,
    "JesterHat": 140855, "GingerCookie": 148850, "FreshSocks": 160538,
    "EasterEgg": 163182, "MousseCake": 174175, "HomemadeCake": 179744,
    "MoodPack": 179835, "CloverPin": 180944, "SpringBasket": 181269,
    "SnakeBox": 182382, "PartySparkler": 185134, "PetSnake": 186169,
    "CookieHeart": 195392, "LunarSnake": 197508, "PoolFloat": 219995,
    "XmasStocking": 222514, "CandyCane": 222346, "HappyBrownie": 224036,
    "SwagBag": 231881, "BDayCandle": 264770, "WhipCupcake": 276105,
    "IceCream": 327738, "DeskCalendar": 341244, "InstantRamen": 384498,
    "ViceCream": 413556, "ChillFlame": 429439, "LolPop": 433806,
    "SnoopDogg": 581407, "LightSword": 124924, "TimelessBook": 85506,
    "KittyMedallion": 80000,
}

GIFT_NAMES = {
    "HeartLocket": "Heart Locket", "PlushPepe": "Plush Pepe", "PreciousPeach": "Precious Peach",
    "HeroicHelmet": "Heroic Helmet", "MightyArm": "Mighty Arm", "IonGem": "Ion Gem",
    "PerfumeBottle": "Perfume Bottle", "DurovsCap": "Durov's Cap", "NailBracelet": "Nail Bracelet",
    "MagicPotion": "Magic Potion", "MiniOscar": "Mini Oscar", "AstralShard": "Astral Shard",
    "GemSignet": "Gem Signet", "ArtisanBrick": "Artisan Brick", "GenieLamp": "Genie Lamp",
    "BondedRing": "Bonded Ring", "SharpTongue": "Sharp Tongue", "ElectricSkull": "Electric Skull",
    "BlingBinky": "Bling Binky", "WestsideSign": "Westside Sign", "RareBird": "Rare Bird",
    "KhabibsPapakha": "Khabib's Papakha", "LootBag": "Loot Bag", "KissedFrog": "Kissed Frog",
    "NekoHelmet": "Neko Helmet", "SignetRing": "Signet Ring", "ScaredCat": "Scared Cat",
    "MadPumpkin": "Mad Pumpkin", "IonicDryer": "Ionic Dryer", "SkullFlower": "Skull Flower",
    "SkyStilettos": "Sky Stilettos", "SleighBell": "Sleigh Bell", "LowRider": "Low Rider",
    "LoveCandle": "Love Candle", "FlyingBroom": "Flying Broom", "CrystalBall": "Crystal Ball",
    "TrappedHeart": "Trapped Heart", "RecordPlayer": "Record Player", "DiamondRing": "Diamond Ring",
    "CupidCharm": "Cupid Charm", "EternalRose": "Eternal Rose", "VintageCigar": "Vintage Cigar",
    "VoodooDoll": "Voodoo Doll", "SwissWatch": "Swiss Watch", "LovePotion": "Love Potion",
    "ToyBear": "Toy Bear", "TopHat": "Top Hat", "UFCStrike": "UFC Strike",
    "ValentineBox": "Valentine Box", "BerryBox": "Berry Box", "BowTie": "Bow Tie",
    "BunnyMuffin": "Bunny Muffin", "HexPot": "Hex Pot", "SnowGlobe": "Snow Globe",
    "SnowMittens": "Snow Mittens", "EternalCandle": "Eternal Candle", "HangingStar": "Hanging Star",
    "MoneyPot": "Money Pot", "EvilEye": "Evil Eye", "BigYear": "Big Year",
    "SantaHat": "Santa Hat", "StarNotepad": "Star Notepad", "WinterWreath": "Winter Wreath",
    "HypnoLollipop": "Hypno Lollipop", "JoyfulBundle": "Joyful Bundle", "WitchHat": "Witch Hat",
    "SpyAgaric": "Spy Agaric", "HolidayDrink": "Holiday Drink", "SakuraFlower": "Sakura Flower",
    "JackInTheBox": "Jack in the Box", "JingleBells": "Jingle Bells", "MoonPendant": "Moon Pendant",
    "RestlessJar": "Restless Jar", "SnoopCigar": "Snoop Cigar", "JollyChimp": "Jolly Chimp",
    "LushBouquet": "Lush Bouquet", "SpicedWine": "Spiced Wine", "TamaGadget": "Tama Gadget",
    "JellyBunny": "Jelly Bunny", "VictoryMedal": "Victory Medal", "PrettyPosy": "Pretty Posy",
    "InputKey": "Input Key", "StellarRocket": "Stellar Rocket", "FaithAmulet": "Faith Amulet",
    "JesterHat": "Jester Hat", "GingerCookie": "Ginger Cookie", "FreshSocks": "Fresh Socks",
    "EasterEgg": "Easter Egg", "MousseCake": "Mousse Cake", "HomemadeCake": "Homemade Cake",
    "MoodPack": "Mood Pack", "CloverPin": "Clover Pin", "SpringBasket": "Spring Basket",
    "SnakeBox": "Snake Box", "PartySparkler": "Party Sparkler", "PetSnake": "Pet Snake",
    "CookieHeart": "Cookie Heart", "LunarSnake": "Lunar Snake", "PoolFloat": "Pool Float",
    "XmasStocking": "Xmas Stocking", "CandyCane": "Candy Cane", "HappyBrownie": "Happy Brownie",
    "SwagBag": "Swag Bag", "BDayCandle": "B-day Candle", "WhipCupcake": "Whip Cupcake",
    "IceCream": "Ice Cream", "DeskCalendar": "Desk Calendar", "InstantRamen": "Instant Ramen",
    "ViceCream": "Vice Cream", "ChillFlame": "Chill Flame", "LolPop": "Lol Pop",
    "SnoopDogg": "Snoop Dogg", "LightSword": "Light Sword", "TimelessBook": "Timeless Book",
    "KittyMedallion": "Kitty Medallion",
}

# ─── СУПЛАЙ (тираж) → редкость/floor ──────────────────────────────────────────
SUPPLY_BY_TITLE = {GIFT_NAMES[k]: v for k, v in NFT_DATA.items() if k in GIFT_NAMES}

TOP_FLOOR_KEYS = {
    "PlushPepe", "DurovsCap", "PreciousPeach", "HeartLocket", "LootBag",
    "NekoHelmet", "AstralShard", "IonGem", "GenieLamp", "NailBracelet",
    "SignetRing", "VintageCigar", "SwissWatch", "PerfumeBottle", "DiamondRing",
    "MagicPotion", "ToyBear",
}
TOP_FLOOR_TITLES = {GIFT_NAMES[k] for k in TOP_FLOOR_KEYS if k in GIFT_NAMES}

# ─── СТРОГИЙ WHITELIST: ТОЛЬКО ЭТИ ПОДАРКИ ИДУТ В ВЕТКУ "ДОРОГИЕ" ────────────
DEAR_WHITELIST = {
    "Heart Locket", "Plush Pepe", "Precious Peach", "Heroic Helmet",
    "Mighty Arm", "Ion Gem", "Perfume Bottle", "Durov's Cap",
    "Nail Bracelet", "Magic Potion", "Mini Oscar", "Astral Shard",
    "Gem Signet", "Artisan Brick", "Genie Lamp", "Bonded Ring",
    "Sharp Tongue", "Electric Skull", "Bling Binky", "Westside Sign",
    "Rare Bird", "Khabib's Papakha", "Loot Bag", "Kissed Frog",
    "Neko Helmet", "Signet Ring", "Scared Cat", "Mad Pumpkin",
    "Ionic Dryer", "Skull Flower",
}


def gift_supply(title: str) -> int | None:
    """Тираж коллекции по названию из листинга. None — если не знаем."""
    return SUPPLY_BY_TITLE.get((title or "").strip())


def supply_rarity_label(supply: int | None) -> str:
    if supply is None:
        return "неизвестно"
    if supply < 10_000:
        return "очень редкая 💎"
    if supply < 50_000:
        return "редкая 🔥"
    if supply < 100_000:
        return "средняя"
    return "массовая (низкий floor)"


# ─── ФОНЫ ────────────────────────────────────────────────────────────────────
BG_LIST = [
    "Pink", "Pistachio", "Seal Brown", "Black", "Gunmetal", "Electric Purple",
    "Lavender", "Cyberpunk", "Electric Indigo", "Neon Blue", "Navy Blue",
    "Sapphire", "Sky Blue", "Azure Blue", "Pacific Cyan", "Aquamarine",
    "Pacific Green", "Emerald", "Mint Green", "Malachite", "Shamrock Green",
    "Lemongrass", "Light Olive", "Satin Gold", "Pure Gold", "Amber", "Caramel",
    "Orange", "Carrot Juice", "Coral Red", "Persimmon", "Strawberry", "Raspberry",
    "Mystic Pearl", "Fandango", "Dark Lilac", "English Violet", "Moonstone",
    "Pine Green", "Hunter Green", "Khaki Green", "Desert Sand", "Cappuccino",
    "Rosewood", "Ivory White", "Platinum", "Roman Silver", "Steel Grey",
    "Silver Blue", "Burgundy", "Indigo Dye", "Midnight Blue", "Onyx Black",
    "Battleship Grey", "Purple", "Grape", "Cobalt Blue", "French Blue",
    "Turquoise", "Jade Green", "Copper", "Chestnut", "Marine Blue",
    "Tactical Pine", "Gunship Green", "Dark Green", "Rifle Green",
    "Ranger Green", "Camo Green", "Feldgrau", "Deep Cyan", "Mexican Pink",
    "Tomato", "Fire Engine", "Celtic Blue", "Old Gold", "Burnt Sienna",
    "Carmine", "Mustard", "French Violet",
]
BG_MAP = {str(i): bg for i, bg in enumerate(BG_LIST)}
_BG_PATTERN = re.compile(
    "|".join(re.escape(bg) for bg in sorted(BG_LIST, key=len, reverse=True)),
    re.IGNORECASE,
)
_BG_LOWER_MAP = {bg.lower(): bg for bg in BG_LIST}

BLACK_BGS = {"black", "onyx black"}


# ─── JSON-УТИЛИТЫ ────────────────────────────────────────────────────────────
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─── ФОН ЛОТА ────────────────────────────────────────────────────────────────
BG_CACHE: dict = {}


async def fetch_bg(session: aiohttp.ClientSession, name: str, nft_id: int) -> dict:
    key = f"{name}-{nft_id}"
    if key in BG_CACHE:
        return {"name": name, "id": nft_id, "bg": BG_CACHE[key],
                "link": f"https://t.me/nft/{name}-{nft_id}"}
    url = f"https://t.me/nft/{name}-{nft_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                BG_CACHE[key] = None
                return {"name": name, "id": nft_id, "bg": None, "link": url}
            text = await resp.text()
            m = _BG_PATTERN.search(text)
            found = _BG_LOWER_MAP.get(m.group(0).lower()) if m else None
            BG_CACHE[key] = found
            return {"name": name, "id": nft_id, "bg": found, "link": url}
    except Exception:
        return {"name": name, "id": nft_id, "bg": None, "link": url}

async def fetch_bg_by_slug(slug: str) -> str | None:
    """Парсит фон лота по slug (вида Name-123) через t.me. Кэшируется в BG_CACHE."""
    if not slug or "-" not in slug:
        return None
    if slug in BG_CACHE:
        return BG_CACHE[slug]
    name, _, num = slug.rpartition("-")
    try:
        nft_id = int(num)
    except ValueError:
        return None
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            r = await fetch_bg(session, name, nft_id)
        bg = r.get("bg")
        BG_CACHE[slug] = bg
        return bg
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  ПРОФИЛЬ ПРОДАВЦА (уровень / подарки / цена сообщения)
# ═══════════════════════════════════════════════════════════════════════════
_PROFILE_CACHE: dict[int, dict] = {}   # user_id -> {level, gifts, paid_msg, cached_at}
PROFILE_TTL = 3600  # 1 час


async def get_user_profile(user_id: int) -> dict | None:
    """Уровень, число подарков и цена платных сообщений. Кэшируется."""
    global _flood_until
    cached = _PROFILE_CACHE.get(user_id)
    if cached and time.time() - cached["cached_at"] < PROFILE_TTL:
        return cached
    if not pyro_client:
        return None
    now = time.monotonic()
    if _flood_until > now:
        await asyncio.sleep(_flood_until - now)
    try:
        async with _PYRO_SEM:
            peer = await pyro_client.resolve_peer(user_id)
            full = await pyro_client.invoke(functions.users.GetFullUser(id=peer))
            gifts_count = None
            try:
                sg = await pyro_client.invoke(
                    functions.payments.GetSavedStarGifts(peer=peer, offset="", limit=1)
                )
                gifts_count = getattr(sg, "count", None)
            except Exception:
                gifts_count = None
        fu = getattr(full, "full_user", None)
        rating = getattr(fu, "stars_rating", None) if fu else None
        stargifts = (getattr(fu, "stargifts_count", 0) or 0) if fu else 0
        info = {
            "level": (getattr(rating, "level", 0) or 0) if rating else 0,
            "gifts": int(gifts_count) if gifts_count is not None else stargifts,
            "paid_msg": (getattr(fu, "send_paid_messages_stars", 0) or 0) if fu else 0,
            "about": (getattr(fu, "about", "") or "") if fu else "",
            "cached_at": time.time(),
        }
        _PROFILE_CACHE[user_id] = info
        return info
    except FloodWait as e:
        _flood_until = time.monotonic() + e.value + 1
        return None
    except Exception as e:
        if "PEER_ID_INVALID" not in str(e):
            print(f"[profile] {user_id}: {type(e).__name__}: {e}")
        return None

async def get_seller_info(user_id: int) -> tuple[int, int] | None:
    """Тонкая обёртка над get_user_profile — единый кэш профиля."""
    p = await get_user_profile(user_id)
    return (p["level"], p["gifts"]) if p else None


# ─── ПОДСЧЁТ ТОЛЬКО NFT (улучшенных) ПОДАРКОВ ────────────────────────────────
_NFT_COUNT_CACHE: dict[int, tuple[int, float]] = {}
NFT_COUNT_TTL = 3600


async def count_nft_gifts(user_id: int) -> int:
    """Считает только улучшенные NFT (unique) подарки в профиле. Кэш 1ч."""
    global _flood_until
    cached = _NFT_COUNT_CACHE.get(user_id)
    if cached and time.time() - cached[1] < NFT_COUNT_TTL:
        return cached[0]
    if not pyro_client:
        return 0
    now = time.monotonic()
    if _flood_until > now:
        await asyncio.sleep(_flood_until - now)
    try:
        async with _PYRO_SEM:
            peer = await pyro_client.resolve_peer(user_id)
            res = await pyro_client.invoke(
                functions.payments.GetSavedStarGifts(
                    peer=peer, offset="", limit=100, exclude_unupgradable=True
                )
            )
        cnt = 0
        for g in (getattr(res, "gifts", []) or []):
            sg = getattr(g, "gift", None)
            if sg is not None and type(sg).__name__ == "StarGiftUnique":
                cnt += 1
        _NFT_COUNT_CACHE[user_id] = (cnt, time.time())
        return cnt
    except FloodWait as e:
        _flood_until = time.monotonic() + e.value + 1
        return 0
    except Exception as e:
        if "PEER_ID_INVALID" not in str(e):
            print(f"[count_nft] {user_id}: {type(e).__name__}: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════════════════
#  МОНИТОРИНГ RESALE-МАРКЕТА
# ═══════════════════════════════════════════════════════════════════════════
WATCH_GIFT_IDS = {
    5936013938331222567, 5915521180483191380, 5898012527257715797,
    5900177027566142759, 5999277561060787166, 5170594532177215681,
    6014591077976114307, 5832644211639321671, 5933629604416717361,
    5933671725160989227, 5895328365971244193, 5895518353849582541,
    5843762284240831056, 5913517067138499193, 5870720080265871962,
    5846226946928673709, 5879737836550226478, 5859442703032386168,
    6005797617768858105, 5933531623327795414, 5870661333703197240,
    5841689550203650524, 5846192273657692751, 5902339509239940491,
    6014697240977737490, 5999116401002939514, 5839094187366024301,
    5868659926187901653, 5845776576658015084, 5933793770951673155,
    5936085638515261992, 5837059369300132790, 5841632504448025405,
    5839038009193792264, 5868503709637411929, 5882125812596999035,
    5868561433997870501, 5857140566201991735, 5836780359634649414,
    5936043693864651359,
}

# ─── ЛИМИТЫ ПАРСИНГА (5 сессий — быстрый режим без флуда) ─────────────────────
RESALE_PER_GIFT_LIMIT           = 30    # золотая середина
RESALE_CATALOG_CONCURRENCY      = 5     # по одной параллельной задаче на сессию
RESALE_MAX_PAGES_PER_TYPE       = 80    # не долбим один тип бесконечно
RESALE_PAUSE_BETWEEN_TYPES      = 0.25  # умеренная пауза
RESALE_PAUSE_BETWEEN_PAGES      = 0.10  # быстро, но не флудим
RESALE_ERROR_COOLDOWN_SECONDS   = 60    # средний cooldown при ошибках
UPGRADED_PAUSE_BETWEEN_PAGES    = 0.07
NAME_MAX_CONCURRENCY            = 2     # для resolve_peer оставляем мало
RESOLVE_BATCH_SIZE              = 250
RESALE_LOOP_INTERVAL = 30
USERBOT_POOL_SIZE = 5

# ── недостающие константы диспетчера/вотчера (раньше вызывали NameError) ──
POSTS_PER_MINUTE                = 15    # 4 секунды между постами (безопасно даже с MIRROR_ALL=True)
POST_INTERVAL                   = round(60.0 / POSTS_PER_MINUTE, 3)  # = 4.0 c
RESALE_GIFT_PAUSE               = 0.3   # пауза между чанками типов гифтов
RESALE_CYCLE_PAUSE              = 25    # пауза между полными циклами обхода
RESALE_DEAR_REPEAT              = 2     # повторный опрос "дорогих" типов
RESALE_DEAR_STARS               = 3000  # порог "дорогого" лота (в звёздах)
RESALE_DEAR_TON                 = 5.0   # порог "дорогого" лота (в TON)

# ── фильтр по тиражу: приоритет дорогим/редким, дешёвые — в свою ветку ──
HIGH_SUPPLY_THRESHOLD    = 100_000  # тираж ≥ этого = «дешёвый массовый» (ветка cheap)
HIGH_SUPPLY_BASE_WEIGHT  = 0.30     # вес на границе 100k
HIGH_SUPPLY_MIN_WEIGHT   = 0.05     # нижний предел веса для гигантских тиражей
HIGH_SUPPLY_ENQUEUE_DROP = 0.0      # 0 = дешёвые не дропаем (у них своя ветка); >0 = прорежать
TOP_FLOOR_BOOST          = 2.5      # ×вес для топ-коллекций (floor 30+)
UNKNOWN_SUPPLY_FACTOR    = 0.8      # лёгкий штраф незнакомым коллекциям


_post_queue: "collections.deque" = collections.deque()
_queued_ids: set[int] = set()
_dear_gift_ids: set[int] = set()   # типы гифтов, где попадались дорогие лоты

def resale_known_ids() -> set[int]:
    conn = sqlite3.connect(OWNERS_DB)
    rows = conn.execute("SELECT instance_id FROM resale_listings").fetchall()
    conn.close()
    return {r[0] for r in rows}


def resale_save(rec: dict):
    conn = sqlite3.connect(OWNERS_DB)
    conn.execute(
        "INSERT OR IGNORE INTO resale_listings "
        "(instance_id, gift_id, title, slug, num, price_stars, price_ton, "
        " value_usd, offer_min, seller_id, seller_name, posted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["instance_id"], rec["gift_id"], rec["title"], rec["slug"], rec["num"],
         rec["price_stars"], rec["price_ton"], rec["value_usd"], rec["offer_min"],
         rec["seller_id"], rec["seller_name"], time.time()),
    )
    conn.commit()
    conn.close()


def parse_resale_amounts(resell_amount) -> tuple[int, float]:
    """Возвращает (звёзды, TON). TON приходит в нано (1 TON = 1e9)."""
    stars, ton = 0, 0.0
    for a in (resell_amount or []):
        cls = getattr(a, "__class__", None)
        name = getattr(cls, "__name__", "")
        amount = getattr(a, "amount", 0) or 0
        if "Ton" in name:
            ton = amount / 1_000_000_000
        else:
            stars = amount
    return stars, ton


def resale_text(rec: dict) -> str:
    price_parts = []
    if rec["price_stars"]:
        price_parts.append(f"⭐ {rec['price_stars']:,}".replace(",", " "))
    if rec["price_ton"]:
        price_parts.append(f"💎 {rec['price_ton']:.2f} TON")
    price = "  /  ".join(price_parts) if price_parts else "—"

    usd = f"≈ ${rec['value_usd']}" if rec["value_usd"] else ""
    seller = rec["seller_name"] or f"id{rec['seller_id']}"

    supply = rec.get("supply")
    if supply is None:
        supply = gift_supply(rec.get("title", ""))
    if supply:
        supply_line = (f"📦 Тираж: <b>{supply:,}</b> шт · "
                       f"{supply_rarity_label(supply)}").replace(",", " ")
    else:
        supply_line = "📦 Тираж: <b>—</b>"

    paid = rec.get("paid_msg", 0)
    if paid:
        msg_line = f"✉️ Сообщения: <b>платно ({paid} ⭐)</b>"
    else:
        msg_line = "✉️ Сообщения: <b>бесплатно</b>"

    return (
        "🆕 <b>НОВЫЙ ЛИСТИНГ НА МАРКЕТЕ</b>\n\n"
        f"🎁 <b>{rec['title']}</b> #{rec['num']}\n"
        f"💰 Цена: <b>{price}</b> {usd}\n"
        f"{supply_line}\n"
        f"🤝 Мин. оффер: <b>{rec['offer_min'] or '—'}</b> ⭐\n"
        f"👤 Продавец: {seller}\n"
        f"⭐ Уровень: <b>{rec.get('seller_level', '—') if rec.get('seller_level') is not None else '—'}</b>\n"
        f"🎁 Подарков в профиле: <b>{rec.get('seller_gifts', '—') if rec.get('seller_gifts') is not None else '—'}</b>\n"
        f"{msg_line}\n"
        f"🔗 <a href='https://t.me/nft/{rec['slug']}'>Открыть подарок</a>"
    )


def resale_kb(rec: dict):
    rows = [[InlineKeyboardButton(
        text="🎯 Занять лот", callback_data=f"rclaim_{rec['instance_id']}"
    )]]
    if rec["seller_name"] and rec["seller_name"].startswith("@"):
        url = f"https://t.me/{rec['seller_name'][1:]}"
    else:
        url = f"tg://user?id={rec['seller_id']}"
    rows.append([InlineKeyboardButton(text="👤 Профиль продавца", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def fetch_resale_for_gift(gift_id: int) -> list[dict]:
    """Свежие resale-листинги одного типа гифта."""
    global _flood_until
    if not pyro_client:
        return []

    now = time.monotonic()
    if _flood_until > now:
        await asyncio.sleep(_flood_until - now)

    try:
        async with _PYRO_SEM:
            res = await pyro_client.invoke(
                functions.payments.GetResaleStarGifts(
                    gift_id=gift_id, offset="", limit=RESALE_PER_GIFT_LIMIT
                )
            )
    except FloodWait as e:
        _flood_until = time.monotonic() + e.value + 1
        print(f"[resale] FloodWait {e.value}s — пауза")
        await asyncio.sleep(e.value + 1)
        return []
    except Exception as e:
        print(f"[resale] gift {gift_id}: {type(e).__name__}: {e}")
        return []

    users = {}
    for u in (getattr(res, "users", []) or []):
        uname = getattr(u, "username", None)
        if not uname:
            unames = getattr(u, "usernames", None) or []
            for un in unames:
                if getattr(un, "active", False):
                    uname = getattr(un, "username", None)
                    break
        first = getattr(u, "first_name", "") or ""
        last  = getattr(u, "last_name", "") or ""
        display = f"@{uname}" if uname else (first or f"id{u.id}")
        users[u.id] = {
            "display": display,
            "paid":  getattr(u, "send_paid_messages_stars", 0) or 0,
            "first": first,
            "last":  last,
            "uname": uname or "",
        }

    out = []
    for g in (getattr(res, "gifts", []) or []):
        inst_id = getattr(g, "id", None)
        if inst_id is None:
            continue
        stars, ton = parse_resale_amounts(getattr(g, "resell_amount", None))
        owner = getattr(g, "owner_id", None)
        seller_id = getattr(owner, "user_id", None) if owner else None
        uinfo = users.get(seller_id) if seller_id else None  # ← НОВАЯ строка
        out.append({
            "instance_id": inst_id,
            "gift_id": getattr(g, "gift_id", gift_id),
            "title": getattr(g, "title", "") or "Gift",
            "slug": getattr(g, "slug", "") or "",
            "num": getattr(g, "num", 0) or 0,
            "price_stars": stars,
            "price_ton": ton,
            "value_usd": getattr(g, "value_amount", 0) or 0,
            "offer_min": getattr(g, "offer_min_stars", 0) or 0,
            "seller_id": seller_id,
            "seller_name": uinfo["display"] if uinfo else "—",
            "paid_msg": uinfo["paid"] if uinfo else 0,
            "seller_first": uinfo["first"] if uinfo else "",
            "seller_last": uinfo["last"] if uinfo else "",
            "seller_uname": uinfo["uname"] if uinfo else "",
        })

    random.shuffle(out)
    return out


async def fetch_catalog_gift_ids() -> list[int]:
    """ID типов гифтов из первичного каталога — по ним крутим resale."""
    if not pyro_client:
        return []
    try:
        async with _PYRO_SEM:
            res = await pyro_client.invoke(functions.payments.GetStarGifts(hash=0))
        ids = []
        for g in (getattr(res, "gifts", []) or []):
            if getattr(g, "limited", False):
                gid = getattr(g, "id", None)
                if gid:
                    ids.append(gid)
        return ids
    except Exception as e:
        print(f"[catalog] {type(e).__name__}: {e}")
        return []



async def dump_gift_catalog() -> list[tuple[int, str, int | None]]:
    """Каталог Telegram (payments.GetStarGifts) → [(gift_id, title, supply), ...].
    Отсюда берутся ID для WATCH_GIFT_IDS."""
    if not pyro_client:
        return []
    try:
        async with _PYRO_SEM:
            res = await pyro_client.invoke(functions.payments.GetStarGifts(hash=0))
    except Exception as e:
        print(f"[giftids] {type(e).__name__}: {e}")
        return []
    out = []
    for g in (getattr(res, "gifts", []) or []):
        if not getattr(g, "limited", False):
            continue
        gid = getattr(g, "id", None)
        if not gid:
            continue
        title = getattr(g, "title", "") or ""
        out.append((gid, title, gift_supply(title)))
    out.sort(key=lambda x: (x[2] is None, x[2] or 0))
    return out

@dp.message(Command("giftids"))
async def cmd_giftids(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    cat = await dump_gift_catalog()
    if not cat:
        await message.answer("Каталог пуст или Pyrogram не запущен.")
        return
    watched = set(WATCH_GIFT_IDS)
    lines = []
    for gid, title, supply in cat:
        mark = "✅" if gid in watched else "▫️"
        sup = f"{supply:,}".replace(",", " ") if supply else "—"
        lines.append(f"{mark} <code>{gid}</code> · {title or '—'} · тираж {sup}")
    chunk = (f"<b>Каталог подарков</b> (всего {len(cat)}, в WATCH: {len(watched)})\n"
             f"✅ = уже в WATCH_GIFT_IDS\n\n")
    for ln in lines:
        if len(chunk) + len(ln) > 3900:
            await message.answer(chunk, parse_mode="HTML")
            chunk = ""
        chunk += ln + "\n"
    if chunk.strip():
        await message.answer(chunk, parse_mode="HTML")


# ─── WATCHLIST (Portals: коллекции с floor ≥ MIN_FLOOR) ──────────────────────
WATCH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch_gifts.json")
MIN_FLOOR = 30.0
PORTALS_COLLECTIONS_URL = "https://portals-market.com/api/nfts/collections"
PORTALS_AUTH = os.getenv("PORTALS_AUTH", "")
WATCH_REFRESH_INTERVAL = 86400


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_watch_list() -> set:
    ids = load_json(WATCH_FILE, None)
    if not ids:
        return set(WATCH_GIFT_IDS)
    try:
        return {int(x) for x in ids}
    except Exception:
        return set(WATCH_GIFT_IDS)


async def build_watch_list_from_portals() -> set:
    """Whitelist gift_id по коллекциям с floor ≥ MIN_FLOOR. Хардкод — резерв."""
    title_to_id = {}
    for gid, title, _sup in await dump_gift_catalog():
        if title:
            title_to_id[_norm_title(title)] = gid
    headers = {"User-Agent": "Mozilla/5.0"}
    if PORTALS_AUTH:
        headers["Authorization"] = PORTALS_AUTH
    try:
        async with aiohttp.ClientSession(headers=headers) as sess:
            async with sess.get(PORTALS_COLLECTIONS_URL,
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
        collections = data.get("collections", data) if isinstance(data, dict) else data
    except Exception as e:
        print(f"[watch] Portals недоступен ({type(e).__name__}: {e}) — беру хардкод")
        return set(WATCH_GIFT_IDS)
    watch = set()
    for col in (collections or []):
        floor = (col.get("floor_price_ton") or col.get("floorPrice")
                 or col.get("floor") or 0)
        try:
            floor = float(floor)
        except (TypeError, ValueError):
            continue
        if floor < MIN_FLOOR:
            continue
        gid = col.get("gift_id") or col.get("giftId")
        if not gid:
            gid = title_to_id.get(_norm_title(col.get("name") or col.get("title") or ""))
        if gid:
            watch.add(int(gid))
    if not watch:
        print("[watch] Portals не дал совпадений — беру хардкод")
        return set(WATCH_GIFT_IDS)
    watch |= set(WATCH_GIFT_IDS)
    save_json(WATCH_FILE, sorted(watch))
    print(f"[watch] whitelist обновлён: {len(watch)} типов (floor ≥ {MIN_FLOOR})")
    return watch


async def watch_list_refresher():
    global WATCH_GIFT_IDS
    while True:
        try:
            WATCH_GIFT_IDS = await build_watch_list_from_portals()
        except Exception as e:
            print(f"[watch_refresher] {type(e).__name__}: {e}")
        await asyncio.sleep(WATCH_REFRESH_INTERVAL)


# ─── ОСНОВНОЙ ЦИКЛ ОБХОДА ────────────────────────────────────────────────────
async def resale_watcher():
    """Тихо ищет новые листинги и кладёт их в очередь (не постит сам)."""
    await asyncio.sleep(8)
    first_run = True
    while True:
        try:
            known = resale_known_ids()
            # динамический каталог всех лимитированных гифтов + базовый список
            catalog_ids = await fetch_catalog_gift_ids()
            all_ids = set(WATCH_GIFT_IDS) | set(catalog_ids)
            gids = list(all_ids)
            print(f"[resale] типов для обхода: {len(gids)} (каталог: {len(catalog_ids)})")
            random.shuffle(gids)
            # дорогие типы (где недавно были дорогие лоты) опрашиваем повторно
            dear_gids = list(_dear_gift_ids)
            scan_list = gids + dear_gids * (RESALE_DEAR_REPEAT - 1)
            random.shuffle(scan_list)
            # ── параллельная обработка типов: чанк = число сессий (3) ──
            RESALE_CHUNK = 3

            async def _fetch_one(gid):
                try:
                    return await fetch_resale_for_gift(gid) or []
                except Exception as e:
                    print(f"[resale-fetch] gid={gid}: {type(e).__name__}: {e}")
                    return []

            async def _handle_rec(rec):
                iid = rec["instance_id"]
                if iid in known:
                    return
                resale_save(rec)
                known.add(iid)
                if first_run:
                    return

                # тираж коллекции (для маршрутизации по веткам и веса очереди)
                rec["supply"] = gift_supply(rec.get("title", ""))

                rec["bg"] = await fetch_bg_by_slug(rec.get("slug", ""))
                is_black = (rec.get("bg") or "").strip().lower() in BLACK_BGS
                is_dear = (rec.get("title") or "").strip() in DEAR_WHITELIST
                if is_black:
                    max_nft, max_lvl = MAX_SELLER_GIFTS_BLACK, MAX_SELLER_LEVEL
                elif is_dear:
                    max_nft, max_lvl = MAX_SELLER_GIFTS_DEAR, MAX_SELLER_LEVEL_DEAR
                else:
                    max_nft, max_lvl = MAX_SELLER_GIFTS, MAX_SELLER_LEVEL

                seller_id = rec.get("seller_id")
                if seller_id:
                    info = await get_seller_info(seller_id)
                    if info is not None:
                        lvl, gifts_cnt = info
                        if lvl > max_lvl:
                            print(f"[skip] {seller_id} ур.{lvl} > {max_lvl}")
                            return
                        nft_cnt = await count_nft_gifts(seller_id)
                        if not (MIN_SELLER_GIFTS <= nft_cnt <= max_nft):
                            print(f"[skip] {seller_id} NFT-подарков {nft_cnt} вне {MIN_SELLER_GIFTS}-{max_nft}")
                            return
                        rec["seller_level"] = lvl
                        rec["seller_gifts"] = nft_cnt
                    else:
                        rec["seller_level"] = None
                        rec["seller_gifts"] = None
                else:
                    rec["seller_level"] = None
                    rec["seller_gifts"] = None

                if iid not in _queued_ids:
                    _post_queue.append(rec)
                    _queued_ids.add(iid)
                    if is_dear or (rec.get("price_stars") or 0) >= RESALE_DEAR_STARS:
                        _dear_gift_ids.add(rec["gift_id"])

            for chunk_start in range(0, len(scan_list), RESALE_CHUNK):
                chunk = scan_list[chunk_start:chunk_start + RESALE_CHUNK]
                # параллельно достаём лоты для всего чанка
                chunk_recs = await asyncio.gather(
                    *[_fetch_one(gid) for gid in chunk],
                    return_exceptions=False,
                )
                # обрабатываем rec-и с ограниченной параллельностью
                all_recs = [rec for recs in chunk_recs for rec in recs]
                # обработка партиями по 4 для контроля нагрузки
                REC_BATCH = 4
                for i in range(0, len(all_recs), REC_BATCH):
                    batch = all_recs[i:i + REC_BATCH]
                    await asyncio.gather(
                        *[_handle_rec(rec) for rec in batch],
                        return_exceptions=True,
                    )
                await asyncio.sleep(RESALE_GIFT_PAUSE)
            first_run = False
            print(f"[resale] обход завершён, в очереди: {len(_post_queue)}")
        except Exception as e:
            print(f"[resale_watcher] {type(e).__name__}: {e}")
        await asyncio.sleep(RESALE_CYCLE_PAUSE)


# ─── ВЕС ОЧЕРЕДИ / МАРШРУТИЗАЦИЯ ПО ВЕТКАМ ───────────────────────────────────
DEAR_BOOST = 3.0  # приоритет в очереди для DEAR_WHITELIST — не зависит от цены/звёзд


def _supply_weight_factor(rec: dict) -> float:
    """Множитель веса по тиражу: дорогие (из списка) и топ-коллекции ↑, массовые (≥100k) ↓."""
    title = rec.get("title", "")
    # DEAR_WHITELIST в приоритете всегда, вне зависимости от цены лота —
    # если подарок есть в списке, он должен чаще всплывать в очереди.
    if title in DEAR_WHITELIST:
        return max(DEAR_BOOST, TOP_FLOOR_BOOST) if title in TOP_FLOOR_TITLES else DEAR_BOOST
    if title in TOP_FLOOR_TITLES:
        return TOP_FLOOR_BOOST
    supply = rec.get("supply")
    if supply is None:
        supply = gift_supply(title)
    if supply is None:
        return UNKNOWN_SUPPLY_FACTOR
    if supply >= HIGH_SUPPLY_THRESHOLD:
        return max(HIGH_SUPPLY_MIN_WEIGHT,
                   HIGH_SUPPLY_BASE_WEIGHT * (HIGH_SUPPLY_THRESHOLD / supply))
    return 1.0


def _pick_weighted_index() -> int | None:
    """Выбирает индекс из очереди с приоритетом дорогим лотам (по price_ton).
    Дешёвые не исчезают — просто реже. Чем выше цена, тем выше шанс."""
    if not _post_queue:
        return None
    weights = []
    for rec in _post_queue:
        stars = rec.get("price_stars") or 0
        weights.append((1.0 + (stars / 1000.0) ** 1.4) * _supply_weight_factor(rec))
    total = sum(weights)
    if total <= 0:
        return random.randrange(len(_post_queue))
    r = random.uniform(0, total)
    upto = 0.0
    for i, w in enumerate(weights):
        upto += w
        if upto >= r:
            return i
    return len(_post_queue) - 1

def _route_topic_key(rec: dict) -> str:
    """Профильная ветка лота: чёрный фон → дорогой → дешёвый.

    В 'dear' попадают ТОЛЬКО подарки из DEAR_WHITELIST (30 конкретных названий).
    Всё остальное (кроме чёрного фона) — 'cheap'.
    """
    if (rec.get("bg") or "").strip().lower() in BLACK_BGS:
        return "black"
    title = (rec.get("title") or "").strip()
    if title in DEAR_WHITELIST:
        return "dear"
    return "cheap"


_dispatch_counter = 0
FORCE_DEAR_EVERY = 4  # каждый N-й пост — гарантированно из DEAR_WHITELIST, если такой есть в очереди


def _pop_first_dear() -> dict | None:
    """Достаёт из очереди первый попавшийся лот из DEAR_WHITELIST (если есть)."""
    for idx, rec in enumerate(_post_queue):
        if (rec.get("title") or "").strip() in DEAR_WHITELIST:
            _post_queue.rotate(-idx)
            rec = _post_queue.popleft()
            _post_queue.rotate(idx)
            return rec
    return None

async def post_dispatcher():
    """Достаёт из очереди по одному и постит раз в POST_INTERVAL секунд."""
    global _dispatch_counter
    await asyncio.sleep(5)
    while True:
        try:
            if _post_queue:
                _dispatch_counter += 1
                rec = None
                # раз в FORCE_DEAR_EVERY постов — гарантированно достаём дорогой лот,
                # чтобы он не тонул в очереди из-за большого числа дешёвых
                if _dispatch_counter % FORCE_DEAR_EVERY == 0:
                    rec = _pop_first_dear()
                if rec is None:
                    # Взвешенный выбор работает только если есть из чего выбирать
                    if len(_post_queue) >= 3:
                        i = _pick_weighted_index()
                        _post_queue.rotate(-i)
                        rec = _post_queue.popleft()
                        _post_queue.rotate(i)
                    else:
                        rec = _post_queue.popleft()
                _queued_ids.discard(rec["instance_id"])

                key = _route_topic_key(rec)

                targets = [key]
                if MIRROR_ALL and key != "all":
                    targets.append("all")

                for k in targets:
                    try:
                        await bot.send_message(
                            GROUP_ID,
                            resale_text(rec),
                            message_thread_id=TOPIC_THREADS[k],
                            reply_markup=resale_kb(rec),
                            parse_mode="HTML",
                        )
                        print(f"[post] {rec['title']} #{rec['num']} → ветка {k} (id {TOPIC_THREADS[k]})")
                    except TelegramRetryAfter as e:
                        # Telegram просит подождать → ждём и повторяем ЭТУ отправку
                        print(f"[post] RetryAfter {e.retry_after}s → жду и повторяю")
                        await asyncio.sleep(e.retry_after + 0.5)
                        try:
                            await bot.send_message(
                                GROUP_ID,
                                resale_text(rec),
                                message_thread_id=TOPIC_THREADS[k],
                                reply_markup=resale_kb(rec),
                                parse_mode="HTML",
                            )
                            print(f"[post] {rec['title']} #{rec['num']} → ветка {k} (повтор ok)")
                        except Exception as e2:
                            print(f"[post] fail-retry {rec['instance_id']} ({k}): {e2}")
                    except Exception as e:
                        print(f"[post] fail {rec['instance_id']} ({k}): {e}")
        except Exception as e:
            print(f"[dispatcher] {type(e).__name__}: {e}")
        await asyncio.sleep(POST_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════
#  КНОПКА «ЗАНЯТЬ ЛОТ»
# ═══════════════════════════════════════════════════════════════════════════
# --- claim: rate limit + БД ---
_claim_rl: dict[int, list[float]] = {}
CLAIM_RL_WINDOW = 60
CLAIM_RL_MAX    = 10

def _claim_rl_check(user_id: int) -> tuple[bool, int]:
    import time as _t
    now = _t.monotonic()
    arr = [x for x in _claim_rl.get(user_id, []) if now - x < CLAIM_RL_WINDOW]
    if len(arr) >= CLAIM_RL_MAX:
        wait = int(CLAIM_RL_WINDOW - (now - arr[0])) + 1
        _claim_rl[user_id] = arr
        return False, wait
    arr.append(now)
    _claim_rl[user_id] = arr
    return True, 0

def _claim_get(instance_id: int):
    conn = sqlite3.connect(OWNERS_DB)
    row = conn.execute(
        "SELECT user_id, username, first_name FROM claimed_lots WHERE instance_id=?",
        (instance_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"user_id": row[0], "username": row[1] or "", "first_name": row[2] or ""}

def _claim_put(instance_id: int, user_id: int, username: str, first_name: str) -> bool:
    import time as _t
    conn = sqlite3.connect(OWNERS_DB)
    try:
        conn.execute(
            "INSERT INTO claimed_lots (instance_id, user_id, username, first_name, claimed_at) VALUES (?, ?, ?, ?, ?)",
            (instance_id, user_id, username or "", first_name or "", int(_t.time()))
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        print(f"[claim_put] {type(e).__name__}: {e}")
        return False
    finally:
        conn.close()

def _claim_mention(user_id: int, username: str, first_name: str) -> str:
    if username:
        return f"@{username}"
    name = (first_name or "пользователь").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{name}</a>'

def _claim_plain(username: str, first_name: str) -> str:
    if username:
        return f"@{username}"
    return first_name or "другой пользователь"

@dp.callback_query(lambda c: c.data and c.data.startswith("rclaim_"))
async def cb_resale_claim(callback: CallbackQuery):
    try:
        instance_id = int(callback.data.replace("rclaim_", ""))
    except ValueError:
        await callback.answer("⚠️ Некорректный лот.", show_alert=True)
        return

    uid = callback.from_user.id

    existing = _claim_get(instance_id)
    if existing:
        if existing["user_id"] == uid:
            await callback.answer("Ты уже занял этот лот.")
        else:
            await callback.answer(
                f"❌ Уже занял: {_claim_plain(existing['username'], existing['first_name'])}",
                show_alert=True
            )
        return

    ok, wait = _claim_rl_check(uid)
    if not ok:
        await callback.answer(f"⏳ Слишком много лотов. Подожди {wait} сек.", show_alert=True)
        return

    username   = callback.from_user.username or ""
    first_name = callback.from_user.first_name or ""

    if not _claim_put(instance_id, uid, username, first_name):
        existing = _claim_get(instance_id)
        if existing:
            await callback.answer(
                f"❌ Уже занял: {_claim_plain(existing['username'], existing['first_name'])}",
                show_alert=True
            )
        else:
            await callback.answer("⚠️ Не удалось занять. Попробуй ещё раз.", show_alert=True)
        return


    # ─── ОТПРАВКА ЛОТА В ЛС ЮЗЕРУ ─────────────────────────────────────
    original_text = ""
    original_kb = None
    try:
        original_text = callback.message.html_text or callback.message.text or ""
    except Exception:
        try:
            original_text = callback.message.caption or ""
        except Exception:
            original_text = ""

    try:
        original_kb = callback.message.reply_markup
    except Exception:
        original_kb = None

    dm_header = "🎯 <b>Вы заняли лот!</b>\n\n"
    dm_footer = "\n\n<i>Свяжитесь с продавцом и завершите сделку.</i>"
    dm_text = dm_header + original_text + dm_footer

    # оставляем кнопки перехода (если были ссылки на лот/продавца)
    dm_kb = None
    if original_kb and hasattr(original_kb, "inline_keyboard"):
        keep_rows = []
        for row in original_kb.inline_keyboard:
            keep_row = [btn for btn in row if getattr(btn, "url", None)]
            if keep_row:
                keep_rows.append(keep_row)
        if keep_rows:
            dm_kb = InlineKeyboardMarkup(inline_keyboard=keep_rows)

    try:
        # если есть картинка/фото — попробуем скопировать сообщение
        if callback.message.photo:
            await callback.message.copy_to(
                chat_id=uid,
                caption=dm_text[:1024],
                parse_mode="HTML",
                reply_markup=dm_kb,
            )
        else:
            await bot.send_message(
                uid, dm_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=dm_kb,
            )
        print(f"[claim] ✅ DM отправлен uid={uid} instance={instance_id}")
    except Exception as e:
        err_name = type(e).__name__
        print(f"[claim] ❌ DM FAILED uid={uid}: {err_name}: {e}")
        # Юзер не нажал /start у бота — покажем это в ответе
        if "Forbidden" in err_name or "chat not found" in str(e).lower():
            try:
                me = await bot.get_me()
                bot_link = f"https://t.me/{me.username}"
                await callback.answer(
                    f"⚠️ Сначала нажми /start у @{me.username}, потом попробуй снова.",
                    show_alert=True,
                )
            except Exception:
                pass

    mention = _claim_mention(uid, username, first_name)
    new_text = (
        "🔒 <b>ЛОТ ЗАНЯТ</b>\n\n"
        "Продавец и подарок скрыты.\n\n"
        f"Занял: {mention}"
    )
    new_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 Занято", callback_data="noop")
    ]])
    try:
        await callback.message.edit_text(
            new_text, reply_markup=new_kb,
            parse_mode="HTML", disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        try:
            await callback.message.edit_caption(
                caption=new_text, reply_markup=new_kb, parse_mode="HTML",
            )
        except Exception as e:
            print(f"[claim] edit failed instance={instance_id}: {e}")

    await callback.answer("✅ Лот занят.")


# ═══════════════════════════════════════════════════════════════════════════
#  ХЭНДЛЕРЫ БОТА (минимум: /start + «Занять лот» + noop + /giftids)
# ═══════════════════════════════════════════════════════════════════════════
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "<b>🛰 Market Tracker</b>\n\n"
        "Я слежу за resale-маркетом и публикую новые листинги в группу.\n"
        "Нажимай «🎯 Занять лот» под постом — карточка придёт сюда, в ЛС.",
        parse_mode="HTML",
    )


@dp.callback_query(lambda c: c.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.errors()
async def global_error_handler(event):
    err = event.exception
    if isinstance(err, TelegramRetryAfter):
        return True
    if isinstance(err, TelegramBadRequest) and "query is too old" in str(err):
        return True
    print(f"[error] {type(err).__name__}: {err}")
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════════════════
async def main():
    global pyro_client, WATCH_GIFT_IDS

    init_db()
    print(f"✅ БД: {OWNERS_DB}", flush=True)

    # ── Pyrogram-пул ──
    if API_ID and API_HASH:
        pyro_client = SessionProxy()
        started = await pyro_client.start_all()
        if started:
            print(f"✅ Pyrogram: запущено сессий — {started}/{len(POOL_SESSIONS)}", flush=True)
            WATCH_GIFT_IDS = load_watch_list()
            asyncio.create_task(resale_watcher())
            asyncio.create_task(post_dispatcher())
            asyncio.create_task(watch_list_refresher())
            print("✅ resale_watcher + post_dispatcher + watch_list_refresher запущены", flush=True)
        else:
            print("⚠️  Ни одна сессия не запущена — проверь .session файлы (auth.py)", flush=True)
            pyro_client = None
    else:
        print("⚠️  Pyrogram не настроен — трекер работать не будет", flush=True)

    # ── aiogram polling ──
    # ВАЖНО: снимаем webhook и старые апдейты — иначе getUpdates конфликтует
    try:
        me = await bot.get_me()
        await bot.delete_webhook(drop_pending_updates=True)
        print(f"✅ Бот @{me.username} запущен, webhook снят — стартую polling", flush=True)
    except Exception as e:
        print(f"❌ Ошибка инициализации бота: {type(e).__name__}: {e}", flush=True)

    try:
        await dp.start_polling(bot)
    finally:
        if pyro_client:
            await pyro_client.stop_all()


if __name__ == "__main__":
    asyncio.run(main())
