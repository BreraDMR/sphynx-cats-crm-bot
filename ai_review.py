"""Grammar/style review of a kitten description via Ollama.

Uses the same OpenAI-compatible /v1/chat/completions endpoint as the other
bot in this homelab (pc-tele-monitor-ai's gemma.py) -- just a single
stateless request, no chat history to keep here.
"""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger("sphynx_crm.ai_review")

SYSTEM_PROMPT = (
    "Ти редактор оголошень про кошенят породи сфінкс для сайту розплідника. "
    "Виправ граматику, пунктуацію та стиль наданого опису українською мовою, "
    "зберігаючи його зміст і довжину приблизно такою ж. Не додавай нової "
    "інформації, яку не вказав автор. Поверни ЛИШЕ виправлений текст, без "
    "пояснень, заголовків чи лапок."
)


class AiReviewUnavailable(Exception):
    """Raised when Ollama can't be reached or returns an error -- callers
    should fall back to the admin's original text rather than block on this."""


async def review_description(session: aiohttp.ClientSession, ollama_url: str, model: str, text: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": False,
    }

    endpoint = f"{ollama_url.rstrip('/')}/v1/chat/completions"

    try:
        async with session.post(endpoint, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                raise AiReviewUnavailable(f"Ollama returned {resp.status}: {err_text[:200]}")
            result = await resp.json()
            return result["choices"][0]["message"]["content"].strip()
    except aiohttp.ClientError as e:
        raise AiReviewUnavailable(str(e)) from e
    except (KeyError, IndexError) as e:
        raise AiReviewUnavailable(f"Unexpected Ollama response shape: {e}") from e
