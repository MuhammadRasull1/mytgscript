#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-автоответчик для личного Telegram (userbot на Pyrogram + Gemini/Groq).

Архитектура (n8n и ngrok не нужны):

  1. Собеседник пишет вам в ЛС -> Pyrogram-клиент (ваша сессия) ловит сообщение.
  2. userbot сам вызывает Gemini API (gemini-2.5-flash) и получает 3 варианта
     ответа; при ошибке лимитов (429/Quota) автоматически фоллбек на Groq API
     (llama-3.3-70b-versatile). Варианты сохраняются в памяти
     (PENDING[(peer_id, msg_id)]).
  3. Режим AI_MODE=bot_chat (по умолчанию): варианты с настоящими inline-кнопками
     [1] [2] [3] [✏️ Ред.] [⏭ Пропустить] уходят через Bot API (BOT_TOKEN)
     в ваш личный чат с ботом @myaccounttbot. Нажатия ловит встроенный
     long-polling (bot_api.py).
  4. Вы нажимаете [1]/[2]/[3] -> выбранный текст уходит собеседнику (как ответ
     на его сообщение), а сообщение в чате с ботом редактируется:
     кнопки убираются, дописывается «✅ Отправлено для <Имя>: "текст"».
  5. Старый режим: AI_MODE=userbot — кнопки в «Избранном», нажатия ловит сам
     userbot.
  6. НИЧЕГО не отправляется автоматически — только по вашему явному действию.

Запуск:
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env   # отредактируйте под себя
    python userbot.py      # первый запуск: телефон, код из Telegram, пароль 2FA
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import aiohttp
from dotenv import load_dotenv

# Совместимость с Python 3.14: pyrogram при импорте вызывает
# asyncio.get_event_loop(), который без активного цикла кидает RuntimeError
# (в 3.12/3.13 это работало с предупреждением). Создаём цикл заранее.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot_api import BotApiClient, BotApiError, BotApiPoller, healthcheck_server

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger("userbot")

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------


@dataclass
class Config:
    api_id: int
    api_hash: str
    session_name: str
    ai_timeout: int
    service_chat: str      # "me" (Избранное) или числовой id чата/группы
    ai_mode: str           # "bot_chat" | "userbot" | "bot"
    max_suggestions: int
    bot_token: Optional[str]   # токен управляющего бота (режим bot_chat)
    owner_id: Optional[int]    # MY_TELEGRAM_ID / OWNER_ID; иначе свой id

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_id=int(os.getenv("TELEGRAM_API_ID") or os.getenv("API_ID") or "611335"),
            api_hash=(
                os.getenv("TELEGRAM_API_HASH")
                or os.getenv("API_HASH")
                or "e86032b40b0213197262024220b333a2"
            ),
            session_name=os.getenv("SESSION_NAME", "ai_responder"),
            ai_timeout=int(os.getenv("AI_TIMEOUT_SEC") or os.getenv("N8N_TIMEOUT_SEC") or "60"),
            service_chat=os.getenv("SERVICE_CHAT_ID", "me"),
            ai_mode=os.getenv("AI_MODE", "bot_chat").lower(),
            max_suggestions=int(os.getenv("MAX_SUGGESTIONS", "3")),
            bot_token=os.getenv("BOT_TOKEN") or None,
            owner_id=_parse_owner_id(),
        )


def _parse_owner_id() -> Optional[int]:
    """Читает MY_TELEGRAM_ID/OWNER_ID; при некорректном значении — warning и None."""
    raw = os.getenv("MY_TELEGRAM_ID") or os.getenv("OWNER_ID") or ""
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("MY_TELEGRAM_ID/OWNER_ID не число (%r) — берём id аккаунта", raw)
        return None


CFG = Config.from_env()

# Глобальные объекты (инициализируются/резолвятся в main())
http_session: Optional[aiohttp.ClientSession] = None
service_chat_id: Optional[int] = None  # резолвится после логина ("me" -> свой id)
owner_id: Optional[int] = None         # владелец: MY_TELEGRAM_ID или свой id
bot_api: Optional[BotApiClient] = None  # клиент Bot API (только режим bot_chat)
bot_user_id: Optional[int] = None       # id управляющего бота (для отсечки его ЛС)

# (peer_id, message_id) -> контекст: варианты ответа из ИИ + имя собеседника
# (нужно для статуса «✅ Отправлено для <Имя>: …» после нажатия кнопки)
PENDING: dict[tuple[int, int], dict[str, Any]] = {}


@dataclass
class EditCtx:
    """Контекст доработки черновика (AI-refinement)."""

    peer_id: int          # собеседник, которому уйдёт ответ
    peer_name: str
    peer_msg_id: int      # id сообщения собеседника (для reply_to_message_id)
    original: str         # исходное сообщение собеседника
    draft: str            # текущий черновик


# message_id сообщения-черновика (доработки) -> контекст правки
EDIT_CTX: dict[int, EditCtx] = {}
# Ключи (peer_id, message_id), для которых отправка уже идёт (защита от двойного клика)
IN_FLIGHT: set[tuple[int, int]] = set()


@dataclass
class GenCtx:
    """Контекст команды /con (генератор произвольных текстов)."""

    instruction: str            # исходный запрос пользователя
    variants: list[str]         # сгенерированные варианты
    selected: str = ""          # выбранный вариант
    target: Any = None          # контакт для отправки (int id или str username)
    target_name: str = ""       # имя/метка контакта


# message_id сообщения-результата /con -> контекст генератора
GEN_CTX: dict[int, GenCtx] = {}

# --- Автопилот: контакты (@username или id), отвечаем без кнопок ---
AUTO_USERS_FILE = os.path.join("data", "auto_users.json")
AUTO_USERS: set[str] = set()


def _load_auto_users() -> None:
    global AUTO_USERS
    try:
        with open(AUTO_USERS_FILE, "r", encoding="utf-8") as fh:
            AUTO_USERS = set(json.load(fh))
    except (FileNotFoundError, ValueError, OSError):
        AUTO_USERS = set()


def _save_auto_users() -> None:
    os.makedirs(os.path.dirname(AUTO_USERS_FILE) or ".", exist_ok=True)
    with open(AUTO_USERS_FILE, "w", encoding="utf-8") as fh:
        json.dump(sorted(AUTO_USERS), fh, ensure_ascii=False, indent=2)


