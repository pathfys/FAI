import asyncio
import logging
from itertools import cycle

from sqlalchemy import select, func

from database import get_session
from models import Account, Campaign, CampaignAccount, Recipient, SentLog
from services.sender import send_message
from utils.helpers import now_utc

logger = logging.getLogger(__name__)

_running_tasks: dict[int, asyncio.Task] = {}
_stop_events: dict[int, asyncio.Event] = {}


async def create_campaign(
    name: str,
    text: str,
    interval_seconds: int,
    account_ids: list[int],
    targets: list[str],
) -> Campaign:
    async with await get_session() as session:
        campaign = Campaign(
            name=name,
            text=text,
            interval_seconds=interval_seconds,
            status="draft",
        )
        session.add(campaign)
        await session.flush()

        for aid in account_ids:
            session.add(CampaignAccount(campaign_id=campaign.id, account_id=aid))

        for t in targets:
            session.add(Recipient(campaign_id=campaign.id, target=t, status="pending"))

        await session.commit()
        await session.refresh(campaign)
        logger.info("Campaign created: id=%d name=%s", campaign.id, name)
        return campaign


async def list_campaigns() -> list[Campaign]:
    async with await get_session() as session:
        result = await session.execute(select(Campaign).order_by(Campaign.id.desc()))
        return list(result.scalars().all())


async def get_campaign(campaign_id: int) -> Campaign | None:
    async with await get_session() as session:
        return await session.get(Campaign, campaign_id)


async def update_campaign(campaign_id: int, **kwargs) -> bool:
    async with await get_session() as session:
        campaign = await session.get(Campaign, campaign_id)
        if not campaign or campaign.status not in ("draft", "paused"):
            return False
        for key, value in kwargs.items():
            if hasattr(campaign, key):
                setattr(campaign, key, value)
        await session.commit()
        return True


async def get_campaign_accounts(campaign_id: int) -> list[Account]:
    async with await get_session() as session:
        stmt = (
            select(Account)
            .join(CampaignAccount, CampaignAccount.account_id == Account.id)
            .where(CampaignAccount.campaign_id == campaign_id, Account.is_active.is_(True))
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_pending_recipients(campaign_id: int) -> list[Recipient]:
    async with await get_session() as session:
        stmt = (
            select(Recipient)
            .where(
                Recipient.campaign_id == campaign_id,
                Recipient.status == "pending",
            )
            .order_by(Recipient.id)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def campaign_stats(campaign_id: int) -> dict:
    async with await get_session() as session:
        total = await session.scalar(
            select(func.count()).select_from(Recipient).where(Recipient.campaign_id == campaign_id)
        )
        sent = await session.scalar(
            select(func.count()).select_from(Recipient).where(
                Recipient.campaign_id == campaign_id, Recipient.status == "sent"
            )
        )
        errors = await session.scalar(
            select(func.count()).select_from(Recipient).where(
                Recipient.campaign_id == campaign_id, Recipient.status == "error"
            )
        )
        duplicates = await session.scalar(
            select(func.count()).select_from(Recipient).where(
                Recipient.campaign_id == campaign_id, Recipient.status == "duplicate"
            )
        )
        pending = await session.scalar(
            select(func.count()).select_from(Recipient).where(
                Recipient.campaign_id == campaign_id, Recipient.status == "pending"
            )
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


async def export_campaign_logs(campaign_id: int) -> list[dict]:
    async with await get_session() as session:
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


async def _run_campaign(campaign_id: int) -> None:
    stop_event = _stop_events.get(campaign_id)
    if not stop_event:
        return

    campaign = await get_campaign(campaign_id)
    if not campaign:
        return

    accounts = await get_campaign_accounts(campaign_id)
    if not accounts:
        logger.error("Campaign %d has no active accounts", campaign_id)
        async with await get_session() as session:
            c = await session.get(Campaign, campaign_id)
            if c:
                c.status = "paused"
                await session.commit()
        return

    account_cycle = cycle(accounts)

    while not stop_event.is_set():
        pending = await get_pending_recipients(campaign_id)
        if not pending:
            break

        recipient = pending[0]
        account = next(account_cycle)

        await send_message(account, recipient, campaign.text, campaign_id)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=campaign.interval_seconds)
            break
        except asyncio.TimeoutError:
            pass

    async with await get_session() as session:
        c = await session.get(Campaign, campaign_id)
        if c:
            if stop_event.is_set():
                c.status = "paused"
            else:
                c.status = "finished"
            await session.commit()

    _stop_events.pop(campaign_id, None)
    _running_tasks.pop(campaign_id, None)
    logger.info("Campaign %d ended (status update committed)", campaign_id)


async def start_campaign(campaign_id: int) -> str | None:
    if campaign_id in _running_tasks:
        return "Campaign is already running."

    campaign = await get_campaign(campaign_id)
    if not campaign:
        return "Campaign not found."
    if campaign.status == "finished":
        return "Campaign is already finished."

    accounts = await get_campaign_accounts(campaign_id)
    if not accounts:
        return "No active accounts linked to this campaign."

    pending = await get_pending_recipients(campaign_id)
    if not pending:
        return "No pending recipients."

    async with await get_session() as session:
        c = await session.get(Campaign, campaign_id)
        if c:
            c.status = "active"
            c.started_at = now_utc()
            await session.commit()

    stop_event = asyncio.Event()
    _stop_events[campaign_id] = stop_event
    task = asyncio.create_task(_run_campaign(campaign_id))
    _running_tasks[campaign_id] = task
    return None


async def stop_campaign(campaign_id: int) -> str | None:
    if campaign_id not in _running_tasks:
        return "Campaign is not running."
    _stop_events[campaign_id].set()
    try:
        await asyncio.wait_for(_running_tasks[campaign_id], timeout=30)
    except asyncio.TimeoutError:
        _running_tasks[campaign_id].cancel()
    return None
