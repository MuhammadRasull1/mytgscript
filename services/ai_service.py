# -*- coding: utf-8 -*-
"""AI-провайдер: системные промпты, генерация вариантов ответа, доработка
черновиков, генерация текстов по запросу и история диалога.

Общие объекты (http_session, CFG, logger) берутся из services.shared —
их заполняет userbot.py.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

from services.shared import shared

# --- Прямая отправка сообщений: распознавание намерения ---

_SEND_DIRECT_RE = re.compile(
    r"^(?:напиши\s+в\s+группу|отправь)\s+(@\S+|\d+)\s+(.+)$",
    re.IGNORECASE,
)
_SEND_DIRECT_FLEX_RE = re.compile(
    r"^(?:напиши\s+(?:в\s+группу|пользователю|кому)|отправь\s+(?:пользователю|кому))\s+(.+)$",
    re.IGNORECASE,
)
_FILLER_WORDS = frozenset(
    {
        "пользователю",
        "в чат",
        "в группу",
        "юзеру",
        "пользователь",
        "чату",
        "группе",
        "группу",
        "чат",
    }
)


@dataclass
class Intent:
    """Распознанное намерение пользователя."""

    action: str
    target: str
    text: str


def _strip_filler(raw: str) -> str:
    """Убирает мусорные слова из начала строки."""
    parts = raw.split()
    while parts and parts[0].lower().strip(".,!?") in _FILLER_WORDS:
        parts.pop(0)
    return " ".join(parts)


def detect_direct_send_intent(text: str) -> Optional[Intent]:
    """Определяет намерение «написать напрямую» по тексту сообщения.

    Поддерживаемые форматы:
      «Напиши в группу @username текст»
      «Напиши в группу НазваниеГруппы текст»
      «Отправь @username текст»
      «Отправь 123456 текст»
      «Напиши пользователю Улу привет»
      «Отправь юзеру Улу привет»

    Мусорные слова ("пользователю", "в чат", "в группу", "юзеру" и
    подобные) автоматически удаляются из target.

    Возвращает Intent(action="send_direct") или None.
    """
    m = _SEND_DIRECT_RE.match(text.strip())
    if m:
        return Intent(
            action="send_direct",
            target=m.group(1),
            text=m.group(2).strip(),
        )
    m = _SEND_DIRECT_FLEX_RE.match(text.strip())
    if not m:
        return None
    cleaned = _strip_filler(m.group(1).strip())
    parts = cleaned.split(None, 1)
    if len(parts) < 2:
        return None
    return Intent(action="send_direct", target=parts[0], text=parts[1].strip())

_DELETE_INTENT_RE = re.compile(
    r"^удали\s+(\d+)?\s*(?:последнее|последних)?\s*(?:сообщение|сообщения|сообщений)?\s*(?:у|пользователю)\s+(.+)$",
    re.IGNORECASE,
)


def detect_delete_intent(text: str) -> Optional[dict]:
    """Определяет намерение удаления последних сообщений у собеседника.

    Поддерживаемые форматы:
      «удали последнее сообщение у Улу»
      «удали 3 последних сообщения у @username»
      «удали последние сообщения у пользователя Улу»

    Возвращает {"action": "delete", "count": N, "target": name} или None.
    """
    m = _DELETE_INTENT_RE.match(text.strip())
    if not m:
        return None
    count_str = m.group(1)
    target = m.group(2).strip()
    count = int(count_str) if count_str else 1
    return {"action": "delete", "count": count, "target": target}


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
# На сколько секунд замораживать ключ Gemini после ошибки квоты (429)
GEMINI_QUOTA_FREEZE_SEC = int(os.getenv("GEMINI_QUOTA_FREEZE_SEC", "600"))
# Как часто логировать общую недоступность Gemini (чтобы не спамить)
_GEMINI_DOWN_LOG_INTERVAL = 300
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models"
    "/{model}:generateContent"
)
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

EMOJI_GUIDELINE = (
    " Используй 1-2 уместных эмодзи в сообщениях там, где это смотрится органично "
    "и естественно в живой человеческой переписке (например: 😊, 👍, 🛠, 💡, 🤝). "
    "НЕ спамь эмодзи в каждом слове и НЕ пихай их, если тема серьезная."
)

BASE_ROLE_RULES = (
    "Ты выступаешь от лица владельца аккаунта — это СЫН / молодой человек. "
    "Твой собеседник — брат, друг, ровесник или обычный контакт, ЕСЛИ для него "
    "не назначена специальная роль (папа / мама). "
    "Если у собеседника нет роли папы или мамы, общайся с ним естественно и "
    "дружелюбно, как ровесник с ровесником. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО обращаться "
    "к собеседнику «пап», «сын», «мам», «сынок», «дочка», «детка» и любыми "
    "подобными словами, если роль не назначена. "
    "Не вставляй имя собеседника в каждое сообщение: пиши его только когда это "
    "уместно по смыслу или владелец явно попросил. "
    "Если собеседник — Папа, ты отвечаешь от лица СЫНА и обращаешься «Папа/Пап» "
    "(или «ада/отта» в зависимости от языка) с сыновьим уважением. "
    "Если собеседник — Мама, ты отвечаешь от лица СЫНА и обращаешься уважительно "
    "«Ойи/Мама» строго на узбекском языке. "
    "Не начинай каждое сообщение с обращения к родителям — обращайся редко и "
    "естественно. "
)

AI_SYSTEM_PROMPT = (
    BASE_ROLE_RULES
    + "Ты — ассистент, который помогает владельцу Telegram-аккаунта отвечать в личных "
    "сообщениях. По сообщению собеседника придумай короткие и естественные варианты "
    "ответа от первого лица, по одному на строку, пронумерованные. Никаких пояснений, "
    "преамбул и лишнего текста — только сами варианты."
    + EMOJI_GUIDELINE
)

REFINE_SYSTEM_PROMPT = (
    BASE_ROLE_RULES
    + "Ты — ассистент, помогающий владельцу Telegram-аккаунта дорабатывать черновик "
    "ответа на сообщение собеседника. В ТОЧНОСТИ выполняй указание пользователя: "
    "стиль, язык, тональность, включая сленг, эмоциональную окраску и ненормативную "
    "лексику, если пользователь просит отвечать дерзко или с матом. Если пользователь "
    "просит написать на конкретном языке (узбекский, русский, английский и т.п.) — "
    "пиши строго на этом языке. При генерации текста на узбекском языке обязательно "
    "используй литературные нормы и правильную грамматику (например: «Tug'ilgan "
    "kuningiz bilan»/«Туғилган кунингиз билан», уважительные обращения «Yusuf aka», "
    "«Siz»). Избегай дословных переводов с русского. Верни только готовый текст "
    "ответа, без пояснений, преамбул и лишнего текста."
    + EMOJI_GUIDELINE
)

CONTENT_SYSTEM_PROMPT = (
    BASE_ROLE_RULES
    + "Ты — креативный ассистент, который по запросу владельца Telegram-аккаунта "
    "придумывает готовые тексты (поздравления, отмазки, сообщения и т.п.). "
    "В ТОЧНОСТИ выполняй запрос: тему, цель, стиль и ЯЗЫК — если указан конкретный "
    "язык (узбекский, русский, английский и т.п.), пиши строго на нём. При генерации "
    "текста на узбекском языке обязательно используй литературные нормы и правильную "
    "грамматику (например: «Tug'ilgan kuningiz bilan»/«Туғилган кунингиз билан», "
    "уважительные обращения «Yusuf aka», «Siz»). Избегай дословных переводов с русского. "
    "Допускаются разговорный сленг, жёсткий юмор и ненормативная лексика, если "
    "пользователь просит. Верни ровно 3 варианта, по одному на строку, пронумерованные. "
    "Без пояснений, преамбул и лишнего текста."
    + EMOJI_GUIDELINE
)

GEMINI_SAFETY = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


@dataclass
class EditCtx:
    """Контекст доработки черновика (AI-refinement)."""

    peer_id: int          # собеседник, которому уйдёт ответ
    peer_name: str
    peer_msg_id: int      # id сообщения собеседника (для reply_to_message_id)
    original: str         # исходное сообщение собеседника
    draft: str            # текущий черновик


# --- Контекст диалога: последние сообщения с каждым собеседником ---
# peer_id -> [(role, text)], role: "peer" | "me"
DIALOG_HISTORY: dict[Any, list[tuple[str, str]]] = {}
HISTORY_LIMIT = 10  # храним не больше 10 записей, в промпт уходят последние 3-5


def _push_dialog(peer_id: Any, role: str, text: str) -> None:
    """Добавляет сообщение в историю переписки с собеседником."""
    if not text:
        return
    hist = DIALOG_HISTORY.setdefault(peer_id, [])
    hist.append((role, str(text)[:500]))
    del hist[:-HISTORY_LIMIT]


def _dialog_history_block(peer_id: Any, limit: int = 5) -> str:
    """Форматирует последние N сообщений диалога для подстановки в промпт."""
    hist = DIALOG_HISTORY.get(peer_id) or []
    lines = []
    for role, text in hist[-limit:]:
        marker = "Собеседник" if role == "peer" else "Ты (владелец)"
        lines.append(f"{marker}: {text}")
    return "\n".join(lines)


def _build_user_prompt(
    text: str, peer_name: str, web_context: str = "", history: str = "", username: str = ""
) -> str:
    who = peer_name or "Незнакомец"
    if username:
        who += f" (username: @{username})"
    body = f"Собеседник: {who}"
    if history:
        body += f"\n\nИстория переписки с ним (последние сообщения, для контекста):\n{history}"
    body += f"\n\nТекущее сообщение собеседника:\n{text}"
    if web_context:
        body += (
            "\n\nРезультаты поиска в интернете (используй их для точного ответа, "
            f"не выдумывай):\n{web_context}"
        )
    return (
        body
        + f"\n\nПредложи {shared.CFG.max_suggestions} варианта ответа (только варианты, по одному "
        "на строку, пронумерованные)."
    )


def _build_refine_prompt(original: str, draft: str, instruction: str) -> str:
    return (
        f"Исходное сообщение собеседника:\n{original or '(нет текста)'}\n\n"
        f"Текущий черновик:\n{draft}\n\n"
        f"Указание пользователя (выполни строго):\n{instruction}"
    )


def _parse_suggestions(raw: str) -> list[str]:
    """Извлекает нумерованные/буллет-строки из текста LLM в список вариантов."""
    out: list[str] = []
    for line in (raw or "").splitlines():
        item = line.strip()
        if not item:
            continue
        if len(item) > 1 and item[0].isdigit() and item[1] in ".):":
            item = item[2:].strip()
        elif item.startswith(("- ", "• ", "— ")):
            item = item[2:].strip()
        item = item.strip('"\'»“”')
        if item and item not in out:
            out.append(item)
        if len(out) >= shared.CFG.max_suggestions:
            break
    if not out and (raw or "").strip():
        return [(raw or "").strip().strip('"')]
    return out


# --- Карантин ключей Gemini (HTTP 429/квота) ---
# api_key -> монотонное время (time.monotonic()), до которого ключ заморожен
_GEMINI_KEY_FAILURES: dict[str, float] = {}
# монотонное время последнего инфо-лога о недоступности Gemini целиком
_last_gemini_down_log: float = 0.0


def _gemini_api_keys() -> list[str]:
    """Все ключи Gemini из env: GEMINI_API_KEY, GEMINI_API_KEY_2, ..."""
    keys = [
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GEMINI_API_KEY_2"),
        os.getenv("GEMINI_API_KEY_3"),
    ]
    return [k for k in keys if k]


def _is_gemini_key_frozen(api_key: str) -> bool:
    """Заморожен ли ключ из-за квоты (попытки не возобновляем до разморозки)."""
    until = _GEMINI_KEY_FAILURES.get(api_key)
    return until is not None and until > time.monotonic()


def _freeze_gemini_key(api_key: str) -> None:
    """Замораживает ключ на GEMINI_QUOTA_FREEZE_SEC (по умолчанию 10 минут)."""
    _GEMINI_KEY_FAILURES[api_key] = time.monotonic() + GEMINI_QUOTA_FREEZE_SEC


async def _gemini_generate(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Вызов Gemini с ротацией ключей.

    При HTTP 429 (квота) ключ МГНОВЕННО замораживается на 10 минут, а запрос
    сразу же уходит на следующий ключ (GEMINI_API_KEY_2, ...). Замороженные
    ключи пропускаются молча — повторные ошибки квоты не спамят в логи.
    Если все ключи заморожены/недоступны — возвращает None, дальше сработает
    фоллбек на Groq.
    """
    keys = _gemini_api_keys()
    if not keys:
        shared.logger.info("GEMINI_API_KEY не задана — пропускаем Gemini")
        return None

    tried = False
    for api_key in keys:
        if _is_gemini_key_frozen(api_key):
            continue
        tried = True
        body = {
            "contents": [{"parts": [{"text": user_prompt}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "safetySettings": GEMINI_SAFETY,
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 500},
        }
        try:
            async with shared.http_session.post(
                GEMINI_API_URL.format(model=GEMINI_MODEL),
                params={"key": api_key},
                json=body,
                timeout=aiohttp.ClientTimeout(total=shared.CFG.ai_timeout),
            ) as resp:
                if resp.status == 429:
                    _freeze_gemini_key(api_key)
                    shared.logger.warning(
                        "Gemini %s: квота (HTTP 429) — ключ заморожен на %s с",
                        GEMINI_MODEL, GEMINI_QUOTA_FREEZE_SEC,
                    )
                    continue
                if resp.status >= 400:
                    err = (await resp.text(errors="replace"))[:300]
                    shared.logger.warning("Gemini %s: HTTP %s %s", GEMINI_MODEL, resp.status, err)
                    continue
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            shared.logger.warning("Ошибка обращения к Gemini (%s): %s", GEMINI_MODEL, exc)
            continue
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        text = "\n".join(p.get("text", "") for p in parts).strip() or None
        if text is not None:
            _GEMINI_KEY_FAILURES.pop(api_key, None)
        return text
    if tried:
        shared.logger.info("Все ключи Gemini недоступны/в карантине — переключаемся на Groq")
    return None


async def _groq_generate(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Вызов Groq (фоллбек). Возвращает текст ответа или None при ошибке."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        shared.logger.info("GROQ_API_KEY не задана — пропускаем Groq")
        return None
    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 500,
    }
    try:
        async with shared.http_session.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=aiohttp.ClientTimeout(total=shared.CFG.ai_timeout),
        ) as resp:
            if resp.status >= 400:
                err = (await resp.text(errors="replace"))[:300]
                shared.logger.warning("Groq %s: HTTP %s %s", GROQ_MODEL, resp.status, err)
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        shared.logger.warning("Ошибка обращения к Groq (%s): %s", GROQ_MODEL, exc)
        return None
    try:
        return (data["choices"][0]["message"]["content"] or "").strip() or None
    except (KeyError, IndexError, TypeError):
        shared.logger.warning("Groq вернул неожиданный ответ: %s", data)
        return None


async def _generate_with_fallback(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Gemini (с ротацией ключей), при лимитах/ошибках — автоматический фоллбек на Groq."""
    global _last_gemini_down_log
    raw = await _gemini_generate(system_prompt, user_prompt)
    if raw is None:
        now = time.monotonic()
        if now - _last_gemini_down_log >= _GEMINI_DOWN_LOG_INTERVAL:
            _last_gemini_down_log = now
            shared.logger.info("Gemini недоступен/лимит — автоматический фоллбек на Groq")
        raw = await _groq_generate(system_prompt, user_prompt)
    return raw


async def generate_suggestions(
    text: str,
    peer_name: str,
    role_suffix: str = "",
    web_context: str = "",
    history: str = "",
    username: str = "",
) -> list[str]:
    """Генерирует варианты ответа напрямую: Gemini, при лимитах — Groq.

    role_suffix — дополнительные правила промпта по роли собеседника
    (мама/папа/кастомная из /role) и флагу интернет-поиска.
    web_context — свежие результаты поиска, если /inter включён для собеседника.
    history — последние сообщения диалога (контекст для коротких реплик).
    username — @username собеседника (пассивное знание, без спама обращением).
    """
    raw = await _generate_with_fallback(
        AI_SYSTEM_PROMPT + role_suffix,
        _build_user_prompt(text, peer_name, web_context, history, username),
    )
    suggestions = _parse_suggestions(raw) if raw else []
    shared.logger.info("Сгенерировано вариантов ответа: %s", len(suggestions))
    return suggestions


async def refine_draft(original: str, draft: str, instruction: str) -> Optional[str]:
    """AI-доработка черновика по произвольному указанию пользователя."""
    user_prompt = _build_refine_prompt(original, draft, instruction)
    refined = await _generate_with_fallback(REFINE_SYSTEM_PROMPT, user_prompt)
    if refined:
        shared.logger.info("Черновик доработан по указанию: %s", instruction[:60])
    return refined


async def generate_content(instruction: str) -> list[str]:
    """Генерация 3 готовых текстов по произвольному запросу (команда /con)."""
    raw = await _generate_with_fallback(
        CONTENT_SYSTEM_PROMPT, f"Запрос: {instruction}"
    )
    variants = _parse_suggestions(raw) if raw else []
    shared.logger.info("Сгенерировано текстов по запросу: %s", len(variants))
    return variants


_DIRECT_SEND_SYSTEM_PROMPT = (
    "Ты — ассистент, который помогает сформулировать естественное "
    "короткое сообщение для Telegram. На основе заданной темы сгенерируй "
    "одно готовое сообщение от первого лица (от имени владельца аккаунта). "
    "Пиши просто, естественно, как живой человек. Без пояснений, без "
    "преамбул — только готовое сообщение."
)


async def generate_direct_send_text(topic: str) -> Optional[str]:
    """Генерирует естественное сообщение для Telegram на основе темы.

    Используется при прямой отправке: пользователь указывает тему
    (например, "привет" или "как дела"), а ИИ формулирует
    готовое сообщение для отправки собеседнику.
    """
    raw = await _generate_with_fallback(
        _DIRECT_SEND_SYSTEM_PROMPT,
        f"Тема: {topic}",
    )
    if raw:
        shared.logger.info("Сгенерировано сообщение для прямой отправки: %s", raw[:80])
    return raw