def _normalize_ref(ref: str) -> Optional[str]:
    """Нормализует упоминание контакта: '@username' -> '@username', число -> int."""
    ref = ref.strip()
    if ref.startswith("@"):
        name = ref[1:].strip()
        return "@" + name.lower() if name else None
    if ref.lstrip("-").isdigit():
        return str(int(ref))
    return None


def _is_auto_peer(peer: Any) -> bool:
    """Проверяет, включён ли автопилот для данного собеседника."""
    for ref in AUTO_USERS:
        if ref.startswith("@"):
            uname = getattr(peer, "username", None)
            if uname and ref[1:] == uname.lower():
                return True
        elif str(getattr(peer, "id", "")) == ref:
            return True
    return False


def _auto_list() -> str:
    return "\n".join(f"• {r}" for r in sorted(AUTO_USERS)) or "— пусто —"


async def _add_auto_user(ref: str) -> None:
    canonical = _normalize_ref(ref)
    if not canonical:
        await bot_api.send_message(
            owner_id, "Некорректный контакт. Формат: /avto @username или /avto 123456789"
        )
        return
    AUTO_USERS.add(canonical)
    _save_auto_users()
    await bot_api.send_message(
        owner_id,
        f"🤖 Автопилот включён для <b>{canonical}</b>.\n\n"
        f"Сейчас в списке:\n{_auto_list()}",
        parse_mode="HTML",
    )


async def _remove_auto_user(ref: str) -> None:
    canonical = _normalize_ref(ref)
    if not canonical:
        await bot_api.send_message(
            owner_id, "Некорректный контакт. Формат: /unavto @username или /unavto 123456789"
        )
        return
    if canonical not in AUTO_USERS:
        await bot_api.send_message(owner_id, f"{canonical} не был в списке автопилота.")
        return
    AUTO_USERS.discard(canonical)
    _save_auto_users()
    await bot_api.send_message(
        owner_id,
        f"Автопилот выключен для <b>{canonical}</b>.\n\nСейчас в списке:\n{_auto_list()}",
        parse_mode="HTML",
    )


_load_auto_users()

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def esc_md(text: str) -> str:
    """Экранирует спецсимволы Markdown (чтобы текст собеседника не ломал разметку)."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def esc_html(text: str) -> str:
    """Экранирует спецсимволы HTML (для сообщений чата с ботом, parse_mode=HTML)."""
    return html.escape(str(text), quote=True)


def describe_media(message: Message) -> str:
    return str(message.media.value) if message.media else "text"


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models"
    "/{model}:generateContent"
)
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

AI_SYSTEM_PROMPT = (
    "Ты — ассистент, который помогает владельцу Telegram-аккаунта отвечать в личных "
    "сообщениях. По сообщению собеседника придумай короткие и естественные варианты "
    "ответа от первого лица, по одному на строку, пронумерованные. Никаких пояснений, "
    "преамбул и лишнего текста — только сами варианты."
)

REFINE_SYSTEM_PROMPT = (
    "Ты — ассистент, помогающий владельцу Telegram-аккаунта дорабатывать черновик "
    "ответа на сообщение собеседника. В ТОЧНОСТИ выполняй указание пользователя: "
    "стиль, язык, тональность, включая сленг, эмоциональную окраску и ненормативную "
    "лексику, если пользователь просит отвечать дерзко или с матом. Если пользователь "
    "просит написать на конкретном языке (узбекский, русский, английский и т.п.) — "
    "пиши строго на этом языке. При генерации текста на узбекском языке обязательно "
    "используй литературные нормы и правильную грамматику (например: «Tug'ilgan "
    "kuningiz bilan»/«Туғилган кунингиз билан», уважительные обращения «Yusuf aka», "
    "«Siz»). Избегай дословных переводов с русского. Верни только готовый текст "
    "ответа, без пояснений, преамбул и лишнего текста."
)

GEMINI_SAFETY = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


def _build_user_prompt(text: str, peer_name: str) -> str:
    return (
        f"Собеседник: {peer_name or 'Незнакомец'}\n"
        f"Сообщение: {text}\n\n"
        f"Предложи {CFG.max_suggestions} варианта ответа (только варианты, по одному "
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
        if len(out) >= CFG.max_suggestions:
            break
    if not out and (raw or "").strip():
        return [(raw or "").strip().strip('"')]
    return out


async def _gemini_generate(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Вызов Gemini. Возвращает текст ответа или None при ошибке/лимите."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.info("GEMINI_API_KEY не задана — пропускаем Gemini")
        return None
    body = {
        "contents": [{"parts": [{"text": user_prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "safetySettings": GEMINI_SAFETY,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 500},
    }
    try:
        async with http_session.post(
            GEMINI_API_URL.format(model=GEMINI_MODEL),
            params={"key": api_key},
            json=body,
            timeout=aiohttp.ClientTimeout(total=CFG.ai_timeout),
        ) as resp:
            if resp.status >= 400:
                err = (await resp.text(errors="replace"))[:300]
                logger.warning("Gemini %s: HTTP %s %s", GEMINI_MODEL, resp.status, err)
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ошибка обращения к Gemini (%s): %s", GEMINI_MODEL, exc)
        return None
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    return "\n".join(p.get("text", "") for p in parts).strip() or None


async def _groq_generate(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Вызов Groq (фоллбек). Возвращает текст ответа или None при ошибке."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.info("GROQ_API_KEY не задана — пропускаем Groq")
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
        async with http_session.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=aiohttp.ClientTimeout(total=CFG.ai_timeout),
        ) as resp:
            if resp.status >= 400:
                err = (await resp.text(errors="replace"))[:300]
                logger.warning("Groq %s: HTTP %s %s", GROQ_MODEL, resp.status, err)
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ошибка обращения к Groq (%s): %s", GROQ_MODEL, exc)
        return None
    try:
        return (data["choices"][0]["message"]["content"] or "").strip() or None
    except (KeyError, IndexError, TypeError):
        logger.warning("Groq вернул неожиданный ответ: %s", data)
        return None


async def _generate_with_fallback(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Gemini, при лимитах/ошибках — автоматический фоллбек на Groq."""
    raw = await _gemini_generate(system_prompt, user_prompt)
    if raw is None:
        logger.info("Gemini недоступен/лимит — автоматический фоллбек на Groq")
        raw = await _groq_generate(system_prompt, user_prompt)
    return raw


