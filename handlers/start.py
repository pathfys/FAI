from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

router = Router()


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Accounts", callback_data="menu:accounts"),
         InlineKeyboardButton(text="Campaigns", callback_data="menu:campaigns")],
        [InlineKeyboardButton(text="Help", callback_data="menu:help")],
    ])


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Welcome to Crypto Broadcast Bot\n\n"
        "Manage your accounts and broadcast campaigns from here.",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(lambda c: c.data == "menu:help")
async def cb_help(callback) -> None:
    await callback.message.edit_text(
        "Commands:\n"
        "/add_account - Add Telegram account\n"
        "/list_accounts - List accounts\n"
        "/remove_account <id> - Remove account\n"
        "/create_campaign - Create campaign\n"
        "/list_campaigns - List campaigns\n"
        "/start_campaign <id> - Start campaign\n"
        "/stop_campaign <id> - Stop campaign\n"
        "/campaign_stats <id> - Campaign stats\n"
        "/export_logs <id> - Export send logs\n"
        "/edit_campaign <id> - Edit draft campaign",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "menu:main")
async def cb_main_menu(callback) -> None:
    await callback.message.edit_text(
        "Main menu", reply_markup=main_menu_kb()
    )
    await callback.answer()
