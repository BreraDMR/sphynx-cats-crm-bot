# 🐱 Sphynx Cats CRM Bot

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-active-brightgreen)

A Telegram bot that lets the [sphynx-cattery-website](https://github.com/BreraDMR/sphynx-cattery-website)
admin team add new kitten cards to the catalog without touching the
website's admin panel — register, get approved by the bot's owner, then add
a kitten straight from a phone, photo included. Every description is run
past a local Ollama model for a quick grammar/style check before it's
published.

This is the companion automation piece for a learning/portfolio project
(a fictional Sphynx-cat cattery storefront) — see the website repo for the
full picture.

## Features

- **Owner-approved admin registration.** Anyone can run `/register`; the
  bot's owner (a fixed Telegram ID) gets a notification with
  ✅ Approve / ❌ Reject buttons. No shared admin password, no manual `.env`
  editing per new admin.
- **`/add_cat`** — a guided, step-by-step flow (name → color → age → price →
  description → photo) that ends with a real card on the live website.
- **AI-assisted description review.** Before a card is created, the
  kitten's description is sent to a local Ollama model for a grammar/style
  pass. The admin sees both versions side by side and picks one — the AI
  never overwrites anything silently.
- **`/list_cats` / `/delete_cat`** — manage what's already in the catalog,
  including draft cards not yet visible to site visitors.
- **Admin management & audit log.** The owner can ban/unban admins
  (`/admins`) and review every approve/reject/ban/add/delete action
  (`/auditlog`).
- **No shared database.** The bot never touches the website's MySQL
  database — it only talks to `api/cats.php` over HTTPS with a shared
  `X-API-Key` secret. The bot's own SQLite file only tracks *who* is
  allowed to use it.

## Architecture

```
                 ┌───────────────────┐
   Telegram  ───▶│   sphynx-cats-     │
   (aiogram      │   crm-bot          │
    long polling)│                    │
                 │  admins.db (SQLite)│
                 └─────────┬──────────┘
                           │ X-API-Key over HTTP
                           ▼
                 ┌───────────────────┐        ┌──────────────┐
                 │ sphynx-cattery-    │        │   Ollama     │
                 │ website            │◀──────▶│ (grammar/    │
                 │ api/cats.php       │  bot   │  style check)│
                 │ (PHP + MySQL)      │ calls  └──────────────┘
                 └───────────────────┘  directly, not via the site
```

The bot and the website are two independent repos/containers sharing only
an HTTP contract (`api/cats.php`) — no shared filesystem, no shared
database connection.

## Commands

| Command | Who | Description |
|---|---|---|
| `/start`, `/help`, `/whoami` | everyone | Status and command list |
| `/register` | anyone, once | Request admin access (login + password, sent to the owner for approval) |
| `/add_cat` | admins | Add a new kitten card, with AI description review |
| `/list_cats` | admins | List every kitten, including drafts |
| `/delete_cat <id>` | admins | Remove a kitten card (with confirmation) |
| `/admins` | owner only | Approve/ban/unban admins |
| `/auditlog` | owner only | Recent admin actions |

## Setup

### 1. Configure

```sh
cp .env.example .env
```

Fill in `.env`:

| Variable | Where it comes from |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) |
| `OWNER_TELEGRAM_ID` | your numeric Telegram ID, e.g. from [@userinfobot](https://t.me/userinfobot) |
| `BOT_API_KEY` | `openssl rand -hex 24` — must match `BOT_API_KEY` in the website's own `.env` |
| `SITE_API_URL` | the website's `api/cats.php` URL (container DNS name inside the same Docker network, or a public URL) |
| `OLLAMA_URL`, `CHAT_MODEL` | wherever Ollama is running; a small model (e.g. `qwen2.5:3b`) is enough for a grammar pass |

### 2. Run with Docker

```sh
docker compose up --build -d
```

### 3. Run without Docker

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 bot.py
```

## Security notes

- `TELEGRAM_BOT_TOKEN` and `BOT_API_KEY` are secrets — never commit `.env`
  (already in `.gitignore`).
- The owner's Telegram ID is the only identity that's trusted by default;
  every other admin must be explicitly approved through the bot.
- The login/password collected during `/register` is hashed (PBKDF2-HMAC-
  SHA256, random salt) and stored, but isn't used to authenticate anything
  *yet* — the real trust boundary today is the Telegram chat ID. It's there
  as a CRM-style identity record and groundwork for a future web panel.

## License

[MIT](LICENSE)