async def generate_suggestions(text: str, peer_name: str) -> list[str]:
    """Генерирует варианты ответа напрямую: Gemini, при лимитах — Groq."""
    raw = await _generate_with_fallback(
        AI_SYSTEM_PROMPT, _build_user_prompt(text, peer_name)
    )
    suggestions = _parse_suggestions(raw) if raw else []
    logger.info("Сгенерировано вариантов ответа: %s", len(suggestions))
    return suggestions


async def refine_draft(original: str, draft: str, instruction: str) -> Optional[str]:
    """AI-доработка черновика по произвольному указанию пользователя."""
    user_prompt = _build_refine_prompt(original, draft, instruction)
    refined = await _generate_with_fallback(REFINE_SYSTEM_PROMPT, user_prompt)
    if refined:
        logger.info("Черновик доработан по указанию: %s", instruction[:60])
    return refined


CONTENT_SYSTEM_PROMPT = (
    "Ты — креативный ассистент, который по запросу владельца Telegram-аккаунта "
    "придумывает готовые тексты (поздравления, отмазки, сообщения и т.п.). "
    "В ТОЧНОСТИ выполняй запрос: тему, цель, стиль и ЯЗЫК — если указан конкретный "
    "язык (узбекский, русский, английский и т.п.), пиши строго на нём. При генерации "
    "текста на узбекском языке обязательно используй литературные нормы и правильную "
    "грамматику (например: «Tug'ilgan kuningiz bilan»/«Туғилган кунингиз билан», "
    "уважительные обращения «Yusuf aka», «Siz»). Избегай дословных переводов с русского. "
    "Допускаются разговорный сленг, жёсткий юмор и ненормативная лексика, если "
    "пользователь просит. Верни ровно 3 варианта, по одному на строку, пронумерованные. "
    "Без пояснений, преамбул и лишнего текста."
)


async def generate_content(instruction: str) -> list[str]:
    """Генерация 3 готовых текстов по произвольному запросу (команда /con)."""
    raw = await _generate_with_fallback(
        CONTENT_SYSTEM_PROMPT, f"Запрос: {instruction}"
    )
    variants = _parse_suggestions(raw) if raw else []
    logger.info("Сгенерировано текстов по запросу: %s", len(variants))
    return variants

# ---------------------------------------------------------------------------
# Обработка входящих ЛС
# ---------------------------------------------------------------------------


async def handle_incoming(message: Message) -> None:
    logger.info(
        f"Детектор: получено сообщение {message.id} от {message.from_user.id if message.from_user else 'unknown'}, me={message.from_user.is_self if message.from_user else False}"
    )
    # Отсечка: не обрабатываем собственные исходящие сообщения (с других устройств)
    if message.from_user and message.from_user.is_self:
        return
    if message.chat.id == service_chat_id:
        return  # не обрабатываем собственный служебный чат
    # В режиме bot_chat игнорируем сообщения управляющего бота: иначе его кнопки
    # ушли бы в генерацию ИИ как «входящее ЛС» и породили бы бесконечный цикл
    if bot_user_id is not None and (
        message.chat.id == bot_user_id
        or (message.from_user and message.from_user.id == bot_user_id)
    ):
        return

    peer = message.from_user or message.sender_chat
    if peer is None:
        return

    payload: dict[str, Any] = {
        "event": "incoming_message",
        "peer_id": peer.id,
        "peer_name": (
            getattr(peer, "first_name", None)
            or getattr(peer, "title", None)
            or str(peer.id)
        ),
        "peer_username": getattr(peer, "username", None),
        "message_id": message.id,
        "text": (message.text or message.caption or "").strip(),
        "chat_type": "private",
        "timestamp": int((message.date or datetime.now()).timestamp()),
        "is_forwarded": bool(message.forward_from or message.forward_sender_name),
        "media_type": describe_media(message),
    }
    logger.info(
        "Получено ЛС от %s (%s): %s",
        payload["peer_id"],
        payload["peer_name"],
        payload["text"][:80] or "(без текста)",
    )

    # Автопилот: для контактов из списка ответ уходит сразу, без кнопок
    if _is_auto_peer(peer):
        await handle_auto_reply(payload)
        return

    suggestions = await generate_suggestions(payload["text"], payload["peer_name"])

    if not suggestions:
        await notify_owner(
            f"⚠️ Не удалось получить варианты ответа для {payload['peer_name']}.\n"
            "Проверьте GEMINI_API_KEY / GROQ_API_KEY в .env."
        )
        return

    # Сохраняем варианты — по ним inline-кнопки командуют отправку
    PENDING[(payload["peer_id"], payload["message_id"])] = {
        "suggestions": suggestions,
        "peer_name": payload["peer_name"],
        "original": payload["text"],
    }

    if CFG.ai_mode == "userbot":
        await show_native_buttons(payload, suggestions)
    elif CFG.ai_mode == "bot_chat":
        await show_bot_chat_buttons(payload, suggestions)
    else:
        logger.info("Варианты получены (режим bot): %s", suggestions)


def _peer_ref(payload: dict[str, Any]) -> str:
    uname = payload.get("peer_username")
    if uname:
        return f"@{uname}"
    return str(payload.get("peer_id"))


