import io
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services import account_manager, campaign_manager
from utils.helpers import export_logs_csv, parse_recipients
import config

logger = logging.getLogger(__name__)
router = Router()


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


# --- campaigns menu ---

@router.callback_query(lambda c: c.data == "menu:campaigns")
async def cb_campaigns_menu(callback: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Create campaign", callback_data="cmp:create"),
         InlineKeyboardButton(text="List campaigns", callback_data="cmp:list")],
        [InlineKeyboardButton(text="Back", callback_data="menu:main")],
    ])
    await callback.message.edit_text("Campaigns management", reply_markup=kb)
    await callback.answer()


# --- create campaign ---

@router.callback_query(lambda c: c.data == "cmp:create")
async def cb_create_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Enter campaign name:")
    await state.set_state(CreateCampaignStates.name)
    await callback.answer()


@router.message(Command("create_campaign"))
async def cmd_create_campaign(message: Message, state: FSMContext) -> None:
    await message.answer("Enter campaign name:")
    await state.set_state(CreateCampaignStates.name)


@router.message(CreateCampaignStates.name)
async def process_campaign_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    accounts = await account_manager.list_accounts()
    if not accounts:
        await message.answer("No accounts available. Add accounts first.")
        await state.clear()
        return

    await state.update_data(selected_accounts=[])
    kb = _build_account_select_kb(accounts, [])
    await message.answer("Select accounts for this campaign:", reply_markup=kb)
    await state.set_state(CreateCampaignStates.accounts)


def _build_account_select_kb(
    accounts: list, selected: list[int]
) -> InlineKeyboardMarkup:
    rows = []
    for a in accounts:
        mark = "[x]" if a.id in selected else "[ ]"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {a.phone}", callback_data=f"cmp:sel:{a.id}"
        )])
    rows.append([InlineKeyboardButton(text="Done", callback_data="cmp:sel:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(
    CreateCampaignStates.accounts, F.data.startswith("cmp:sel:")
)
async def cb_select_account(callback: CallbackQuery, state: FSMContext) -> None:
    part = callback.data.split(":")[2]
    if part == "done":
        data = await state.get_data()
        selected = data.get("selected_accounts", [])
        if not selected:
            await callback.answer("Select at least one account.")
            return
        await callback.message.edit_text(
            "Enter the message text (Markdown supported, emojis allowed):"
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

    accounts = await account_manager.list_accounts()
    kb = _build_account_select_kb(accounts, selected)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


@router.message(CreateCampaignStates.text)
async def process_campaign_text(message: Message, state: FSMContext) -> None:
    await state.update_data(text=message.text)
    await message.answer(
        f"Enter interval between messages in seconds "
        f"(minimum {config.MIN_INTERVAL_SECONDS}):"
    )
    await state.set_state(CreateCampaignStates.interval)


@router.message(CreateCampaignStates.interval)
async def process_campaign_interval(message: Message, state: FSMContext) -> None:
    try:
        interval = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Must be a number. Try again:")
        return
    if interval < config.MIN_INTERVAL_SECONDS:
        await message.answer(
            f"Minimum interval is {config.MIN_INTERVAL_SECONDS}s. Try again:"
        )
        return
    await state.update_data(interval=interval)
    await message.answer(
        "Enter recipients (usernames or user IDs), comma-separated.\n"
        "Or send a .txt file with one per line."
    )
    await state.set_state(CreateCampaignStates.recipients)


@router.message(CreateCampaignStates.recipients)
async def process_campaign_recipients(message: Message, state: FSMContext) -> None:
    targets: list[str] = []

    if message.document and message.document.file_name and message.document.file_name.endswith(".txt"):
        file = await message.bot.download(message.document)
        if file:
            content = file.read().decode("utf-8", errors="replace")
            targets = parse_recipients(content)
    elif message.text:
        targets = parse_recipients(message.text)

    if not targets:
        await message.answer("No valid recipients found. Try again:")
        return

    await state.update_data(targets=targets)
    data = await state.get_data()

    preview = (
        f"Campaign: {data['name']}\n"
        f"Accounts: {len(data['selected_accounts'])}\n"
        f"Interval: {data['interval']}s\n"
        f"Recipients: {len(targets)}\n\n"
        f"Message preview:\n{data['text']}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm", callback_data="cmp:confirm"),
         InlineKeyboardButton(text="Cancel", callback_data="cmp:cancel")],
    ])
    await message.answer(preview, reply_markup=kb)
    await state.set_state(CreateCampaignStates.confirm)


@router.callback_query(
    CreateCampaignStates.confirm, F.data == "cmp:confirm"
)
async def cb_confirm_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    campaign = await campaign_manager.create_campaign(
        name=data["name"],
        text=data["text"],
        interval_seconds=data["interval"],
        account_ids=data["selected_accounts"],
        targets=data["targets"],
    )
    await callback.message.edit_text(
        f"Campaign created (id={campaign.id}).\n"
        f"Use /start_campaign {campaign.id} to launch."
    )
    await state.clear()
    await callback.answer()


@router.callback_query(
    CreateCampaignStates.confirm, F.data == "cmp:cancel"
)
async def cb_cancel_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Campaign creation cancelled.")
    await state.clear()
    await callback.answer()


# --- list campaigns ---

