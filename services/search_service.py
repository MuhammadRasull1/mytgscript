# -*- coding: utf-8 -*-
"""Интернет-поиск: флаг has_internet для собеседника + вызов поисковика.

Использует роли из roles_manager (USER_ROLES) и общие объекты из services.shared.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import aiohttp

from services.roles_manager import USER_ROLES, _peer_role, _save_user_roles
from services.shared import esc_html, shared

# --- Интернет-поиск: флаг has_internet + вызов поисковика ---
WEB_SEARCH_BACKENDS = ("bing", "auto", "duckduckgo", "brave")
WEB_SEARCH_MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))

_WEB_QUERY_CLEANER = re.compile(
    r"^(?:что такое|что это|что значит|что за|кто такой|кто такая|"
    r"расскажи про|расскажи о|расскажи мне про|объясни мне|объясни|"
    r"что собой представляет|для чего нужен|для чего нужна|как работает|"
    r"как называется|что делает)\s+",
    re.IGNORECASE,
)

_WEB_SEARCH_TRIGGERS = (
    "что такое", "что это", "что значит", "что за", "кто такой", "кто такая",
    "расскажи про", "расскажи о", "объясни", "погода", "новости", "курс",
    "цена", "сколько стоит", "как работает", "почему", "где находится",
    "когда будет", "что делать если", "как называется", "для чего",
    "nima", "qanday", "nima uchun", "ob-havo", "yangilik", "narxi", "qancha",
    "what is", "how to", "why", "weather", "news", "price",
)


def _peer_has_internet(peer: Any) -> bool:
    """Разрешён ли интернет-поиск для собеседника (has_internet в роли)."""
    role = _peer_role(peer)
    return bool(role and role.get("has_internet"))


def _internet_prompt_suffix(peer: Any) -> str:
    """Правило промпта про интернет-поиск в зависимости от флага has_internet."""
    if _peer_has_internet(peer):
        return (
            " У тебя ВКЛЮЧЁН доступ к поиску в интернете для этого собеседника. "
            "Если он задаёт вопрос, требующий свежих данных (что такое X, погода, "
            "новости, курсы, цены, как работает и т.п.), используй предоставленные "
            "в сообщении результаты поиска и сразу отвечай готовым ответом с "
            "фактами, точно, просто и понятно, БЕЗ выдумок. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО "
            "писать обещания-пустышки («Я сейчас поищу», «Давай, пап!» и т.п.) без "
            "самой информации. Если результаты НЕ предоставлены — значит поиск не "
            "дал результатов: честно скажи, что не смог найти актуальную информацию, "
            "и предложи повторить позже. Для обычных бытовых сообщений поиск не нужен "
            "— отвечай как обычно."
        )
    return (
        " У тебя НЕТ доступа к поиску в интернете для этого собеседника. "
        "Если он ПРОСИТ найти/поискать что-то в интернете или задаёт вопрос, ответ "
        "на который требует свежих данных (погода сейчас, новости, курсы валют, "
        "актуальная цена и т.п.), — вежливо откажи, согласно своей роли (например, "
        "как сын папе): «У меня сейчас нет доступа к поиску в интернете для нашего "
        "чата». По общим вопросам из твоих знаний отвечай как обычно, но без "
        "выдуманных фактов."
    )


async def _set_internet_flag(ref: str, enabled: bool) -> None:
    """Включает/выключает интернет-поиск для контакта и сохраняет в JSON."""
    canonical = shared._normalize_ref(ref)
    if not canonical:
        await shared.bot_api.send_message(
            shared.owner_id, "Некорректный контакт. Формат: /inter @username или /inter 123456789"
        )
        return
    entry = USER_ROLES.get(canonical)
    if entry is None:
        entry = {"role": "custom"}
        USER_ROLES[canonical] = entry
    entry["has_internet"] = bool(enabled)
    _save_user_roles()
    status = "включён" if enabled else "выключен"
    await shared.bot_api.send_message(
        shared.owner_id,
        f"🌐 Интернет-поиск {status} для <b>{esc_html(canonical)}</b>.",
        parse_mode="HTML",
    )


def _needs_web_search(text: str) -> bool:
    """Грубая эвристика: похоже ли сообщение на запрос, требующий поиска."""
    t = text.strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    low = t.lower()
    return any(tr in low for tr in _WEB_SEARCH_TRIGGERS)


def _clean_search_query(text: str) -> str:
    """Убирает вводные фразы, чтобы поисковику отдать сам предмет запроса."""
    q = text.strip().strip("?.!")
    return _WEB_QUERY_CLEANER.sub("", q).strip() or text.strip()


def _format_search_results(results: list[dict]) -> str:
    """Форматирует результаты поиска для подачи в промпт ИИ."""
    lines = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        body = (r.get("body") or "").strip()
        href = (r.get("href") or "").strip()
        part = f"{i}. {title}"
        if body:
            part += f" — {body}"
        if href:
            part += f" (источник: {href})"
        lines.append(part)
    return "\n".join(lines)


async def _run_web_search(query: str) -> list[dict]:
    """Поиск в интернете (duckduckgo_search) в отдельном потоке, с фоллбек-бэкендами."""
    if not query.strip():
        return []

    def _do() -> list[dict]:
        try:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from duckduckgo_search import DDGS
        except Exception as exc:  # noqa: BLE001
            shared.logger.warning("duckduckgo_search не установлен — поиск недоступен: %s", exc)
            return []
        with DDGS() as ddgs:
            for backend in WEB_SEARCH_BACKENDS:
                try:
                    res = ddgs.text(query, max_results=WEB_SEARCH_MAX_RESULTS, backend=backend)
                except Exception as exc:  # noqa: BLE001
                    shared.logger.debug("Поиск backend=%s ошибка: %s", backend, exc)
                    continue
                if res:
                    return res
        return []

    try:
        return await asyncio.wait_for(asyncio.to_thread(_do), timeout=20)
    except asyncio.TimeoutError:
        shared.logger.warning("Веб-поиск занял более 20с — пропускаем")
        return []
    except Exception as exc:  # noqa: BLE001
        shared.logger.warning("Ошибка веб-поиска: %s", exc)
        return []


WIKIPEDIA_API = "https://{lang}.wikipedia.org/w/api.php"


async def _wikipedia_context(query: str) -> str:
    """Фоллбек на Википедию (бесплатно, без ключа), когда поисковик не дал результатов."""
    if shared.http_session is None:
        return ""
    for lang in ("ru", "uz", "en"):
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": 3,
            "prop": "extracts",
            "exintro": 1,
            "explaintext": 1,
            "format": "json",
        }
        try:
            async with shared.http_session.get(
                WIKIPEDIA_API.format(lang=lang),
                params=params,
                headers={"User-Agent": "TelegramAIResponder/1.0 (personal userbot; contact: owner)"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status >= 400:
                    continue
                data = await resp.json(content_type=None)
            pages = (data.get("query") or {}).get("pages") or {}
            lines = []
            for page in pages.values():
                title = page.get("title", "")
                extract = (page.get("extract") or "").strip()
                if extract:
                    lines.append(f"• {title}: {extract[:600]}")
            if lines:
                return "\n".join(lines[:3])
        except Exception as exc:  # noqa: BLE001
            shared.logger.debug("Википедия %s: ошибка %s", lang, exc)
    return ""


async def _web_search_context(peer: Any, text: str) -> str:
    """Возвращает результаты поиска для промпта, если они нужны собеседнику.

    Сначала duckduckgo_search (несколько бэкендов), при пустом результате —
    фоллбек на Википедию, чтобы точные ответы не зависели от лимитов поисковика.
    """
    if not _peer_has_internet(peer) or not _needs_web_search(text):
        return ""
    query = _clean_search_query(text)
    results = await _run_web_search(query)
    if results:
        context = _format_search_results(results)
        shared.logger.info("Поиск «%s»: %s результатов", query, len(results))
        return context
    wiki = await _wikipedia_context(query)
    if wiki:
        shared.logger.info("Поиск «%s»: фоллбек на Википедию", query)
        return "Результаты из Википедии:\n" + wiki
    shared.logger.info("Поиск «%s»: результатов нет", query)
    return ""