async def handle_auto_reply(payload: dict[str, Any]) -> None:
    """Автопилот: генерируем ответ и сразу отправляем собеседнику без кнопок."""
    ref = _peer_ref(payload)
    variants = await generate_suggestions(payload["text"], payload["peer_name"])
    if not variants:
        await notify_owner(
            f"⚠️ Авто-ответ не удался для {ref}: не получены варианты. "
            "Проверьте GEMINI_API_KEY / GROQ_API_KEY."
        )
        return
    answer = variants[0]
    try:
        await client.send_message(
            payload["peer_id"], answer, reply_to_message_id=payload["message_id"]
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось отправить авто-ответ для %s", ref)
        await notify_owner(f"⚠️ Не удалось отправить авто-ответ для {ref}: {exc}")
        return
    logger.info("Авто-ответ отправлен для %s", ref)
    await notify_owner(f"🤖 Авто-ответ отправлен для {ref}: {answer}")


async def show_native_buttons(payload: dict[str, Any], suggestions: list[str]) -> None:
    """Режим AI_MODE=userbot: рисуем кнопки сами в служебном чате (Избранное).

    Кнопки [1] [2] [3] — номер соответствует списку вариантов в тексте сообщения.
    В callback_data передаём только peer_id + message_id + index (лимит 64 байта,
    текст варианта не влезает), а сам текст берётся из PENDING.
    """
    buttons: list[list[InlineKeyboardButton]] = []
    buttons.append(
        [
            InlineKeyboardButton(
                f"[{i}]",
                callback_data=f"send|{payload['peer_id']}|{payload['message_id']}|{i - 1}",
            )
            for i in range(1, len(suggestions) + 1)
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                "✏️ Редактировать",
                callback_data=f"edit|{payload['peer_id']}|{payload['message_id']}|0",
            ),
            InlineKeyboardButton(
                "⏭ Пропустить",
                callback_data=f"skip|{payload['peer_id']}|{payload['message_id']}|0",
            ),
        ]
    )

    body = (
        f"💬 **{esc_md(payload['peer_name'])}** (`{payload['peer_id']}`) · {payload['media_type']}\n"
        f"_{esc_md(payload['text'][:300])}_\n\n"
        f"🤖 Варианты ответа:\n"
        + "\n".join(f"{i}. {esc_md(s)}" for i, s in enumerate(suggestions, start=1))
    )
    await client.send_message(
        service_chat_id,
        body,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def notify_owner(text: str) -> None:
    """Отправляет служебное сообщение владельцу.

    В режиме bot_chat — через бота в личный чат; иначе — в служебный чат.
    """
    if CFG.ai_mode == "bot_chat" and bot_api is not None and owner_id is not None:
        try:
            await bot_api.send_message(owner_id, esc_html(text), parse_mode="HTML")
            return
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось отправить уведомление через бота", exc_info=True)
    await client.send_message(service_chat_id, text)


async def show_bot_chat_buttons(payload: dict[str, Any], suggestions: list[str]) -> None:
    """Режим AI_MODE=bot_chat: кнопки в личном чате с ботом @myaccounttbot.

    Сообщение шлётся через Bot API (BOT_TOKEN) владельцу; кнопки [1] [2] [3] —
    настоящие inline-кнопки. Нажатия ловит встроенный long-polling
    (bot_handle_callback): выбранный текст уходит собеседнику от имени личного
    аккаунта, а сообщение редактируется со статусом «✅ Отправлено …».
    """
    buttons: list[list[dict[str, Any]]] = [
        [
            {
                "text": f"[{i}]",
                "callback_data": f"send|{payload['peer_id']}|{payload['message_id']}|{i - 1}",
            }
            for i in range(1, len(suggestions) + 1)
        ],
        [
            {
                "text": "✏️ Ред.",
                "callback_data": f"edit|{payload['peer_id']}|{payload['message_id']}|0",
            },
            {
                "text": "⏭ Пропустить",
                "callback_data": f"skip|{payload['peer_id']}|{payload['message_id']}|0",
            },
        ],
    ]
    body = (
        f"💬 <b>{esc_html(payload['peer_name'])}</b> (<code>{payload['peer_id']}</code>) · {payload['media_type']}\n"
        f"<i>«{esc_html(payload['text'][:300])}»</i>\n\n"
        f"🤖 Варианты ответа:\n"
        + "\n".join(f"<b>{i}.</b> {esc_html(s)}" for i, s in enumerate(suggestions, start=1))
    )
    try:
        await bot_api.send_message(
            owner_id,
            body,
            parse_mode="HTML",
            reply_markup={"inline_keyboard": buttons},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить кнопки в чат с ботом")
        # Страховка: дублируем варианты в служебный чат без кнопок,
        # чтобы контекст не потерялся, пока чинится бот
        try:
            await client.send_message(
                service_chat_id,
                f"⚠️ Кнопки в чат с ботом не отправились (проверьте BOT_TOKEN).\n\n"
                f"{payload['peer_name']}:\n"
                + "\n".join(f"{i}. {s}" for i, s in enumerate(suggestions, start=1)),
            )
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось продублировать варианты в служебный чат", exc_info=True)


async def _safe_answer(cb: CallbackQuery, text: str) -> None:
    """Отвечаем на callback_query (снимает «крутилку» у кнопки).

    На пользовательских аккаунтах SetBotCallbackAnswer может падать — ошибка
    не должна блокировать саму отправку/правку сообщения.
    """
    try:
        await cb.answer(text)
    except Exception:  # noqa: BLE001
        logger.debug("Не удалось ответить на callback_query (не критично)", exc_info=True)


async def _replace_buttons_with_status(cb: CallbackQuery, status: str) -> None:
    """Редактирует сообщение с кнопками: убирает клавиатуру и дописывает статус.

    reply_markup=InlineKeyboardMarkup([]) (пустая клавиатура) — единственный
    способ снять inline-кнопки при правке (None оставляет их как есть).
    """
    if not cb.message:
        return
    base = cb.message.text.markdown if cb.message.text else ""
    try:
        await cb.message.edit_text(
            f"{base}\n\n{esc_md(status)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([]),
        )
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось отредактировать сообщение кнопок", exc_info=True)


async def handle_callback(cb: CallbackQuery) -> None:
    """Нажатие на inline-кнопку в режиме AI_MODE=userbot.

    В callback_data только action|peer_id|message_id|index (лимит 64 байта),
    а текст варианта и имя собеседника берутся из PENDING. После действия
    кнопки заменяются статусом «✅ Отправлено для <Имя>: "текст"».
    """
    if not cb.data:
        return
    try:
        action, peer_id_str, msg_id_str, idx_str = cb.data.split("|", 3)
        peer_id, msg_id, idx = int(peer_id_str), int(msg_id_str), int(idx_str)
    except (ValueError, AttributeError):
        await _safe_answer(cb, "Неизвестная кнопка")
        return

    entry = PENDING.get((peer_id, msg_id)) or {}
    items = entry.get("suggestions") or []
    peer_name = entry.get("peer_name") or str(peer_id)

    if action == "skip":
        await _safe_answer(cb, "Пропущено ⏭")
        await _replace_buttons_with_status(cb, "⏭ Пропущено")
        PENDING.pop((peer_id, msg_id), None)
        return

    if idx >= len(items):
        await _safe_answer(cb, "Контекст устарел (возможно, перезапуск)")
        return
    variant = items[idx]

    if action == "edit":
        sent = await client.send_message(
            service_chat_id,
            f"✏️ Редактируйте черновик. Пришлите правку ответом на это сообщение, "
            f"например: «сделай вежливее», «перепиши с матом», «на узбекском языке» "
            f"(или /cancel для отмены).\n\nЧерновик:\n{variant}",
        )
        EDIT_CTX[sent.id] = EditCtx(
            peer_id=peer_id,
            peer_name=peer_name,
            peer_msg_id=msg_id,
            original=entry.get("original") or "",
            draft=variant,
        )
        PENDING.pop((peer_id, msg_id), None)
        await _safe_answer(cb, "Сообщение для правки создано")
        await _replace_buttons_with_status(cb, "✏️ Создано сообщение для правки")
        return

    if action == "send":
        # Отправляем выбранный вариант собеседнику как ответ на его сообщение
        try:
            await client.send_message(peer_id, variant, reply_to_message_id=msg_id)
        except Exception:  # noqa: BLE001
            # Не удалось отправить (заблокирован/удалён собеседник и т.п.) —
            # не «вешаем» кнопку, а показываем ошибку и оставляем варианты
            logger.exception("Не удалось отправить ответ собеседнику id=%s", peer_id)
            await _safe_answer(cb, "⚠️ Не удалось отправить (проверьте доступ)")
            return
        PENDING.pop((peer_id, msg_id), None)
        await _safe_answer(cb, "Отправлено ✅")
        await _replace_buttons_with_status(
            cb, f"✅ Отправлено для {peer_name}: \"{variant}\""
        )


async def handle_edited(message: Message) -> None:
    """Правка сообщения в служебном чате -> AI-доработка и отправка собеседнику."""
    if message.chat.id != service_chat_id:
        return
    ctx = EDIT_CTX.get(message.id)
    if ctx is None or not message.text:
        return
    if "/cancel" in message.text:
        EDIT_CTX.pop(message.id, None)
        await message.reply("❌ Отменено.")
        return

    refined = await refine_draft(ctx.original, ctx.draft, message.text.strip())
    if not refined:
        await message.reply("⚠️ Не удалось доработать текст. Попробуйте ещё раз.")
        return
    try:
        await client.send_message(ctx.peer_id, refined, reply_to_message_id=ctx.peer_msg_id)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить доработанный ответ собеседнику id=%s", ctx.peer_id)
        await message.reply("⚠️ Не удалось отправить (проверьте доступ). Попробуйте ещё раз.")
        return
    logger.info("Отправлен доработанный ответ собеседнику id=%s", ctx.peer_id)
    EDIT_CTX.pop(message.id, None)
    await message.reply(f"✅ Отправлено собеседнику ({ctx.peer_name}).")

# ---------------------------------------------------------------------------
# Telegram Bot (@myaccounttbot): long-polling и кнопки в личном чате с владельцем
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "🤖 Пульт AI-автоответчика.\n\n"
    "Команды:\n"
    "/help — эта справка\n"
    "/con <запрос> — генератор текстов (3 варианта):\n"
    "  /con @ky_747 поздравь Юсуф ака с ДР на узбекском\n"
    "  /con Придумай причину, почему я заболел и не приду сегодня\n"
    "/avto @username — включить автопилот для контакта\n"
    "/unavto @username — выключить автопилот для контакта\n"
    "/cancel — отменить текущую генерацию/правку\n\n"
    "Когда вам пишут в ЛС, приходят варианты ответа с кнопками:\n"
    "• [1] [2] [3] — отправить выбранный вариант собеседнику\n"
    "• ✏️ Ред. — доработать черновик (например «сделай вежливее», «с матом»,\n"
    "  «на узбекском языке») — ответьте правкой на черновик\n"
    "• ⏭ Пропустить — ничего не отправлять\n\n"
    "После доработки ИИ пришлёт обновлённый вариант с кнопками:\n"
    "🚀 Отправить · ✏️ Редактировать ещё · ❌ Отмена\n\n"
    "Автопилот: для контактов из списка ответ уходит собеседнику сразу,\n"
    "в этот чат приходит только уведомление."
)


def _refine_inline_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🚀 Отправить", "callback_data": "rsend|0|0|0"}],
            [
                {"text": "✏️ Редактировать ещё", "callback_data": "redit|0|0|0"},
                {"text": "❌ Отмена", "callback_data": "rcancel|0|0|0"},
            ],
        ]
    }