@router.message(Command("list_campaigns"))
async def cmd_list_campaigns(message: Message) -> None:
    await _show_campaigns(message)


@router.callback_query(lambda c: c.data == "cmp:list")
async def cb_list_campaigns(callback: CallbackQuery) -> None:
    await _show_campaigns(callback.message, edit=True)
    await callback.answer()


async def _show_campaigns(message: Message, edit: bool = False) -> None:
    campaigns = await campaign_manager.list_campaigns()
    if not campaigns:
        text = "No campaigns."
    else:
        lines = []
        for c in campaigns:
            lines.append(f"[{c.id}] {c.name} — {c.status}")
        text = "Campaigns:\n" + "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back", callback_data="menu:campaigns")]
    ])
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


# --- start / stop / stats ---

@router.message(Command("start_campaign"))
async def cmd_start_campaign(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /start_campaign <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID must be a number.")
        return

    campaign = await campaign_manager.get_campaign(cid)
    if not campaign:
        await message.answer("Campaign not found.")
        return

    stats = await campaign_manager.campaign_stats(cid)
    preview = (
        f"Start campaign [{cid}] \"{campaign.name}\"?\n"
        f"Recipients: {stats['total']} (pending: {stats['pending']})\n\n"
        f"Message preview:\n{campaign.text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Start", callback_data=f"cmp:start:{cid}"),
         InlineKeyboardButton(text="Cancel", callback_data="menu:campaigns")],
    ])
    await message.answer(preview, reply_markup=kb)


@router.callback_query(F.data.startswith("cmp:start:"))
async def cb_start_campaign(callback: CallbackQuery) -> None:
    cid = int(callback.data.split(":")[2])
    err = await campaign_manager.start_campaign(cid)
    if err:
        await callback.message.edit_text(f"Error: {err}")
    else:
        await callback.message.edit_text(f"Campaign {cid} started.")
    await callback.answer()


@router.message(Command("stop_campaign"))
async def cmd_stop_campaign(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /stop_campaign <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID must be a number.")
        return
    err = await campaign_manager.stop_campaign(cid)
    if err:
        await message.answer(f"Error: {err}")
    else:
        await message.answer(f"Campaign {cid} stopped.")


@router.message(Command("campaign_stats"))
async def cmd_campaign_stats(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /campaign_stats <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID must be a number.")
        return

    campaign = await campaign_manager.get_campaign(cid)
    if not campaign:
        await message.answer("Campaign not found.")
        return

    stats = await campaign_manager.campaign_stats(cid)
    text = (
        f"Campaign [{cid}] \"{campaign.name}\" ({campaign.status})\n\n"
        f"Total: {stats['total']}\n"
        f"Sent: {stats['sent']}\n"
        f"Errors: {stats['errors']}\n"
        f"Duplicates: {stats['duplicates']}\n"
        f"Pending: {stats['pending']}\n"
    )
    if stats["recent"]:
        text += "\nRecent:\n"
        for r in stats["recent"]:
            text += f"  {r['target']} at {r['sent_at']}\n"

    await message.answer(text)


# --- export logs ---

@router.message(Command("export_logs"))
async def cmd_export_logs(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /export_logs <campaign_id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID must be a number.")
        return

    rows = await campaign_manager.export_campaign_logs(cid)
    if not rows:
        await message.answer("No logs for this campaign.")
        return

    csv_text = export_logs_csv(rows)
    doc = BufferedInputFile(
        csv_text.encode("utf-8"), filename=f"campaign_{cid}_logs.csv"
    )
    await message.answer_document(doc, caption=f"Logs for campaign {cid}")


# --- edit campaign ---

@router.message(Command("edit_campaign"))
async def cmd_edit_campaign(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /edit_campaign <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID must be a number.")
        return

    campaign = await campaign_manager.get_campaign(cid)
    if not campaign:
        await message.answer("Campaign not found.")
        return
    if campaign.status not in ("draft", "paused"):
        await message.answer("Only draft or paused campaigns can be edited.")
        return

    await state.update_data(edit_campaign_id=cid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Edit text", callback_data="cmp:edit:text"),
         InlineKeyboardButton(text="Edit interval", callback_data="cmp:edit:interval")],
        [InlineKeyboardButton(text="Cancel", callback_data="menu:campaigns")],
    ])
    await message.answer(
        f"Editing campaign [{cid}] \"{campaign.name}\".\n"
        f"Current interval: {campaign.interval_seconds}s\n"
        f"Current text:\n{campaign.text}",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("cmp:edit:"))
async def cb_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":")[2]
    await state.update_data(edit_field=field)
    if field == "text":
        await callback.message.edit_text("Enter new message text:")
    else:
        await callback.message.edit_text(
            f"Enter new interval in seconds (min {config.MIN_INTERVAL_SECONDS}):"
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
            await message.answer("Must be a number.")
            return
        if val < config.MIN_INTERVAL_SECONDS:
            await message.answer(f"Minimum is {config.MIN_INTERVAL_SECONDS}s.")
            return
        ok = await campaign_manager.update_campaign(cid, interval_seconds=val)
    else:
        ok = await campaign_manager.update_campaign(cid, text=message.text)

    if ok:
        await message.answer(f"Campaign {cid} updated.")
    else:
        await message.answer("Update failed (campaign may be active).")
    await state.clear()
