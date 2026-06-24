"""Thin client for the sphynx-cattery-website kitten catalog API.

Talks to api/cats.php over HTTP with the shared X-API-Key secret -- the
bot never touches the website's MySQL database directly, so the two
projects only share an HTTP contract, not a database connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger("sphynx_crm.catalog_api")


class CatalogApiError(Exception):
    """Raised when api/cats.php rejects a request or is unreachable."""


@dataclass
class NewCat:
    name: str
    color: str
    age_months: int
    price_eur: int
    description: str
    created_by: str
    photo_bytes: bytes | None = None
    photo_filename: str | None = None


async def create_cat(session: aiohttp.ClientSession, base_url: str, api_key: str, cat: NewCat) -> dict:
    form = aiohttp.FormData()
    form.add_field("name", cat.name)
    form.add_field("color", cat.color)
    form.add_field("age_months", str(cat.age_months))
    form.add_field("price_eur", str(cat.price_eur))
    form.add_field("description", cat.description)
    form.add_field("created_by", cat.created_by)

    if cat.photo_bytes:
        form.add_field("photo", cat.photo_bytes, filename=cat.photo_filename or "photo.jpg")

    try:
        async with session.post(base_url, data=form, headers={"X-API-Key": api_key}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            payload = await resp.json()
            if resp.status not in (200, 201):
                raise CatalogApiError(payload.get("message", f"HTTP {resp.status}"))
            return payload
    except aiohttp.ClientError as e:
        logger.exception("Failed to reach the catalog API")
        raise CatalogApiError(f"Не вдалося з'єднатися з сайтом: {e}") from e


async def list_cats(session: aiohttp.ClientSession, base_url: str, api_key: str) -> list[dict]:
    try:
        async with session.get(base_url, params={"all": "1"}, headers={"X-API-Key": api_key}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise CatalogApiError(payload.get("message", f"HTTP {resp.status}"))
            return payload.get("cats", [])
    except aiohttp.ClientError as e:
        logger.exception("Failed to reach the catalog API")
        raise CatalogApiError(f"Не вдалося з'єднатися з сайтом: {e}") from e


async def delete_cat(session: aiohttp.ClientSession, base_url: str, api_key: str, cat_id: int) -> None:
    try:
        async with session.delete(base_url, params={"id": str(cat_id)}, headers={"X-API-Key": api_key}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise CatalogApiError(payload.get("message", f"HTTP {resp.status}"))
    except aiohttp.ClientError as e:
        logger.exception("Failed to reach the catalog API")
        raise CatalogApiError(f"Не вдалося з'єднатися з сайтом: {e}") from e