async def _resolve_contact(ref: str) -> Optional[tuple[Any, str]]:
    """Распознаёт контакт из ответа: '@username' или числовой ID."""
    ref = ref.strip()
    if ref.startswith("@"):
        username = ref[1:].lower()
        try:
            user = await client.get_users(username)
            display = user.username or user.first_name or f"@{username}"
            return user.id, display
        except Exception:  # noqa: BLE001
            return username, f"@{username}"
    if ref.lstrip("-").isdigit():
        uid = int(ref)
        try:
            user = await client.get_users(uid)
            display = user.first_name or str(uid)
        except Exception:  # noqa: BLE001
            display = str(uid)
        return uid, display
    return None


def _recipient_status_html(target: Any, target_name: str) -> str:
    """Строка статуса получателя для сообщений /con."""
    if target is not None:
        label = target_name or str(target)
        return f"🎯 Получатель: <b>{esc_html(label)}</b>"
    return "🎯 Получатель: не указан — отправьте <b>@username</b>"


def _extract_con_recipient(raw: str) -> tuple[Any, str, str]:
    """Извлекает получателя из начала /con-запроса: '@username' или ID.

    Возвращает (target, target_name, оставшийся запрос).
    """
    raw = raw.strip()
    if raw.startswith("@"):
        head, _, rest = raw.partition(" ")
        if rest:
            return head, head, rest.strip()
    else:
        head, _, rest = raw.partition(" ")
        if head.lstrip("-").isdigit() and rest:
            return int(head), str(int(head)), rest.strip()
    return None, "", raw


