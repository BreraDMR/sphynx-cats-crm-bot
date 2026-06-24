"""Thin client for the sphynx-cattery-website contact-form requests API.

Pull side of the requests notification flow: the site pushes new requests
to notify_server.py as they come in, and /requests lets an admin pull the
current list on demand (e.g. if a push got lost while the bot was down).
"""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger("sphynx_crm.requests_api")


class RequestsApiError(Exception):
    """Raised when api/requests.php rejects a request or is unreachable."""


async def list_requests(session: aiohttp.ClientSession, base_url: str, api_key: str, limit: int = 10) -> list[dict]:
    try:
        async with session.get(base_url, params={"limit": str(limit)}, headers={"X-API-Key": api_key}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise RequestsApiError(payload.get("message", f"HTTP {resp.status}"))
            return payload.get("requests", [])
    except aiohttp.ClientError as e:
        logger.exception("Failed to reach the requests API")
        raise RequestsApiError(f"Не вдалося з'єднатися з сайтом: {e}") from e
