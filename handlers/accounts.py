import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telethon import TelegramClient
from telethon.sessions import StringSession

from services import account_manager

logger = logging.getLogger(__name__)
router = Router()


class AddAccountStates(StatesGroup):
    api_id = State()
    api_hash = State()
    phone = State()
    code = State()
    password = State()


_pending_clients: dict[int, dict] = {}


@router.callback_query(lambda c: c.data == "menu:accounts")
async def cb_accounts_menu(callback: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Add account", callback_data="acc:add"),
         InlineKeyboardButton(text="List accounts", callback_data="acc:list")],
        [InlineKeyboardButton(text="Back", callback_data="menu:main")],
    ])
    await callback.message.edit_text("Accounts management", reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data == "acc:add")
async def cb_add_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Enter api_id:")
    await state.set_state(AddAccountStates.api_id)
    await callback.answer()


@router.message(Command("add_account"))
async def cmd_add_account(message: Message, state: FSMContext) -> None:
    await message.answer("Enter api_id:")
    await state.set_state(AddAccountStates.api_id)


@router.message(AddAccountStates.api_id)
async def process_api_id(message: Message, state: FSMContext) -> None:
    try:
        api_id = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("api_id must be a number. Try again:")
        return
    await state.update_data(api_id=api_id)
    await message.answer("Enter api_hash:")
    await state.set_state(AddAccountStates.api_hash)


@router.message(AddAccountStates.api_hash)
async def process_api_hash(message: Message, state: FSMContext) -> None:
    await state.update_data(api_hash=message.text.strip())
    await message.answer("Enter phone number (e.g. +79001234567):")
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
        await message.answer("Code sent. Enter the verification code:")
        await state.set_state(AddAccountStates.code)
    except Exception as exc:
        logger.error("Failed to send code: %s", exc)
        await message.answer(f"Error sending code: {exc}")
        await client.disconnect()
        await state.clear()


@router.message(AddAccountStates.code)
async def process_code(message: Message, state: FSMContext) -> None:
    info = _pending_clients.get(message.from_user.id)
    if not info:
        await message.answer("Session expired. Start /add_account again.")
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
        if "SessionPasswordNeeded" in error_name or "password" in str(exc).lower():
            await message.answer("2FA is enabled. Enter your password:")
            await state.set_state(AddAccountStates.password)
            return
        logger.error("Sign-in failed: %s", exc)
        await message.answer(f"Sign-in error: {exc}")
        await client.disconnect()
        _pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return

    session_string = client.session.save()
    await client.disconnect()
    _pending_clients.pop(message.from_user.id, None)

    account = await account_manager.add_account(
        info["api_id"], info["api_hash"], info["phone"], session_string
    )
    await message.answer(f"Account added (id={account.id}, phone={account.phone}).")
    await state.clear()


@router.message(AddAccountStates.password)
async def process_password(message: Message, state: FSMContext) -> None:
    info = _pending_clients.get(message.from_user.id)
    if not info:
        await message.answer("Session expired. Start /add_account again.")
        await state.clear()
        return

    client: TelegramClient = info["client"]
    try:
        await client.sign_in(password=message.text.strip())
    except Exception as exc:
        logger.error("2FA sign-in failed: %s", exc)
        await message.answer(f"2FA error: {exc}")
        await client.disconnect()
        _pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return

    session_string = client.session.save()
    await client.disconnect()
    _pending_clients.pop(message.from_user.id, None)

    account = await account_manager.add_account(
        info["api_id"], info["api_hash"], info["phone"], session_string
    )
    await message.answer(f"Account added (id={account.id}, phone={account.phone}).")
    await state.clear()


@router.message(Command("list_accounts"))
async def cmd_list_accounts(message: Message) -> None:
    await _show_accounts(message)


@router.callback_query(lambda c: c.data == "acc:list")
async def cb_list_accounts(callback: CallbackQuery) -> None:
    await _show_accounts(callback.message, edit=True)
    await callback.answer()


async def _show_accounts(message: Message, edit: bool = False) -> None:
    accounts = await account_manager.list_accounts()
    if not accounts:
        text = "No accounts added yet."
    else:
        lines = []
        for a in accounts:
            status = "active" if a.is_active else "inactive"
            lines.append(f"[{a.id}] {a.phone} — {status}")
        text = "Accounts:\n" + "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back", callback_data="menu:accounts")]
    ])
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.message(Command("remove_account"))
async def cmd_remove_account(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /remove_account <id>")
        return
    try:
        account_id = int(parts[1])
    except ValueError:
        await message.answer("ID must be a number.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm", callback_data=f"acc:rm:{account_id}"),
         InlineKeyboardButton(text="Cancel", callback_data="menu:accounts")],
    ])
    await message.answer(f"Remove account {account_id}?", reply_markup=kb)


@router.callback_query(F.data.startswith("acc:rm:"))
async def cb_confirm_remove(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[2])
    removed = await account_manager.remove_account(account_id)
    if removed:
        await callback.message.edit_text(f"Account {account_id} removed.")
    else:
        await callback.message.edit_text("Account not found.")
    await callback.answer()
