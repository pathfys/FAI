# Telegram Crypto Broadcast Bot

Telegram-bot for managing broadcast campaigns via multiple user accounts.

## Requirements

- Python 3.10+
- Telegram Bot Token (from @BotFather)
- Telegram API credentials (api_id / api_hash from https://my.telegram.org)

## Installation

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `ADMIN_USER_ID` | Your Telegram user ID (only this user can control the bot) |
| `DATABASE_URL` | SQLAlchemy async URL (default: `sqlite+aiosqlite:///bot.db`) |
| `ENCRYPTION_KEY` | Fernet key for encrypting api_id/api_hash/sessions. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `LOG_FILE` | Path to log file (default: `bot.log`) |

## Running

```bash
python main.py
```

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Main menu |
| `/add_account` | Add a Telegram user account (api_id, api_hash, phone, code) |
| `/list_accounts` | List all added accounts |
| `/remove_account <id>` | Remove an account |
| `/create_campaign` | Create a new broadcast campaign |
| `/list_campaigns` | List all campaigns |
| `/start_campaign <id>` | Start a campaign (with preview and confirmation) |
| `/stop_campaign <id>` | Stop a running campaign |
| `/campaign_stats <id>` | View campaign statistics |
| `/export_logs <id>` | Export send logs as CSV |
| `/edit_campaign <id>` | Edit text or interval of a draft/paused campaign |

## Architecture

```
main.py              - Entry point, bot initialization
config.py            - Configuration from environment
database.py          - SQLAlchemy engine and session factory
models.py            - ORM models (Account, Campaign, Recipient, SentLog)
handlers/
  start.py           - /start and main menu
  accounts.py        - Account management (add, list, remove)
  campaigns.py       - Campaign CRUD, start/stop, stats, export
  middleware.py       - Admin-only access middleware
services/
  account_manager.py - Telethon account operations, encryption
  campaign_manager.py- Campaign lifecycle, background send tasks
  sender.py          - Single message send with duplicate checking
utils/
  crypto.py          - Fernet symmetric encryption
  helpers.py         - Recipient parsing, CSV export, timestamps
```

## Features

- Multi-account support with encrypted session storage
- Inline keyboard UI for account/campaign selection
- Duplicate detection (per account+recipient within 30 days)
- Round-robin account rotation across recipients
- Background async tasks with graceful stop
- Campaign editing (text, interval) for draft/paused campaigns
- CSV log export
- 2FA support during account authorization
- File upload for recipient lists (.txt)
