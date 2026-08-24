# -*- coding: utf-8 -*-
"""
parser_bot.py — интерактивный бот-парсер NFT-подарков Telegram.

Меню:
  🎲 Рандом            — случайные подарки (easy / medium / hard)
  🎨 Поиск по фону     — подарки с конкретным фоном
  🎁 Поиск по подаркам — владельцы выбранной коллекции
  👩 Поиск девушек     — владелицы NFT (по имени)
  🔓 Неулучшенные      — юзеры с лимитированными неулучшёнными подарками
  ⚙️ Лимит             — сколько результатов искать

БД owners.db — ОБЩАЯ с market_tracker.py (лежит рядом с этим файлом).
Запуск: python parser_bot.py
"""
import json
import asyncio
import random
import re
import time
import os
import sqlite3
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from pyrogram import Client, enums
from pyrogram.raw import functions
from pyrogram.errors import FloodWait

# ═══════════════════════════════════════════════════════════════════════════
#  КОНФИГ
# ═══════════════════════════════════════════════════════════════════════════
ADMIN_ID = 899438668
PARSER_BOT_TOKEN = "ВСТАВЬ_ТОКЕН"      # ← токен ВТОРОГО бота из BotFather
API_ID = 32508082
API_HASH = "b5acbc0925f91f9e411f3397ec8b95a5"

# ─── СЕССИИ ДЛЯ ПУЛА ─────────────────────────────────────────────────────────
# Пусто — добавь 2-3 свои сессии в формате ("имя_сессии", "+номер").
# Файлы <имя_сессии>.session должны лежать рядом с этим скриптом.
POOL_SESSIONS = [
    # ("parsersess1", "+10000000000"),   # ← добавь сессии
    # ("parsersess2", "+10000000001"),   # ← добавь сессии
    # ("parsersess3", "+10000000002"),   # ← добавь сессии
]
POOL_CONCURRENCY_PER_SESSION = 2

# ─── БД (общая с market_tracker.py) ──────────────────────────────────────────
DB_FILE = "users.json"
OWNERS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "owners.db")
GIFT_CACHE_TTL = 86400  # 24 часа

# ─── ФИЛЬТРЫ ПОИСКА ──────────────────────────────────────────────────────────
FILTER_MAX_LEVEL = 4       # не показывать выше этого уровня (0 = выкл)
FILTER_MIN_GIFTS = 1
FILTER_MAX_GIFTS = 6
SEARCH_TIMEOUT = 180.0     # 0 = без верхней границы


# ═══════════════════════════════════════════════════════════════════════════
#  БД (owners.db) — создаём ВСЕ таблицы, база общая с market_tracker.py
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
_PYRO_SEM = asyncio.Semaphore(6)       # 2-3 сессии × 2 параллельных вызова
_flood_until: float = 0.0
_USER_CACHE: dict[int, tuple[str, str]] = {}
_active_search: dict[int, asyncio.Task] = {}

bot: Bot | None = None                 # создаётся в main() — токен проверяем там
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

# ─── СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЕЙ ─────────────────────────────────────────────────
user_limit = {}
user_search_mode = {}
user_mode = {}
user_bg = {}
user_gift = {}
user_max_level = {}


# ─── STATES ──────────────────────────────────────────────────────────────────
class BroadcastState(StatesGroup):
    waiting_message = State()


# ═══════════════════════════════════════════════════════════════════════════
#  NFT-ДАННЫЕ / ФОНЫ / ЖЕНСКИЕ ИМЕНА
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

GIFT_KEYS = list(NFT_DATA.keys())
GIFT_PAGE_SIZE = 20

NFT_EASY = {k: v for k, v in NFT_DATA.items() if v >= 100_000}
NFT_MEDIUM = {k: v for k, v in NFT_DATA.items() if 20_000 <= v < 100_000}
NFT_HARD = {k: v for k, v in NFT_DATA.items() if v < 20_000}
NFT_TIER = {"easy": NFT_EASY, "medium": NFT_MEDIUM, "hard": NFT_HARD}

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

FEMALE_NAMES = {
    "анна", "аня", "настя", "анастасия", "мария", "маша", "марина", "екатерина",
    "катя", "ольга", "оля", "наталья", "наташа", "татьяна", "таня", "елена",
    "лена", "алёна", "ирина", "ира", "светлана", "света", "юлия", "юля", "дарья",
    "даша", "полина", "валерия", "лера", "алина", "вика", "виктория", "ксения",
    "ксюша", "александра", "саша", "соня", "софия", "диана", "лиза", "елизавета",
    "вероника", "ника", "кристина", "людмила", "милана", "ева", "арина", "варвара",
    "anna", "anastasia", "maria", "kate", "katherine", "olga", "natalia", "natasha",
    "tatiana", "elena", "helen", "irina", "svetlana", "julia", "daria", "polina",
    "valeria", "alina", "victoria", "ksenia", "alexandra", "sofia", "sophie",
    "diana", "elizabeth", "liza", "veronika", "kristina", "milena", "nadia", "nina",
    "zhanna", "inna", "larisa", "tamara", "valentina", "oksana", "yana", "regina",
    "albina", "bella", "ilona", "karina", "camilla", "arina", "varvara", "evgenia",
    "nicole", "zlata", "eva", "evelina", "elvira", "lily", "rosa", "margaret",
    "emma", "emily", "jessica", "sarah", "ashley", "amanda", "stephanie", "rebecca",
    "jennifer", "linda", "patricia", "melissa", "laura", "kimberly", "angela",
    "samantha", "rachel", "hannah", "brittany", "megan", "amber", "danielle",
    "chelsea", "vanessa", "miranda", "gabrielle", "brianna", "alyssa", "kayla",
    "madison", "abigail", "taylor", "morgan", "sydney", "audrey", "grace",
    "luna", "aurora", "aria", "violet", "scarlett", "ruby", "hazel", "eleanor",
    "chloe", "zoe", "isla", "freya", "willow", "daisy", "alice", "clara", "vera",
}


# ─── УТИЛИТЫ ─────────────────────────────────────────────────────────────────
EDIT_INTERVAL = 3.0  # не редактируем одно сообщение чаще, чем раз в N секунд


