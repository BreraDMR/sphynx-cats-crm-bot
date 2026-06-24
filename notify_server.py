"""Tiny internal-only aiohttp server: receives a push from the website
when a new contact-form request comes in, and forwards it to every
admin in Telegram.

Runs alongside the bot's own long-polling in the same process (see
bot.py's main()). Only reachable from other containers on the same
Docker network -- nothing here is exposed to the public internet.
"""

from __future__ import annotations

import html
import logging
from typing import Callable

from aiogram import Bot
from aiohttp import web

logger = logging.getLogger("sphynx_crm.notify_server")


def format_request_message(data: dict) -> str:
    lines = ["🔔 <b>Нова заявка з сайту!</b>", ""]
    lines.append(f"👤 {html.escape(str(data.get('name', '')))}")
    lines.append(f"📧 {html.escape(str(data.get('email', '')))}")
    if data.get("phone"):
        lines.append(f"📱 {html.escape(str(data['phone']))}")
    if data.get("color"):
        lines.append(f"🎨 Бажаний колір: {html.escape(str(data['color']))}")
    if data.get("age"):
        lines.append(f"📅 Вік кошеняти: {html.escape(str(data['age']))}")
    if data.get("message"):
        lines.append(f"💬 {html.escape(str(data['message']))}")
    lines.append("")
    lines.append("👉 Повний список — /requests")
    return "\n".join(lines)


def create_notify_app(bot: Bot, api_key: str, get_target_ids: Callable[[], list[int]]) -> web.Application:
    async def handle_notify(request: web.Request) -> web.Response:
        if not api_key or request.headers.get("X-API-Key") != api_key:
            return web.json_response({"status": "error", "message": "unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"status": "error", "message": "invalid json"}, status=400)

        message = format_request_message(data)
        sent = 0
        for telegram_id in get_target_ids():
            try:
                await bot.send_message(telegram_id, message)
                sent += 1
            except Exception:
                logger.exception(f"Failed to notify {telegram_id} about new request")

        return web.json_response({"status": "success", "notified": sent})

    app = web.Application()
    app.router.add_post("/notify-request", handle_notify)
    return app