async def handle_con_command(arg: str) -> None:
    """Команда /con: генерирует 3 текста по запросу и присылает с кнопками.

    Получатель может быть указан прямо в команде: /con @ky_747 поздравь …
    """
    target, target_name, instruction = _extract_con_recipient(arg)
    if not instruction:
        await bot_api.send_message(
            owner_id,
            "Укажите запрос. Примеры:\n"
            "/con @ky_747 поздравь Юсуф ака с ДР на узбекском\n"
            "/con Придумай причину, почему я заболел и не приду сегодня",
        )
        return
    variants = await generate_content(instruction)
    if not variants:
        await bot_api.send_message(
            owner_id,
            "⚠️ Не удалось сгенерировать тексты. Проверьте GEMINI_API_KEY / GROQ_API_KEY в .env.",
        )
        return
    body = (
        "🎨 Генератор текстов\n\n"
        f"{_recipient_status_html(target, target_name)}\n"
        f"Тема: <i>«{esc_html(instruction)}»</i>\n\n"
        + "\n".join(f"<b>{i}.</b> {esc_html(v)}" for i, v in enumerate(variants, start=1))
    )
    buttons: list[list[dict[str, Any]]] = [
        [
            {"text": f"[{i}]", "callback_data": f"gensel|0|0|{i - 1}"}
            for i in range(1, len(variants) + 1)
        ],
        [{"text": "❌ Отмена", "callback_data": "gencancel|0|0|0"}],
    ]
    try:
        sent = await bot_api.send_message(
            owner_id, body, parse_mode="HTML", reply_markup={"inline_keyboard": buttons}
        )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить результат /con")
        return
    GEN_CTX[sent["message_id"]] = GenCtx(
        instruction=instruction,
        variants=variants,
        target=target,
        target_name=target_name,
    )


async def _handle_gen_callback(
    action: str,
    cb_id: Any,
    chat_id: Any,
    message_id: Any,
    base: str,
    idx: int,
    gen: GenCtx,
) -> None:
    """Обработка кнопок команды /con."""
    if action == "gensel":
        if idx >= len(gen.variants):
            await bot_api.answer_callback_query(cb_id, "Контекст устарел (возможно, перезапуск)")
            return
        gen.selected = gen.variants[idx]
        await bot_api.answer_callback_query(cb_id, f"Выбран вариант {idx + 1}")
        await bot_api.edit_message_text(
            owner_id,
            message_id,
            f"Выбран вариант <b>{idx + 1}</b>:\n\n{esc_html(gen.selected)}\n\n"
            f"{_recipient_status_html(gen.target, gen.target_name)}\n\n"
            "Пришлите правку ответом, чтобы доработать текст, либо нажмите 🚀 Отправить.",
            parse_mode="HTML",
            reply_markup=_refine_inline_keyboard(),
        )
        return
    if action == "rsend":
        if not gen.selected:
            await bot_api.answer_callback_query(cb_id, "Сначала выберите вариант")
            return
        if gen.target is None:
            # Получатель не указан — запрашиваем отдельным чётким сообщением
            ask = await bot_api.send_message(
                owner_id,
                "🎯 Получатель не указан.\n"
                "Ответьте <b>@username</b> или цифровым ID, кому отправить текст "
                "(например: <b>@ky_747</b> или <b>123456789</b>).",
                parse_mode="HTML",
            )
            GEN_CTX[ask["message_id"]] = gen
            await bot_api.answer_callback_query(cb_id, "Укажите получателя")
            return
        try:
            await client.send_message(gen.target, gen.selected)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить сгенерированный текст")
            await bot_api.answer_callback_query(cb_id, "⚠️ Не удалось отправить (проверьте контакт)")
            return
        label = gen.target_name or str(gen.target)
        GEN_CTX.pop(message_id, None)
        await bot_api.answer_callback_query(cb_id, "Отправлено ✅")
        await bot_edit_with_status(
            chat_id, message_id, base, f"✅ Отправлено для {label}: \"{gen.selected}\""
        )
        return
    if action == "redit":
        if not gen.selected:
            await bot_api.answer_callback_query(cb_id, "Сначала выберите вариант")
            return
        await bot_api.edit_message_text(
            owner_id,
            message_id,
            f"Текущий текст:\n\n{esc_html(gen.selected)}\n\n"
            f"{_recipient_status_html(gen.target, gen.target_name)}\n\n"
            "Пришлите правку ответом на это сообщение (или укажите получателя).",
            parse_mode="HTML",
            reply_markup={"inline_keyboard": []},
        )
        await bot_api.answer_callback_query(cb_id, "Пришлите правку ответом")
        return
    if action in ("rcancel", "gencancel"):
        GEN_CTX.pop(message_id, None)
        await bot_edit_with_status(chat_id, message_id, base, "❌ Отменено")
        await bot_api.answer_callback_query(cb_id, "Отменено ❌")
        return


async def bot_handle_update(update: dict[str, Any]) -> None:
    """Точка входа для обновлений из long-polling бота."""
    if "callback_query" in update:
        await bot_handle_callback(update["callback_query"])
    elif "message" in update:
        await bot_handle_message(update["message"])


async def bot_edit_with_status(
    chat_id: int, message_id: int, base: str, status: str
) -> None:
    """Редактирует сообщение бота: убирает inline-кнопки и дописывает статус."""
    new_text = f"{base}\n\n{esc_html(status)}" if base else esc_html(status)
    try:
        await bot_api.edit_message_text(
            chat_id,
            message_id,
            new_text,
            parse_mode="HTML",
            reply_markup={"inline_keyboard": []},
        )
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось отредактировать сообщение бота", exc_info=True)


