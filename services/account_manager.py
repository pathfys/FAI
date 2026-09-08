import logging

from sqlalchemy import select
from telethon import TelegramClient
from telethon.sessions import StringSession

from database import get_session
from models import Account
from utils.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)


async def add_account(api_id: int, api_hash: str, phone: str, session_string: str) -> Account:
    async with await get_session() as session:
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
        logger.info("Account added: id=%d phone=%s", account.id, phone)
        return account


async def list_accounts() -> list[Account]:
    async with await get_session() as session:
        result = await session.execute(select(Account).order_by(Account.id))
        return list(result.scalars().all())


async def get_account(account_id: int) -> Account | None:
    async with await get_session() as session:
        return await session.get(Account, account_id)


async def remove_account(account_id: int) -> bool:
    async with await get_session() as session:
        account = await session.get(Account, account_id)
        if not account:
            return False
        await session.delete(account)
        await session.commit()
        logger.info("Account removed: id=%d", account_id)
        return True


async def toggle_account(account_id: int, active: bool) -> bool:
    async with await get_session() as session:
        account = await session.get(Account, account_id)
        if not account:
            return False
        account.is_active = active
        await session.commit()
        return True


def get_client(account: Account) -> TelegramClient:
    api_id = int(decrypt(account.api_id))
    api_hash = decrypt(account.api_hash)
    session_str = decrypt(account.session_string)
    return TelegramClient(StringSession(session_str), api_id, api_hash)


async def check_session_valid(api_id: int, api_hash: str, session_string: str) -> bool:
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False
        return True
    except Exception:
        logger.exception("Session validation failed")
        return False
    finally:
        await client.disconnect()
