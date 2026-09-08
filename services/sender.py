import logging
from datetime import timedelta

from sqlalchemy import select

from database import get_session
from models import Recipient, SentLog
from services.account_manager import get_client, Account
from utils.helpers import now_utc
import config

logger = logging.getLogger(__name__)


async def is_duplicate(account_id: int, target: str, campaign_id: int) -> bool:
    cutoff = now_utc() - timedelta(days=config.DUPLICATE_CHECK_DAYS)
    async with await get_session() as session:
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


async def send_message(
    account: Account,
    recipient: Recipient,
    text: str,
    campaign_id: int,
) -> bool:
    if await is_duplicate(account.id, recipient.target, campaign_id):
        logger.info(
            "Duplicate skipped: account=%d target=%s campaign=%d",
            account.id, recipient.target, campaign_id,
        )
        async with await get_session() as session:
            r = await session.get(Recipient, recipient.id)
            if r:
                r.status = "duplicate"
                await session.commit()
        return False

    client = get_client(account)
    try:
        await client.connect()
        target = recipient.target
        if target.isdigit():
            target = int(target)

        await client.send_message(target, text, parse_mode="md")

        ts = now_utc()
        async with await get_session() as session:
            r = await session.get(Recipient, recipient.id)
            if r:
                r.status = "sent"
                r.sent_at = ts
                await session.commit()

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
            "Sent: account=%d -> %s (campaign=%d)",
            account.id, recipient.target, campaign_id,
        )
        return True

    except Exception as exc:
        logger.error(
            "Send error: account=%d target=%s err=%s",
            account.id, recipient.target, exc,
        )
        async with await get_session() as session:
            r = await session.get(Recipient, recipient.id)
            if r:
                r.status = "error"
                r.error_message = str(exc)[:500]
                await session.commit()
        return False

    finally:
        await client.disconnect()
