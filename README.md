<div align="center">

# 🐱 Sphynx Cats CRM Bot

**Manage a website's catalogs from your phone over Telegram — guided add flows, owner-approved admins, and a local-AI grammar pass on every listing.**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white&style=for-the-badge)](requirements.txt)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white&style=for-the-badge)](requirements.txt)
[![Ollama](https://img.shields.io/badge/Ollama-local%20AI-000000?logo=ollama&logoColor=white&style=for-the-badge)](ai_review.py)
[![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white&style=for-the-badge)](docker-compose.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](LICENSE)
[![status](https://img.shields.io/badge/status-active-brightgreen?style=for-the-badge)](#)

</div>

A Telegram bot that lets the team behind
[sphynx-cattery-website](https://github.com/BreraDMR/sphynx-cattery-website)
manage the site's two catalogs — **kittens** and **treats** — straight from a
phone, photo included, without ever opening the web admin panel. Each listing's
description is run past a **local AI model** for a grammar/style check, and the
admin picks which model does the checking.

Companion automation for a learning/portfolio project (a fictional Sphynx-cat
cattery storefront) — see the website repo for the full picture.

## The problems it solves

- **Updating the catalog meant a laptop + the web admin panel.** Now a new
  card — name, attributes, photo — is added in a guided chat flow from any
  phone, by any approved admin.
- **Onboarding admins meant sharing a password or hand-editing config.** Anyone
  runs `/register`; the **owner** gets ✅/❌ buttons and approves with one tap.
  No shared secret, no redeploy per person, and a full audit log of who did
  what.
- **Listings were written by non-native, non-copywriter admins.** Every
  description is sent to a **local Ollama model** for a grammar/style pass; the
  admin sees their text vs. the AI's suggestion **side by side and chooses** —
  nothing is overwritten silently. It runs on our own hardware: no cloud, no
  per-request cost, no data leaving the box.
- **AI quality vs. speed is a real tradeoff on CPU-only hardware.** So the
  admin **chooses the model per-account** with `/model`: a fast **`qwen2.5:3b`**
  or a smarter, slower **`qwen2.5:14b`**. Both stay resident in Ollama, so
  switching costs nothing.
- **Two product lines shouldn't mean two different tools.** The treat commands
  mirror the kitten ones exactly, so there's nothing new to learn.
- **A lead could be missed while the bot was offline.** The website pushes
  every new contact-form submission to the bot in real time, and `/requests`
  re-pulls the current list on demand as a fallback.

## Commands

| Command | Who | Description |
|---|---|---|
| `/start`, `/help`, `/whoami` | everyone | Status and command list |
| `/register` | anyone, once | Request admin access (owner approves) |
| `/add_cat` | admins | Add a kitten card, with AI description review |
| `/list_cats`, `/delete_cat <id>` | admins | Manage kittens (incl. drafts) |
| `/add_treat` | admins | Add a treat card, with AI description review |
| `/list_treats`, `/delete_treat <id>` | admins | Manage treats |
| `/model` | admins | Pick the AI model (fast `3b` vs. smarter `14b`) |
| `/requests` | admins | Pull the latest contact-form requests |
| `/admins`, `/auditlog` | owner only | Approve/ban admins, review actions |

## Architecture

```
                 ┌────────────────────┐
   Telegram ───▶ │  sphynx-cats-      │     per-admin model choice
  (long polling) │  crm-bot           │            (/model)
                 │  SQLite: admins +  │               │
                 │  per-user settings │               ▼
                 └─────────┬──────────┘        ┌──────────────┐
                           │ X-API-Key (HTTP)  │    Ollama    │
              ┌────────────┴───────────┐       │ qwen2.5:3b   │
              ▼                        ▼        │ qwen2.5:14b │
   ┌────────────────────┐   ┌────────────────┐ └──────────────┘
   │ api/cats.php       │   │ api/treats.php │   grammar/style
   │ (kitten catalog)   │   │ (treat catalog)│   check, chosen
   └────────────────────┘   └────────────────┘   per request
        sphynx-cattery-website (PHP + MySQL)
```

The bot and website are independent repos/containers sharing only an HTTP
contract (`api/cats.php`, `api/treats.php`) — **no shared database**. The bot's
own SQLite file only tracks *who* may use it and *which model* each admin
prefers; the catalogs live in the website's MySQL.

## Setup

### 1. Configure

```sh
cp .env.example .env
```

| Variable | Where it comes from |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) |
| `OWNER_TELEGRAM_ID` | your numeric Telegram ID ([@userinfobot](https://t.me/userinfobot)) |
| `BOT_API_KEY` | `openssl rand -hex 24` — must match the website's `BOT_API_KEY` |
| `SITE_API_URL` / `SITE_TREATS_API_URL` / `SITE_REQUESTS_API_URL` | the site's `api/cats.php` / `api/treats.php` / `api/requests.php` URLs (container DNS name on the shared Docker network, or a public URL) |
| `OLLAMA_URL` | wherever Ollama runs (e.g. `http://ollama:11434`) |
| `CHAT_MODEL` / `CHAT_MODEL_STRONG` | the weak/strong models (`qwen2.5:3b` / `qwen2.5:14b`); pull both and set `OLLAMA_MAX_LOADED_MODELS=2` so they stay loaded together |

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
- The owner's Telegram ID is the only identity trusted by default; every other
  admin must be explicitly approved.
- The login/password collected during `/register` is hashed (PBKDF2-HMAC-SHA256,
  random salt) but isn't used to authenticate yet — the real trust boundary is
  the Telegram chat ID; it's groundwork for a future web panel.

## License

[MIT](LICENSE)