async def bot_handle_callback(cb: dict[str, Any]) -> None:
    """Нажатие на inline-кнопку в чате с ботом (режим bot_chat).

    В callback_data только action|peer_id|message_id|index (лимит 64 байта),
    текст варианта и имя собеседника берутся из PENDING. После действия кнопки
    заменяются статусом «✅ Отправлено для <Имя>: "текст"».
    """
    cb_id = cb.get("id")
    user_id = (cb.get("from") or {}).get("id")
    if user_id != owner_id:
        await bot_api.answer_callback_query(cb_id, "⛔ Кнопки доступны только владельцу")
        return
    data = cb.get("data") or ""
    try:
        action, peer_id_str, msg_id_str, idx_str = data.split("|", 3)
        peer_id, msg_id, idx = int(peer_id_str), int(msg_id_str), int(idx_str)
    except (ValueError, AttributeError):
        await bot_api.answer_callback_query(cb_id, "Неизвестная кнопка")
        return

    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    base = message.get("text") or ""

    # Кнопки доработанного черновика (rsend/redit/rcancel) и генератора /con
    if action in ("rsend", "redit", "rcancel", "gensel", "gencancel"):
        gen = GEN_CTX.get(message_id)
        if gen is not None:
            await _handle_gen_callback(
                action, cb_id, chat_id, message_id, base, idx, gen
            )
            return
        ctx = EDIT_CTX.get(message_id)
        if ctx is None:
            await bot_api.answer_callback_query(cb_id, "Контекст устарел (возможно, перезапуск)")
            await bot_edit_with_status(chat_id, message_id, base, "⏳ Контекст устарел")
            return
        if action == "rsend":
            try:
                await client.send_message(
                    ctx.peer_id, ctx.draft, reply_to_message_id=ctx.peer_msg_id
                )
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось отправить доработанный ответ id=%s", ctx.peer_id)
                await bot_api.answer_callback_query(cb_id, "⚠️ Не удалось отправить (проверьте доступ)")
                return
            EDIT_CTX.pop(message_id, None)
            await bot_api.answer_callback_query(cb_id, "Отправлено ✅")
            await bot_edit_with_status(
                chat_id, message_id, base, f"✅ Отправлено для {ctx.peer_name}: \"{ctx.draft}\""
            )
            return
        if action == "redit":
            await bot_api.edit_message_text(
                owner_id,
                message_id,
                "✏️ Текущий черновик:\n\n" + esc_html(ctx.draft)
                + "\n\nПришлите следующую правку <b>ответом на это сообщение</b> (или /cancel).",
                parse_mode="HTML",
                reply_markup={"inline_keyboard": []},
            )
            await bot_api.answer_callback_query(cb_id, "Жду следующую правку")
            return
        if action == "rcancel":
            EDIT_CTX.pop(message_id, None)
            await bot_edit_with_status(chat_id, message_id, base, "❌ Отменено")
            await bot_api.answer_callback_query(cb_id, "Отменено ❌")
            return

    key = (peer_id, msg_id)
    if key in IN_FLIGHT:
        await bot_api.answer_callback_query(cb_id, "⏳ Отправка уже выполняется…")
        return

    entry = PENDING.get(key) or {}
    items = entry.get("suggestions") or []
    peer_name = entry.get("peer_name") or str(peer_id)

    if action == "skip":
        await bot_api.answer_callback_query(cb_id, "Пропущено ⏭")
        await bot_edit_with_status(chat_id, message_id, base, "⏭ Пропущено")
        PENDING.pop(key, None)
        return

    if idx >= len(items):
        # Контекст потерян (например, после перезапуска) — снимаем кнопки
        await bot_api.answer_callback_query(cb_id, "Контекст устарел (возможно, перезапуск)")
        await bot_edit_with_status(chat_id, message_id, base, "⏳ Контекст устарел")
        return

    variant = items[idx]

    if action == "edit":
        sent = await bot_api.send_message(
            owner_id,
            "✏️ Редактируйте черновик. Пришлите правку <b>ответом на это сообщение</b>, "
            "например: «сделай вежливее», «перепиши с матом», «на узбекском языке» "
            "(или /cancel для отмены).\n\nЧерновик:\n" + esc_html(variant),
            parse_mode="HTML",
        )
        EDIT_CTX[sent["message_id"]] = EditCtx(
            peer_id=peer_id,
            peer_name=peer_name,
            peer_msg_id=msg_id,
            original=entry.get("original") or "",
            draft=variant,
        )
        PENDING.pop(key, None)
        await bot_api.answer_callback_query(cb_id, "Сообщение для правки отправлено")
        await bot_edit_with_status(chat_id, message_id, base, "✏️ Создано сообщение для правки")
        return

    if action == "send":
        # Отправляем выбранный вариант собеседнику как ответ на его сообщение
        IN_FLIGHT.add(key)
        try:
            await client.send_message(peer_id, variant, reply_to_message_id=msg_id)
        except Exception:  # noqa: BLE001
            IN_FLIGHT.discard(key)
            logger.exception("Не удалось отправить ответ собеседнику id=%s", peer_id)
            await bot_api.answer_callback_query(cb_id, "⚠️ Не удалось отправить (проверьте доступ)")
            return
        IN_FLIGHT.discard(key)
        PENDING.pop(key, None)
        await bot_api.answer_callback_query(cb_id, "Отправлено ✅")
        await bot_edit_with_status(
            chat_id, message_id, base, f"✅ Отправлено для {peer_name}: \"{variant}\""
        )