async def safe_edit(message, text, **kwargs) -> bool:
    """edit_text с защитой от флуд-контроля. True — если правка прошла."""
    try:
        await message.edit_text(text, **kwargs)
        return True
    except TelegramRetryAfter as e:
        print(f"[safe_edit] flood, retry_after={e.retry_after}s — пропускаю апдейт")
        return False
    except TelegramBadRequest as e:
        s = str(e)
        if any(x in s for x in ("not modified", "message to edit not found", "query is too old")):
            return False
        print(f"[safe_edit] {s}")
        return False
    except Exception as e:
        print(f"[safe_edit] {type(e).__name__}: {e}")
        return False

def is_female(first_name: str) -> bool:
    if not first_name:
        return False
    name = first_name.strip().split()[0].lower()
    name = re.sub(r"[^\w]", "", name, flags=re.UNICODE)
    return name in FEMALE_NAMES

def profile_line(r: dict) -> str:
    """Единая строка профиля: уровень · подарки · цена сообщения."""
    lvl = r.get("level")
    gifts = r.get("gifts")
    paid = r.get("paid_msg")
    parts = []
    parts.append(f"⭐ ур.{lvl}" if lvl is not None else "⭐ ур.—")
    parts.append(f"🎁 {gifts}" if gifts is not None else "🎁 —")
    parts.append(f"✉️ {paid}⭐" if paid else "✉️ free")
    return " · ".join(parts)


# ─── БД (JSON, users.json) ───────────────────────────────────────────────────
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_db():  return load_json(DB_FILE, {"users": []})
def save_db(d): save_json(DB_FILE, d)
def add_user(user_id: int, username: str = ""):
    data = load_db()
    ids = [u["id"] if isinstance(u, dict) else u for u in data["users"]]
    if user_id not in ids:
        data["users"].append({"id": user_id, "username": username})
        save_db(data)


def get_all_user_ids() -> list[int]:
    data = load_db()
    result = []
    for u in data["users"]:
        if isinstance(u, dict):
            result.append(u["id"])
        elif isinstance(u, (int, str)):
            try:
                result.append(int(u))
            except Exception:
                pass
    return result


def get_users_count() -> int:
    return len(get_all_user_ids())


# ═══════════════════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════════════════
def menu_kb():
    """Главное меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Парсить", callback_data="parse_menu")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
    ])


def parse_menu_kb():
    """Меню парсинга."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Рандом", callback_data="random")],
        [InlineKeyboardButton(text="🎨 Поиск по фону", callback_data="bg_menu")],
        [InlineKeyboardButton(text="🎁 Поиск по подаркам", callback_data="gift_menu")],
        [InlineKeyboardButton(text="👩 Поиск девушек", callback_data="girls")],
        [InlineKeyboardButton(text="🔓 Неулучшенные подарки", callback_data="upgradable")],
        [InlineKeyboardButton(text="⚙️ Лимит", callback_data="limit_menu")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])
def again_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Ещё", callback_data="again")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
    ])

def level_kb(prefix: str):
    """Кнопки выбора максимального уровня парса (2..10).
    prefix: 'glvl_' — подарки, 'flvl_' — девушки."""
    cycle = ("primary", "success", "danger")
    keyboard, row = [], []
    for n in range(2, 11):
        row.append(InlineKeyboardButton(text=str(n), callback_data=f"{prefix}{n}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def limit_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="10", callback_data="limit_10"),
            InlineKeyboardButton(text="20", callback_data="limit_20"),
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])


def gift_menu_kb(page: int = 0):
    cycle = ("primary", "success", "danger")
    total = len(GIFT_KEYS)
    pages = max(1, (total + GIFT_PAGE_SIZE - 1) // GIFT_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * GIFT_PAGE_SIZE
    chunk = GIFT_KEYS[start:start + GIFT_PAGE_SIZE]
    keyboard, row = [], []
    for n, key in enumerate(chunk):
        idx = start + n
        row.append(InlineKeyboardButton(text=GIFT_NAMES.get(key, key),
                                        callback_data=f"gsel_{idx}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"gpage_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"gpage_{page + 1}"))
    keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def bg_menu_kb():
    cycle = ("primary", "success", "danger")
    keyboard = []
    row = []
    for n, (i, bg) in enumerate(BG_MAP.items()):
        row.append(InlineKeyboardButton(text=bg, callback_data=f"bg_{i}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def mode_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Easy — 1 до 5 TON", callback_data="mode_easy")],
        [InlineKeyboardButton(text="🟡 Medium — 5 до 25 TON", callback_data="mode_medium")],
        [InlineKeyboardButton(text="🔴 Hard — 50 до 300 TON", callback_data="mode_hard")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])


TIER_LABELS = {
    "easy": "🟢 Easy — 1 до 5 TON",
    "medium": "🟡 Medium — 5 до 25 TON",
    "hard": "🔴 Hard — 50 до 300 TON",
}


# ═══════════════════════════════════════════════════════════════════════════
#  ПАРСЕР: ФОНЫ / РАНДОМ
# ═══════════════════════════════════════════════════════════════════════════
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


def random_entry(tier: str | None = None) -> tuple[str, int]:
    pool = NFT_TIER.get(tier, NFT_DATA) if tier else NFT_DATA
    names = list(pool.keys())
    weights = list(pool.values())
    name = random.choices(names, weights=weights, k=1)[0]
    nft_id = random.randint(1, pool[name])
    return name, nft_id


