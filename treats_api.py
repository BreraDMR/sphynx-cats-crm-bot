"""Thin client for the sphynx-cattery-website treats catalog API.

A sibling of catalog_api.py: talks to api/treats.php over HTTP with the same
shared X-API-Key secret. The bot never touches the website's MySQL database
directly -- the two projects only share an HTTP contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger("sphynx_crm.treats_api")


class TreatsApiError(Exception):
    """Raised when api/treats.php rejects a request or is unreachable."""


@dataclass
class NewTreat:
    name: str
    category: str
    price_eur: int
    weight_g: int
    description: str
    created_by: str
    photo_bytes: bytes | None = None
    photo_filename: str | None = None


async def create_treat(session: aiohttp.ClientSession, base_url: str, api_key: str, treat: NewTreat) -> dict:
    form = aiohttp.FormData()
    form.add_field("name", treat.name)
    form.add_field("category", treat.category)
    form.add_field("price_eur", str(treat.price_eur))
    form.add_field("weight_g", str(treat.weight_g))
    form.add_field("description", treat.description)
    form.add_field("created_by", treat.created_by)

    if treat.photo_bytes:
        form.add_field("photo", treat.photo_bytes, filename=treat.photo_filename or "photo.jpg")

    try:
        async with session.post(base_url, data=form, headers={"X-API-Key": api_key}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            payload = await resp.json()
            if resp.status not in (200, 201):
                raise TreatsApiError(payload.get("message", f"HTTP {resp.status}"))
            return payload
    except aiohttp.ClientError as e:
        logger.exception("Failed to reach the treats API")
        raise TreatsApiError(f"Не вдалося з'єднатися з сайтом: {e}") from e


async def list_treats(session: aiohttp.ClientSession, base_url: str, api_key: str) -> list[dict]:
    try:
        async with session.get(base_url, params={"all": "1"}, headers={"X-API-Key": api_key}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise TreatsApiError(payload.get("message", f"HTTP {resp.status}"))
            return payload.get("treats", [])
    except aiohttp.ClientError as e:
        logger.exception("Failed to reach the treats API")
        raise TreatsApiError(f"Не вдалося з'єднатися з сайтом: {e}") from e


async def delete_treat(session: aiohttp.ClientSession, base_url: str, api_key: str, treat_id: int) -> None:
    try:
        async with session.delete(base_url, params={"id": str(treat_id)}, headers={"X-API-Key": api_key}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise TreatsApiError(payload.get("message", f"HTTP {resp.status}"))
    except aiohttp.ClientError as e:
        logger.exception("Failed to reach the treats API")
        raise TreatsApiError(f"Не вдалося з'єднатися з сайтом: {e}") from e