async def bot_handle_message(msg: dict[str, Any]) -> None:
    """Сообщения владельца в чате с ботом: команды, контакт/правка для /con, правка черновика."""
    if (msg.get("from") or {}).get("id") != owner_id:
        return  # бот игнорирует посторонних
    text = (msg.get("text") or "").strip()
    reply_to = msg.get("reply_to_message") or {}
    bot_msg_id = reply_to.get("message_id")

    if "/cancel" in text:
        removed = False
        if bot_msg_id:
            if EDIT_CTX.pop(bot_msg_id, None) is not None:
                removed = True
            if GEN_CTX.pop(bot_msg_id, None) is not None:
                removed = True
            if removed:
                await bot_edit_with_status(owner_id, bot_msg_id, "", "❌ Отменено")
        await bot_api.send_message(owner_id, "Отменено ✅")
        return

    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/start", "/help"):
            await bot_api.send_message(owner_id, HELP_TEXT)
            return
        if cmd == "/con":
            if not arg:
                await bot_api.send_message(
                    owner_id,
                    "Использование: /con <запрос>\n"
                    "Например:\n"
                    "/con Поздравь Юсуф ака с ДР на узбекском\n"
                    "/con Придумай причину, почему я заболел и не приду сегодня",
                )
                return
            await handle_con_command(arg)
            return
        if cmd == "/avto":
            if not arg:
                await bot_api.send_message(owner_id, "Использование: /avto @username или /avto 123456789")
                return
            await _add_auto_user(arg)
            return
        if cmd == "/unavto":
            if not arg:
                await bot_api.send_message(owner_id, "Использование: /unavto @username или /unavto 123456789")
                return
            await _remove_auto_user(arg)
            return
        await bot_api.send_message(owner_id, "Неизвестная команда. Наберите /help для списка команд.")
        return

    if not text or not bot_msg_id:
        return  # простое сообщение без ответа — игнорируем

    # Ответ на сообщение генератора /con: контакт для отправки или правка текста
    gen = GEN_CTX.get(bot_msg_id)
    if gen is not None and gen.selected:
        # Текст начинается с @ или состоит из цифр — это получатель, а не правка
        head = text.split(None, 1)[0]
        is_contact = head.startswith("@") or head.lstrip("-").isdigit()
        if is_contact:
            contact = await _resolve_contact(head)
            if contact is None:
                await bot_api.send_message(
                    owner_id,
                    "⚠️ Не удалось распознать получателя. Формат: @username или цифровой ID.",
                )
                return
            gen.target, gen.target_name = contact
            await bot_api.edit_message_text(
                owner_id,
                bot_msg_id,
                f"{_recipient_status_html(gen.target, gen.target_name)}\n\n"
                f"{esc_html(gen.selected)}\n\nНажмите 🚀 Отправить.",
                parse_mode="HTML",
                reply_markup=_refine_inline_keyboard(),
            )
            await bot_api.send_message(
                owner_id, f"🎯 Получатель установлен: {gen.target_name}."
            )
            return
        refined = await refine_draft(gen.instruction, gen.selected, text)
        if not refined:
            await bot_api.send_message(owner_id, "⚠️ Не удалось доработать текст. Попробуйте ещё раз.")
            return
        gen.selected = refined
        await bot_api.edit_message_text(
            owner_id,
            bot_msg_id,
            f"✅ Применил: «{esc_html(text)}»\n\n{esc_html(refined)}\n\n"
            f"{_recipient_status_html(gen.target, gen.target_name)}\n\n"
            "Пришлите ещё правку или укажите получателя (@username / ID).",
            parse_mode="HTML",
            reply_markup=_refine_inline_keyboard(),
        )
        return

    # Ответ на черновик = строгое указание для AI-доработки текущего черновика
    ctx = EDIT_CTX.get(bot_msg_id)
    if ctx is None:
        return

    refined = await refine_draft(ctx.original, ctx.draft, text)
    if not refined:
        await bot_api.send_message(owner_id, "⚠️ Не удалось доработать текст. Попробуйте ещё раз.")
        return

    ctx.draft = refined
    await bot_api.edit_message_text(
        owner_id,
        bot_msg_id,
        f"✅ Применил: «{esc_html(text)}»\n\n{esc_html(refined)}",
        parse_mode="HTML",
        reply_markup=_refine_inline_keyboard(),
    )


# ---------------------------------------------------------------------------
# Pyrogram-клиент и регистрация обработчиков
# ---------------------------------------------------------------------------

session_str = os.getenv("SESSION_STRING")
logger.info("SESSION_STRING подтянута из env: %s", bool(session_str))
client = Client(CFG.session_name, api_id=CFG.api_id, api_hash=CFG.api_hash, workdir=".", session_string=session_str or None)


def service_chat_filter(_, __, message: Message) -> bool:
    return service_chat_id is not None and message.chat.id == service_chat_id


@client.on_message(filters.private)
async def on_incoming(_c: Client, message: Message) -> None:
    await handle_incoming(message)


@client.on_callback_query()
async def on_callback(_c: Client, cb: CallbackQuery) -> None:
    await handle_callback(cb)


@client.on_edited_message(filters.create(service_chat_filter))
async def on_edited(_c: Client, message: Message) -> None:
    await handle_edited(message)

# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


async def main() -> None:
    global http_session, service_chat_id, owner_id, bot_api, bot_user_id

    http_session = aiohttp.ClientSession()
    # Запускаем фоновый HTTP-сервер для healthcheck Render
    asyncio.create_task(healthcheck_server())
    await client.start()
    service_chat_id = client.me.id if CFG.service_chat.lower() == "me" else int(CFG.service_chat)
    owner_id = CFG.owner_id or client.me.id
    logger.info("Вошли как %s (id=%s), владелец id=%s", client.me.first_name, client.me.id, owner_id)

    poller_task: Optional[asyncio.Task] = None
    if CFG.ai_mode == "bot_chat":
        if not CFG.bot_token:
            raise SystemExit("AI_MODE=bot_chat требует BOT_TOKEN в .env (токен бота из BotFather)")
        bot_api = BotApiClient(CFG.bot_token, http_session)
        try:
            me = await bot_api.get_me()
        except BotApiError as exc:
            logger.error("Не удалось получить getMe по BOT_TOKEN: %s", exc)
            raise SystemExit("Проверьте BOT_TOKEN в .env (токен бота из BotFather)") from exc
        bot_user_id = me.get("id")
        logger.info("Управляющий бот: @%s (id=%s)", me.get("username"), bot_user_id)
        # Используем long polling -> сбрасываем вебхук, если он был зарегистрирован.
        # Если токеном пользуется ещё что-то — будет 409.
        try:
            await bot_api.delete_webhook(drop_pending_updates=True)
        except BotApiError as exc:
            logger.warning("Не удалось сбросить вебхук бота: %s", exc)
        await bot_api.send_message(
            owner_id,
            "🤖 AI-автоответчик запущен (режим: bot_chat). Слушаю входящие ЛС…",
        )
        poller = BotApiPoller(bot_api, bot_handle_update)
        poller_task = asyncio.create_task(poller.run())
        logger.info("Long-polling бота запущен (getUpdates)")
    else:
        await client.send_message(
            service_chat_id,
            f"🤖 AI-автоответчик запущен (режим: {CFG.ai_mode}). Слушаю входящие ЛС…",
        )

    try:
        await idle()
    finally:
        if poller_task is not None:
            poller_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller_task
        await http_session.close()
        await client.stop()


if __name__ == "__main__":
    try:
        # ВАЖНО: клиент (Client) создан на уровне модуля и привязан к циклу,
        # созданному хаком для Python 3.14 (см. выше). asyncio.run() создал бы
        # НОВЫЙ цикл, и фоновые задачи Pyrogram (диспетчер апдейтов) остались бы
        # на мёртвом цикле — апдейты бы не приходили. Запускаем main() на том же
        # цикле, что и клиент.
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")