async def generate_random(limit: int, tier: str | None = None,
                          use_filter: bool = True) -> list[dict]:
    # добираем с запасом, т.к. фильтр часть отсеет
    over = limit * 3 if use_filter else limit
    entries = [random_entry(tier) for _ in range(over)]
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            bg_results, owner_results = await asyncio.wait_for(
                asyncio.gather(
                    asyncio.gather(*[fetch_bg(session, n, i) for n, i in entries]),
                    asyncio.gather(*[get_nft_owner(n, i) for n, i in entries]),
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            bg_results = [{"name": n, "id": i, "bg": None,
                           "link": f"https://t.me/nft/{n}-{i}"} for n, i in entries]
            owner_results = [None] * len(entries)

    merged = []
    for bg, owner in zip(bg_results, owner_results):
        if not bg["bg"]:
            bg["bg"] = "Unknown"
        bg["username"] = owner.get("username", "") if owner else ""
        bg["user_id"] = owner.get("user_id") if owner else None
        bg["first_name"] = owner.get("first_name", "") if owner else ""
        bg["nft_name"] = bg["name"]
        bg["nft_id"] = bg["id"]
        merged.append(bg)

    # обогащаем параллельно тех, у кого есть владелец
    enrich_tasks = [enrich_and_filter(m, use_filter=use_filter)
                    for m in merged if m.get("user_id")]
    enriched = await asyncio.gather(*enrich_tasks) if enrich_tasks else []
    results = [r for r in enriched if r]
    if not use_filter:
        results += [m for m in merged if not m.get("user_id")]
    return results[:limit]


async def generate_by_bg(target_bg: str, limit: int) -> list[dict]:
    target = target_bg.strip().lower()
    found = []
    done = asyncio.Event()
    headers = {"User-Agent": "Mozilla/5.0"}

    async def worker(session):
        while not done.is_set():
            name, nft_id = random_entry()
            r = await fetch_bg(session, name, nft_id)
            if r["bg"] and r["bg"].strip().lower() == target:
                found.append(r)
                if len(found) >= limit:
                    done.set()

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = [asyncio.create_task(worker(session)) for _ in range(50)]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=180.0)
        except asyncio.TimeoutError:
            for t in tasks:
                t.cancel()
    return found[:limit]


# ═══════════════════════════════════════════════════════════════════════════
#  ПАРСЕР: ВЛАДЕЛЬЦЫ NFT
# ═══════════════════════════════════════════════════════════════════════════
async def get_nft_owner(name: str, nft_id: int) -> dict | None:
    global pyro_client, _flood_until
    if not pyro_client:
        return None

    MAX_RETRIES = 2
    for attempt in range(MAX_RETRIES):
        now = time.monotonic()
        if _flood_until > now:
            wait = _flood_until - now
            print(f"[get_nft_owner] FLOOD active, sleep {wait:.0f}s")
            await asyncio.sleep(wait)
        try:
            async with _PYRO_SEM:
                result = await pyro_client.invoke(
                    functions.payments.GetUniqueStarGift(slug=f"{name}-{nft_id}")
                )
                gift = getattr(result, "gift", None) or result

                owner_id = None
                for attr in ("owner_id", "owner", "user_id"):
                    val = getattr(gift, attr, None)
                    if val is not None:
                        owner_id = (val.user_id if hasattr(val, "user_id")
                                    else val if isinstance(val, int) else None)
                        break
                if not owner_id:
                    return None

                if owner_id in _USER_CACHE:
                    first_name, username = _USER_CACHE[owner_id]
                else:
                    user = await pyro_client.get_users(owner_id)
                    if not user:
                        return None
                    first_name = user.first_name or ""
                    username = user.username or ""
                    _USER_CACHE[owner_id] = (first_name, username)
                    db_add_owner(owner_id, username, first_name)

                return {
                    "user_id": owner_id,
                    "first_name": first_name,
                    "username": username,
                    "nft_name": name,
                    "nft_id": nft_id,
                    "link": f"https://t.me/nft/{name}-{nft_id}",
                }
        except FloodWait as e:
            wait = e.value + 1
            print(f"[get_nft_owner] FLOODWAIT {e.value}s on {name}-{nft_id}")
            _flood_until = time.monotonic() + wait
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(wait)
                continue
            return None
        except Exception as e:
            s = str(e)
            if not any(x in s for x in ("PEER_ID_INVALID", "STARGIFT_SLUG_INVALID", "STARGIFT_ALREADY_BURNED")):
                print(f"[get_nft_owner] {name}-{nft_id}: {type(e).__name__}: {e}")
            return None
    return None


# ─── ПРОФИЛЬ (уровень / подарки / цена сообщения) ─────────────────────────────
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


async def enrich_and_filter(r: dict, *, use_filter: bool = True,
                            max_level: int | None = None) -> dict | None:
    """Добавляет level/gifts/paid_msg к найденному владельцу.
    Возвращает None, если не прошёл фильтр (при use_filter=True).
    Если задан max_level — фильтруем только по уровню (+ минимум подарков)."""
    if not r:
        return None
    prof = await get_user_profile(r["user_id"])
    if prof is None:
        if use_filter:
            return None
        r["level"] = r["gifts"] = r["paid_msg"] = None
        return r
    if use_filter:
        if max_level is not None:
            if prof["level"] > max_level:
                return None
            if prof["gifts"] < FILTER_MIN_GIFTS:
                return None
        else:
            if FILTER_MAX_LEVEL and prof["level"] > FILTER_MAX_LEVEL:
                return None
            if prof["gifts"] < FILTER_MIN_GIFTS:
                return None
            if FILTER_MAX_GIFTS and prof["gifts"] > FILTER_MAX_GIFTS:
                return None
    r["level"] = prof["level"]
    r["gifts"] = prof["gifts"]
    r["paid_msg"] = prof["paid_msg"]
    return r


async def get_seller_info(user_id: int) -> tuple[int, int] | None:
    """Тонкая обёртка над get_user_profile — единый кэш профиля."""
    p = await get_user_profile(user_id)
    return (p["level"], p["gifts"]) if p else None


# ─── ФОНОВЫЙ СБОРЩИК ВЛАДЕЛЬЦЕВ ──────────────────────────────────────────────
async def background_collector():
    """Медленно и постоянно копит владельцев NFT в базу 24/7."""
    await asyncio.sleep(10)
    while True:
        if not pyro_client:
            await asyncio.sleep(8)
            continue
        try:
            n, i = random_entry()
            await get_nft_owner(n, i)
        except Exception:
            pass
        await asyncio.sleep(8.0)


# ─── НЕУЛУЧШЕННЫЕ ПОДАРКИ + ПОИСК ДЕВУШЕК ────────────────────────────────────
async def get_users_with_upgradable_gifts(limit: int, message) -> list[dict]:
    """Поиск людей с лимитированными неулучшёнными подарками."""
    global pyro_client
    if not pyro_client:
        return []

    found = []
    last_text = ""
    last_edit = 0.0
    check_flood = 0.0
    candidates = db_get_random_owners(limit * 30)
    print(f"[upgradable] кандидатов: {len(candidates)}, всего в базе: {db_count_owners()}")
    if not candidates:
        try:
            await message.edit_text(
                "⏳ База ещё наполняется, попробуй через пару минут.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return []

    async def update_msg():
        nonlocal last_text, last_edit
        if time.monotonic() - last_edit < EDIT_INTERVAL:
            return
        text = f"⏳ Ищу... найдено <b>{len(found)}/{limit}</b>\n\n"
        for fr in found:
            if fr["username"]:
                prof = f"<a href='https://t.me/{fr['username']}'>@{fr['username']}</a>"
            else:
                prof = f"<a href='tg://user?id={fr['user_id']}'>Профиль</a>"
            text += f"{prof} — 🎁 {fr['gift_count']} шт.\n"
        if text != last_text and await safe_edit(message, text,
                                                 disable_web_page_preview=True, parse_mode="HTML"):
            last_text = text
            last_edit = time.monotonic()

    async def check(user_id, username, first_name):
        nonlocal check_flood
        cached = db_get_cache(user_id)
        if cached is not None:
            has, cnt = cached
            return {"user_id": user_id, "username": username,
                    "first_name": first_name, "gift_count": cnt} if has else None

        now = time.monotonic()
        if check_flood > now:
            await asyncio.sleep(check_flood - now)
        try:
            peer = await pyro_client.resolve_peer(user_id)
            result = await pyro_client.invoke(
                functions.payments.GetSavedStarGifts(
                    peer=peer, offset="", limit=100,
                    exclude_unique=True,
                    exclude_unupgradable=True,
                )
            )
            gifts = getattr(result, "gifts", []) or []
            cnt = 0
            for g in gifts:
                star_gift = getattr(g, "gift", None)
                if star_gift is None:
                    continue
                limited = getattr(star_gift, "limited", False)
                if limited:
                    cnt += 1
            print(f"[check] {user_id}: подходящих={cnt} (всего {len(gifts)})")
            db_set_cache(user_id, cnt > 0, cnt)
            if cnt > 0:
                return {"user_id": user_id, "username": username,
                        "first_name": first_name, "gift_count": cnt}
        except FloodWait as e:
            print(f"[check] {user_id}: FloodWait {e.value}s")
            check_flood = time.monotonic() + e.value + 1
        except Exception as e:
            print(f"[check] {user_id}: {type(e).__name__}: {e}")
        return None

    sem = asyncio.Semaphore(5)
    idx = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal idx
        while len(found) < limit:
            async with lock:
                if idx >= len(candidates):
                    return
                uid, uname, fname = candidates[idx]
                idx += 1
            async with sem:
                r = await check(uid, uname, fname)
            if r and len(found) < limit:
                found.append(r)
                await update_msg()

    tasks = [asyncio.create_task(worker()) for _ in range(10)]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=120.0)
    except asyncio.TimeoutError:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    print(f"[upgradable] финиш: найдено {len(found)}")
    return found[:limit]


# ─── ПОИСК ДЕВУШЕК ────────────────────────────────────────────────────────────
async def _find_female_inner(limit: int, message,
                             max_level: int | None = None) -> list[dict]:
    found = []
    seen_ids = set()
    last_text = ""
    last_edit = time.monotonic()
    deadline = time.monotonic() + SEARCH_TIMEOUT
    CONCURRENT = 40

    def make_owner_task():
        n, i = random_entry()
        return asyncio.create_task(get_nft_owner(n, i))

    async def render():
        nonlocal last_text, last_edit
        if time.monotonic() - last_edit < EDIT_INTERVAL:
            return
        text = f"⏳ Ищу... найдено <b>{len(found)}/{limit}</b>\n\n"
        for fr in found:
            profile = (f"@{fr['username']}" if fr["username"]
                       else f"<a href='tg://user?id={fr['user_id']}'>профиль</a>")
            text += (f"{profile} · {profile_line(fr)}\n"
                     f"<a href='{fr['link']}'>{fr['nft_name']} #{fr['nft_id']}</a>\n\n")
        if text != last_text and await safe_edit(message, text,
                                                 disable_web_page_preview=True, parse_mode="HTML"):
            last_text = text
            last_edit = time.monotonic()

    pending = {make_owner_task() for _ in range(CONCURRENT)}
    enrich_pending = set()

    while len(found) < limit and (pending or enrich_pending) and time.monotonic() < deadline:
        done_set, _ = await asyncio.wait(
            pending | enrich_pending, return_when=asyncio.FIRST_COMPLETED
        )
        pending = {t for t in pending if t not in done_set}
        enrich_pending = {t for t in enrich_pending if t not in done_set}

        for task in done_set:
            try:
                res = task.result()
            except Exception:
                continue

            if getattr(task, "_is_enrich", False):
                if res and len(found) < limit:
                    found.append(res)
                    await render()
                continue

            if len(found) + len(enrich_pending) < limit:
                pending.add(make_owner_task())
            if not res:
                continue
            uid = res.get("user_id")
            if not uid or uid in seen_ids:
                continue
            if not is_female(res.get("first_name", "")):
                continue
            seen_ids.add(uid)
            et = asyncio.create_task(enrich_and_filter(res, use_filter=True, max_level=max_level))
            et._is_enrich = True
            enrich_pending.add(et)

    for t in pending | enrich_pending:
        t.cancel()
    await asyncio.gather(*(pending | enrich_pending), return_exceptions=True)
    return found[:limit]


async def find_female_owners_live(user_id: int, limit: int, message,
                                  max_level: int | None = None) -> list[dict]:
    old = _active_search.get(user_id)
    if old and not old.done():
        old.cancel()
        try:
            await old
        except Exception:
            pass
    task = asyncio.create_task(_find_female_inner(limit, message, max_level))
    _active_search[user_id] = task
    try:
        return await task
    except asyncio.CancelledError:
        return []
    finally:
        if _active_search.get(user_id) is task:
            _active_search.pop(user_id, None)


# ─── СТАТУС «БЫЛ В СЕТИ» ─────────────────────────────────────────────────────
def _humanize_online(dt) -> str:
    try:
        now = datetime.now(dt.tzinfo) if getattr(dt, "tzinfo", None) else datetime.now()
        secs = max(0, int((now - dt).total_seconds()))
    except Exception:
        return "был(а) в сети недавно"
    if secs < 60:
        return "был(а) в сети только что"
    if secs < 3600:
        return f"был(а) в сети {secs // 60} мин назад"
    if secs < 86400:
        return f"был(а) в сети {secs // 3600} ч назад"
    return f"был(а) в сети {secs // 86400} дн назад"


def format_last_seen(user) -> str:
    status = getattr(user, "status", None)
    if status == enums.UserStatus.ONLINE:
        return "🟢 в сети"
    if status == enums.UserStatus.OFFLINE:
        last = getattr(user, "last_online_date", None)
        return "🕐 " + (_humanize_online(last) if last else "был(а) в сети недавно")
    if status == enums.UserStatus.RECENTLY:
        return "🕐 был(а) в сети недавно"
    if status == enums.UserStatus.LAST_WEEK:
        return "🕐 был(а) в сети на этой неделе"
    if status == enums.UserStatus.LAST_MONTH:
        return "🕐 был(а) в сети в этом месяце"
    if status == enums.UserStatus.LONG_AGO:
        return "🕐 был(а) в сети давно"
    return "🕐 статус скрыт"


async def fetch_last_seen(user_id: int) -> str:
    global _flood_until
    if not pyro_client:
        return "🕐 —"
    now = time.monotonic()
    if _flood_until > now:
        await asyncio.sleep(_flood_until - now)
    try:
        async with _PYRO_SEM:
            user = await pyro_client.get_users(user_id)
        return format_last_seen(user)
    except FloodWait as e:
        _flood_until = time.monotonic() + e.value + 1
        return "🕐 —"
    except Exception:
        return "🕐 —"


# ─── ПОИСК ПО ПОДАРКУ ────────────────────────────────────────────────────────
def random_gift_entry(gift_name: str) -> tuple[str, int]:
    return gift_name, random.randint(1, max(1, NFT_DATA.get(gift_name, 1000)))


async def _enrich_gift_owner(r: dict, max_level: int | None = None) -> dict | None:
    """Обогащение + статус для одного владельца (запускается параллельно)."""
    enriched = await enrich_and_filter(r, use_filter=True, max_level=max_level)
    if not enriched:
        return None
    enriched["last_seen"] = await fetch_last_seen(enriched["user_id"])
    return enriched


async def _find_gift_inner(gift_name: str, limit: int, message,
                           max_level: int | None = None) -> list[dict]:
    display = GIFT_NAMES.get(gift_name, gift_name)
    found, seen_ids = [], set()
    last_text = ""
    last_edit = time.monotonic()
    deadline = time.monotonic() + SEARCH_TIMEOUT
    CONCURRENT = 40

    def make_owner_task():
        _, i = random_gift_entry(gift_name)
        return asyncio.create_task(get_nft_owner(gift_name, i))

    async def render():
        nonlocal last_text, last_edit
        if time.monotonic() - last_edit < EDIT_INTERVAL:
            return
        text = f"🎁 <b>{display}</b> — найдено <b>{len(found)}/{limit}</b>\n\n"
        for n, fr in enumerate(found, 1):
            uname = (f"@{fr['username']}" if fr.get("username")
                     else f"<a href='tg://user?id={fr['user_id']}'>профиль</a>")
            text += (f"{n}. <a href='{fr['link']}'>{display} #{fr['nft_id']}</a> · {uname}\n"
                     f"{profile_line(fr)} · {fr.get('last_seen', '🕐 —')}\n\n")
        if text != last_text and await safe_edit(message, text,
                                                 disable_web_page_preview=True, parse_mode="HTML"):
            last_text = text
            last_edit = time.monotonic()

    pending = {make_owner_task() for _ in range(CONCURRENT)}
    enrich_pending = set()

    while len(found) < limit and (pending or enrich_pending) and time.monotonic() < deadline:
        done_set, _ = await asyncio.wait(
            pending | enrich_pending, return_when=asyncio.FIRST_COMPLETED
        )
        pending = {t for t in pending if t not in done_set}
        enrich_pending = {t for t in enrich_pending if t not in done_set}

        for task in done_set:
            try:
                res = task.result()
            except Exception:
                continue

            if getattr(task, "_is_enrich", False):
                if res and len(found) < limit:
                    found.append(res)
                    await render()
                continue

            if len(found) + len(enrich_pending) < limit:
                pending.add(make_owner_task())
            if not res:
                continue
            uid = res.get("user_id")
            if not uid or uid in seen_ids:
                continue
            seen_ids.add(uid)
            et = asyncio.create_task(_enrich_gift_owner(res, max_level))
            et._is_enrich = True
            enrich_pending.add(et)

    for t in pending | enrich_pending:
        t.cancel()
    await asyncio.gather(*(pending | enrich_pending), return_exceptions=True)
    return found[:limit]


async def find_gift_owners_live(user_id: int, gift_name: str, limit: int, message,
                                max_level: int | None = None) -> list[dict]:
    old = _active_search.get(user_id)
    if old and not old.done():
        old.cancel()
        try:
            await old
        except Exception:
            pass
    task = asyncio.create_task(_find_gift_inner(gift_name, limit, message, max_level))
    _active_search[user_id] = task
    try:
        return await task
    except asyncio.CancelledError:
        return []
    finally:
        if _active_search.get(user_id) is task:
            _active_search.pop(user_id, None)


# ═══════════════════════════════════════════════════════════════════════════
#  ХЭНДЛЕРЫ: СТАРТ / МЕНЮ / ПРОФИЛЬ
# ═══════════════════════════════════════════════════════════════════════════
@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.from_user.id, message.from_user.username or "")
    await message.answer(
        "<b>🎁 NFT Gift Parser</b>\n\nВыберите действие:",
        reply_markup=menu_kb(), parse_mode="HTML",
    )


@dp.callback_query(lambda c: c.data == "back_menu")
async def cb_back_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "<b>🎁 NFT Gift Parser</b>\n\nВыберите действие:",
        reply_markup=menu_kb(), parse_mode="HTML",
    )


@dp.callback_query(lambda c: c.data == "parse_menu")
async def cb_parse_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "<b>🎁 NFT Gift Parser</b>\n\nВыберите действие:",
        reply_markup=parse_menu_kb(), parse_mode="HTML",
    )


@dp.callback_query(lambda c: c.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    u = callback.from_user
    username = f"@{u.username}" if u.username else "—"
    text = (
        "<b>👤 Профиль</b>\n\n"
        f"Username: {username}\n"
        f"ID: <code>{u.id}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
# ═══════════════════════════════════════════════════════════════════════════
#  ХЭНДЛЕРЫ: ЛИМИТ / ФОН / РАНДОМ / ДЕВУШКИ / ПОДАРКИ
# ═══════════════════════════════════════════════════════════════════════════
# ─── ХЭНДЛЕРЫ: ЛИМИТ ──────────────────────────────────────────────────────────
@dp.callback_query(lambda c: c.data == "limit_menu")
async def cb_limit_menu(callback: CallbackQuery):
    await callback.answer()
    cur = user_limit.get(callback.from_user.id, 20)
    await callback.message.edit_text(
        f"<b>⚙️ Текущий лимит: {cur}</b>\n\nВыберите количество:",
        reply_markup=limit_kb(), parse_mode="HTML",
    )


@dp.callback_query(lambda c: c.data.startswith("limit_") and c.data != "limit_menu")
async def cb_set_limit(callback: CallbackQuery):
    await callback.answer()
    limit = int(callback.data.replace("limit_", ""))
    user_limit[callback.from_user.id] = limit
    await callback.message.edit_text(
        f"<b>✅ Лимит установлен: {limit}</b>",
        reply_markup=menu_kb(), parse_mode="HTML",
    )


# ─── ХЭНДЛЕРЫ: ФОН ────────────────────────────────────────────────────────────
@dp.callback_query(lambda c: c.data == "bg_menu")
async def cb_bg_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("<b>🎨 Выберите фон:</b>", reply_markup=bg_menu_kb(), parse_mode="HTML")


@dp.callback_query(lambda c: c.data.startswith("bg_") and c.data not in ("bg_menu",))
async def cb_bg_select(callback: CallbackQuery):
    await callback.answer()
    bg_id = callback.data.replace("bg_", "")
    bg = BG_MAP.get(bg_id)
    if not bg:
        return
    user_bg[callback.from_user.id] = bg
    user_mode[callback.from_user.id] = "bg"
    limit = user_limit.get(callback.from_user.id, 20)
    await callback.message.edit_text(f"⏳ Ищу подарки с фоном <b>{bg}</b> ({limit} шт.)...", parse_mode="HTML")
    results = await generate_by_bg(bg, limit)
    if not results:
        await callback.message.edit_text(f"❌ Ничего не найдено для фона <b>{bg}</b>.", reply_markup=again_kb(),
                                         parse_mode="HTML")
        return
    text = f"<b>🎨 Фон: {bg}</b> | Найдено: {len(results)}/{limit}\n\n"
    for item in results:
        text += f"<a href='{item['link']}'>{item['name']} #{item['id']}</a>\n\n"
    await callback.message.edit_text(text, reply_markup=again_kb(), disable_web_page_preview=True, parse_mode="HTML")


# ─── ХЭНДЛЕРЫ: РАНДОМ ─────────────────────────────────────────────────────────
@dp.callback_query(lambda c: c.data == "random")
async def cb_random(callback: CallbackQuery):
    await callback.answer()
    user_mode[callback.from_user.id] = "random"
    user_bg.pop(callback.from_user.id, None)
    await callback.message.edit_text(
        "<b>🎲 Выберите режим поиска:</b>\n\n"
        "🟢 <b>Easy</b> — Недорогие подарки от 1 до 5 TON\n\n"
        "🟡 <b>Medium</b> — Подарки от 5 до 25 TON\n\n"
        "🔴 <b>Hard</b> — Дорогие подарки от 50 до 300 TON",
        reply_markup=mode_kb(), parse_mode="HTML",
    )


@dp.callback_query(lambda c: c.data.startswith("mode_"))
async def cb_mode_select(callback: CallbackQuery):
    await callback.answer()
    tier = callback.data.replace("mode_", "")
    user_search_mode[callback.from_user.id] = tier
    limit = user_limit.get(callback.from_user.id, 20)
    label = TIER_LABELS.get(tier, "🎲 Рандом")
    await callback.message.edit_text(f"⏳ Ищу {limit} подарков [{label}]...", parse_mode="HTML")
    results = await generate_random(limit, tier=tier)
    text = f"<b>{label}</b> | {len(results)} шт.\n\n"
    for item in results:
        profile = (f"@{item['username']}" if item.get("username")
                   else f"<a href='tg://user?id={item['user_id']}'>профиль</a>" if item.get("user_id")
                   else "—")
        text += (f"<a href='{item['link']}'>{item['name']} #{item['id']}</a> — "
                 f"{item['bg']} — {profile} · {profile_line(item)}\n\n")
    await callback.message.edit_text(text, reply_markup=again_kb(), disable_web_page_preview=True, parse_mode="HTML")


# ─── ХЭНДЛЕРЫ: ДЕВУШКИ ────────────────────────────────────────────────────────
@dp.callback_query(lambda c: c.data == "girls")
async def cb_girls(callback: CallbackQuery):
    await callback.answer()
    user_mode[callback.from_user.id] = "girls"
    if not pyro_client:
        await callback.message.edit_text("❌ <b>Pyrogram не подключён.</b>", reply_markup=again_kb(), parse_mode="HTML")
        return
    await callback.message.edit_text(
        "<b>👩 Поиск девушек</b>\n\nКакой максимальный уровень парса?",
        reply_markup=level_kb("flvl_"), parse_mode="HTML",
    )


@dp.callback_query(lambda c: c.data.startswith("flvl_"))
async def cb_girls_level(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    try:
        max_level = int(callback.data.replace("flvl_", ""))
    except ValueError:
        return
    user_max_level[uid] = max_level
    user_mode[uid] = "girls"
    limit = user_limit.get(uid, 20)
    if not pyro_client:
        await callback.message.edit_text("❌ <b>Pyrogram не подключён.</b>", reply_markup=again_kb(), parse_mode="HTML")
        return
    await callback.message.edit_text(
        f"⏳ Ищу девушек (ур. ≤ {max_level})... найдено <b>0/{limit}</b>", parse_mode="HTML")
    results = await find_female_owners_live(uid, limit, callback.message, max_level=max_level)
    if not results:
        await callback.message.edit_text("Ничего не нашёл — запусти следующий запрос", reply_markup=again_kb(),
                                         parse_mode="HTML")
        return
    text = f"<b>Найдено: {len(results)}</b> (ур. ≤ {max_level})\n\n"
    for r in results:
        profile = f"@{r['username']}" if r["username"] else f"<a href='tg://user?id={r['user_id']}'>профиль</a>"
        text += (f"{profile} · {profile_line(r)}\n"
                 f"<a href='{r['link']}'>{r['nft_name']} #{r['nft_id']}</a>\n\n")
    await callback.message.edit_text(text, reply_markup=again_kb(),
                                     disable_web_page_preview=True, parse_mode="HTML")


@dp.callback_query(lambda c: c.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(lambda c: c.data == "gift_menu")
async def cb_gift_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "<b>🎁 Поиск по подаркам</b>\n\nВыберите подарок:",
        reply_markup=gift_menu_kb(0), parse_mode="HTML",
    )


@dp.callback_query(lambda c: c.data.startswith("gpage_"))
async def cb_gift_page(callback: CallbackQuery):
    await callback.answer()
    try:
        page = int(callback.data.replace("gpage_", ""))
    except ValueError:
        page = 0
    await callback.message.edit_text(
        "<b>🎁 Поиск по подаркам</b>\n\nВыберите подарок:",
        reply_markup=gift_menu_kb(page), parse_mode="HTML",
    )


@dp.callback_query(lambda c: c.data.startswith("gsel_"))
async def cb_gift_select(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    try:
        gift_name = GIFT_KEYS[int(callback.data.replace("gsel_", ""))]
    except (ValueError, IndexError):
        return
    user_gift[uid] = gift_name
    user_mode[uid] = "gift"
    display = GIFT_NAMES.get(gift_name, gift_name)
    if not pyro_client:
        await callback.message.edit_text("❌ <b>Pyrogram не подключён.</b>", reply_markup=again_kb(), parse_mode="HTML")
        return
    await callback.message.edit_text(
        f"<b>🎁 {display}</b>\n\nКакой максимальный уровень парса?",
        reply_markup=level_kb("glvl_"), parse_mode="HTML",
    )


@dp.callback_query(lambda c: c.data.startswith("glvl_"))
async def cb_gift_level(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    try:
        max_level = int(callback.data.replace("glvl_", ""))
    except ValueError:
        return
    gift_name = user_gift.get(uid)
    if not gift_name:
        await callback.message.edit_text("<b>🎁 Поиск по подаркам</b>\n\nВыберите подарок:",
                                         reply_markup=gift_menu_kb(0), parse_mode="HTML")
        return
    user_max_level[uid] = max_level
    user_mode[uid] = "gift"
    limit = user_limit.get(uid, 20)
    display = GIFT_NAMES.get(gift_name, gift_name)
    if not pyro_client:
        await callback.message.edit_text("❌ <b>Pyrogram не подключён.</b>", reply_markup=again_kb(), parse_mode="HTML")
        return
    await callback.message.edit_text(
        f"⏳ Ищу владельцев <b>{display}</b> (ур. ≤ {max_level})... <b>0/{limit}</b>", parse_mode="HTML")
    results = await find_gift_owners_live(uid, gift_name, limit, callback.message, max_level=max_level)
    if not results:
        await callback.message.edit_text("❌ Никого не нашёл — попробуй ещё раз", reply_markup=again_kb(),
                                         parse_mode="HTML")
        return
    text = f"<b>🎁 {display}</b> | суплай: {NFT_DATA.get(gift_name, '—')} | ур. ≤ {max_level} | найдено: {len(results)}\n\n"
    for n, r in enumerate(results, 1):
        uname = (f"@{r['username']}" if r.get("username")
                 else f"<a href='tg://user?id={r['user_id']}'>профиль</a>")
        text += (f"{n}. <a href='{r['link']}'>{display} #{r['nft_id']}</a> · {uname}\n"
                 f"{profile_line(r)} · {r.get('last_seen', '🕐 —')}\n\n")
    await callback.message.edit_text(text, reply_markup=again_kb(), disable_web_page_preview=True, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════
#  ХЭНДЛЕР: НЕУЛУЧШЕННЫЕ ПОДАРКИ (доступно всем, без подписки)
# ═══════════════════════════════════════════════════════════════════════════
@dp.callback_query(lambda c: c.data == "upgradable")
async def cb_upgradable(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    user_mode[uid] = "upgradable"
    limit = user_limit.get(uid, 20)

    if not pyro_client:
        await callback.message.edit_text(
            "❌ <b>Pyrogram не подключён.</b>",
            reply_markup=again_kb(), parse_mode="HTML",
        )
        return

    await callback.message.edit_text(
        f"⏳ Ищу пользователей с неулучшёнными подарками... <b>0/{limit}</b>",
        parse_mode="HTML",
    )
    results = await get_users_with_upgradable_gifts(limit, callback.message)
    if not results:
        await callback.message.edit_text(
            "❌ Ничего не найдено — попробуй ещё раз",
            reply_markup=again_kb(), parse_mode="HTML",
        )
        return

    text = f"<b>🔓 Неулучшённые подарки | Найдено: {len(results)}</b>\n\n"
    for r in results:
        if r["username"]:
            profile = f"<a href='https://t.me/{r['username']}'>@{r['username']}</a>"
        else:
            profile = f"<a href='tg://user?id={r['user_id']}'>{r.get('first_name', 'Профиль')}</a>"
        text += f"{profile} — 🎁 {r['gift_count']} шт.\n"

    await callback.message.edit_text(
        text, reply_markup=again_kb(),
        disable_web_page_preview=True, parse_mode="HTML",
    )
# ═══════════════════════════════════════════════════════════════════════════
#  АДМИН: РАССЫЛКА
# ═══════════════════════════════════════════════════════════════════════════
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    count = get_users_count()
    await message.answer(
        f"📢 <b>Рассылка</b>\n"
        f"Юзеров в базе: <b>{count}</b>\n\n"
        f"Отправь сообщение для рассылки:",
        parse_mode="HTML",
    )
    await state.set_state(BroadcastState.waiting_message)


@dp.message(BroadcastState.waiting_message)
async def do_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()

    users = get_all_user_ids()
    sent = failed = 0
    status = await message.answer(
        f"⏳ Начинаю рассылку на <b>{len(users)}</b> юзеров...",
        parse_mode="HTML",
    )

    for user_id in users:
        try:
            await message.copy_to(user_id)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
        if (sent + failed) % 50 == 0:
            try:
                await status.edit_text(
                    f"⏳ Прогресс: <b>{sent + failed}/{len(users)}</b>\n"
                    f"✅ Отправлено: {sent}\n❌ Ошибок: {failed}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    await status.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n"
        f"📤 Отправлено: {sent}\n❌ Не доставлено: {failed}",
        parse_mode="HTML",
    )


# ─── ХЭНДЛЕР: ЕЩЁ ────────────────────────────────────────────────────────────
@dp.callback_query(lambda c: c.data == "again")
async def cb_again(callback: CallbackQuery):
    await callback.answer()
    mode = user_mode.get(callback.from_user.id, "random")
    limit = user_limit.get(callback.from_user.id, 20)
    uid = callback.from_user.id

    if mode == "bg":
        bg = user_bg.get(uid)
        if not bg:
            await callback.message.edit_text("<b>🎁 NFT Gift Parser</b>\n\nВыберите действие:", reply_markup=menu_kb(),
                                             parse_mode="HTML")
            return
        await callback.message.edit_text(f"⏳ Снова ищу по фону <b>{bg}</b>...", parse_mode="HTML")
        results = await generate_by_bg(bg, limit)
        if not results:
            await callback.message.edit_text(f"❌ Ничего не найдено для фона <b>{bg}</b>.", reply_markup=again_kb(),
                                             parse_mode="HTML")
            return
        text = f"<b>🎨 Фон: {bg}</b> | Найдено: {len(results)}/{limit}\n\n"
        for item in results:
            text += f"<a href='{item['link']}'>{item['name']} #{item['id']}</a>\n\n"
        await callback.message.edit_text(text, reply_markup=again_kb(), disable_web_page_preview=True,
                                         parse_mode="HTML")

    elif mode == "girls":
        if not pyro_client:
            await callback.message.edit_text("❌ <b>Pyrogram не подключён.</b>", reply_markup=again_kb(),
                                             parse_mode="HTML")
            return
        max_level = user_max_level.get(uid)
        await callback.message.edit_text(f"⏳ Ищу... найдено <b>0/{limit}</b>", parse_mode="HTML")
        results = await find_female_owners_live(uid, limit, callback.message, max_level=max_level)
        if not results:
            await callback.message.edit_text("Ничего не нашёл — запусти следующий запрос", reply_markup=again_kb(),
                                             parse_mode="HTML")
            return
        text = f"<b>Найдено: {len(results)}</b>\n\n"
        for r in results:
            profile = f"@{r['username']}" if r["username"] else f"<a href='tg://user?id={r['user_id']}'>профиль</a>"
            text += (f"{profile} · {profile_line(r)}\n"
                     f"<a href='{r['link']}'>{r['nft_name']} #{r['nft_id']}</a>\n\n")
        await callback.message.edit_text(text, reply_markup=again_kb(), disable_web_page_preview=True,
                                         parse_mode="HTML")

    elif mode == "upgradable":
        if not pyro_client:
            await callback.message.edit_text(
                "❌ <b>Pyrogram не подключён.</b>",
                reply_markup=again_kb(), parse_mode="HTML",
            )
            return
        await callback.message.edit_text(f"⏳ Ищу... <b>0/{limit}</b>", parse_mode="HTML")
        results = await get_users_with_upgradable_gifts(limit, callback.message)
        if not results:
            await callback.message.edit_text(
                "❌ Ничего не найдено — попробуй ещё раз",
                reply_markup=again_kb(), parse_mode="HTML",
            )
            return
        text = f"<b>🔓 Неулучшённые подарки | Найдено: {len(results)}</b>\n\n"
        for r in results:
            if r["username"]:
                profile = f"<a href='https://t.me/{r['username']}'>@{r['username']}</a>"
            else:
                profile = f"<a href='tg://user?id={r['user_id']}'>{r.get('first_name', 'Профиль')}</a>"
            text += f"{profile} — 🎁 {r['gift_count']} шт.\n"
        await callback.message.edit_text(
            text, reply_markup=again_kb(),
            disable_web_page_preview=True, parse_mode="HTML",
        )

    elif mode == "gift":
        gift_name = user_gift.get(uid)
        if not gift_name:
            await callback.message.edit_text("<b>🎁 Поиск по подаркам</b>\n\nВыберите подарок:",
                                             reply_markup=gift_menu_kb(0), parse_mode="HTML")
            return
        if not pyro_client:
            await callback.message.edit_text("❌ <b>Pyrogram не подключён.</b>", reply_markup=again_kb(),
                                             parse_mode="HTML")
            return
        display = GIFT_NAMES.get(gift_name, gift_name)
        max_level = user_max_level.get(uid)
        await callback.message.edit_text(f"⏳ Снова ищу владельцев <b>{display}</b>... <b>0/{limit}</b>",
                                         parse_mode="HTML")
        results = await find_gift_owners_live(uid, gift_name, limit, callback.message, max_level=max_level)
        if not results:
            await callback.message.edit_text("❌ Никого не нашёл — попробуй ещё раз", reply_markup=again_kb(),
                                             parse_mode="HTML")
            return
        text = f"<b>🎁 {display}</b> | суплай: {NFT_DATA.get(gift_name, '—')} | найдено: {len(results)}\n\n"
        for n, r in enumerate(results, 1):
            uname = (f"@{r['username']}" if r.get("username")
                     else f"<a href='tg://user?id={r['user_id']}'>профиль</a>")
            text += (f"{n}. <a href='{r['link']}'>{display} #{r['nft_id']}</a> · {uname}\n"
                     f"{profile_line(r)} · {r.get('last_seen', '🕐 —')}\n\n")
        await callback.message.edit_text(text, reply_markup=again_kb(), disable_web_page_preview=True,
                                         parse_mode="HTML")

    else:  # random
        tier = user_search_mode.get(uid)
        label = TIER_LABELS.get(tier, "🎲 Рандом")
        await callback.message.edit_text(f"⏳ Ищу {limit} подарков [{label}]...", parse_mode="HTML")
        results = await generate_random(limit, tier=tier)
        text = f"<b>{label}</b> | {len(results)} шт.\n\n"
        for item in results:
            profile = (f"@{item['username']}" if item.get("username")
                       else f"<a href='tg://user?id={item['user_id']}'>профиль</a>" if item.get("user_id")
                       else "—")
            text += (f"<a href='{item['link']}'>{item['name']} #{item['id']}</a> — "
                     f"{item['bg']} — {profile} · {profile_line(item)}\n\n")
        await callback.message.edit_text(text, reply_markup=again_kb(), disable_web_page_preview=True,
                                         parse_mode="HTML")


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
    global bot, pyro_client

    if not PARSER_BOT_TOKEN or "ВСТАВЬ" in PARSER_BOT_TOKEN:
        print("❌ Укажи PARSER_BOT_TOKEN в начале файла (токен из @BotFather).", flush=True)
        return

    init_db()
    print(f"✅ БД: {OWNERS_DB}", flush=True)

    bot = Bot(token=PARSER_BOT_TOKEN)

    # ── Pyrogram-пул ──
    if API_ID and API_HASH and POOL_SESSIONS:
        pyro_client = SessionProxy()
        started = await pyro_client.start_all()
        if started:
            print(f"✅ Pyrogram: запущено сессий — {started}/{len(POOL_SESSIONS)}", flush=True)
            # asyncio.create_task(background_collector())   # копилка владельцев 24/7
        else:
            print("⚠️  Ни одна сессия не запущена — проверь .session файлы (auth.py)", flush=True)
            pyro_client = None
    else:
        print("⚠️  POOL_SESSIONS пуст — добавь 2-3 сессии в начале файла", flush=True)

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
