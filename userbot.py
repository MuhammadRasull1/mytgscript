#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-автоответчик для личного Telegram (userbot на Pyrogram + n8n + Gemini/Groq).

Архитектура:

  1. Собеседник пишет вам в ЛС -> Pyrogram-клиент (ваша сессия) ловит сообщение.
  2. userbot отправляет POST-вебхук в n8n (N8N_WEBHOOK_URL) с данными сообщения.
  3. n8n (workflow_ai_responder) вызывает Gemini/Groq и отвечает вебхуку JSON
     {"ok": true, "suggestions": [...]}; userbot сохраняет варианты в памяти
     (PENDING[(peer_id, msg_id)]).
  4. Режим AI_MODE=bot_chat (по умолчанию): варианты с настоящими inline-кнопками
     [1] [2] [3] [✏️ Ред.] [⏭ Пропустить] уходят через Bot API (BOT_TOKEN)
     в ваш личный чат с ботом @myaccounttbot. Нажатия ловит встроенный
     long-polling (bot_api.py) — n8n для кнопок не нужен.
  5. Вы нажимаете [1]/[2]/[3] -> выбранный текст уходит собеседнику (как ответ
     на его сообщение), а сообщение в чате с ботом редактируется:
     кнопки убираются, дописывается «✅ Отправлено для <Имя>: "текст"».
  6. Старые режимы: AI_MODE=userbot — кнопки в «Избранном», нажатия ловит сам
     userbot; AI_MODE=bot — кнопки рисует бот через n8n, нажатия обрабатывает
     n8n (workflow_send_callbacks) через POST /api/command.
  7. НИЧЕГО не отправляется автоматически — только по вашему явному действию.

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
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import aiohttp
from aiohttp import web
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

from bot_api import BotApiClient, BotApiError, BotApiPoller

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


def _resolve_webhook_url() -> str:
    """Полный URL вебхука n8n (из .env через python-dotenv).

    Приоритет: N8N_WEBHOOK_URL (полный адрес) -> WEBHOOK_URL (база, к ней
    добавляется /webhook/telegram-in) -> локальный дефолт.
    """
    url = os.getenv("N8N_WEBHOOK_URL") or os.getenv("WEBHOOK_URL") or ""
    if not url:
        return "http://localhost:5678/webhook/telegram-in"
    if "/webhook/telegram-in" not in url:
        url = url.rstrip("/") + "/webhook/telegram-in"
    return url


@dataclass
class Config:
    api_id: int
    api_hash: str
    session_name: str
    n8n_webhook_url: str
    n8n_timeout: int
    callback_host: str
    callback_port: int
    callback_api_key: str
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
            n8n_webhook_url=_resolve_webhook_url(),
            n8n_timeout=int(os.getenv("N8N_TIMEOUT_SEC", "90")),
            callback_host=os.getenv("CALLBACK_HOST", "0.0.0.0"),
            callback_port=int(os.getenv("CALLBACK_PORT", "8123")),
            callback_api_key=os.getenv("CALLBACK_API_KEY", "change-me-strong-secret"),
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

# (peer_id, message_id) -> контекст: варианты ответа из n8n + имя собеседника
# (нужно для статуса «✅ Отправлено для <Имя>: …» после нажатия кнопки)
PENDING: dict[tuple[int, int], dict[str, Any]] = {}
# message_id сообщения-черновика («отредактируй это» / черновик бота) -> (peer_id, peer_name)
EDIT_CTX: dict[int, tuple[int, str]] = {}
# Ключи (peer_id, message_id), для которых отправка уже идёт (защита от двойного клика)
IN_FLIGHT: set[tuple[int, int]] = set()

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


async def request_suggestions(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Отправляет вебхук в n8n и ждёт JSON-ответ с вариантами ответа."""
    logger.info("Отправка POST на вебхук n8n: %s", CFG.n8n_webhook_url)
    try:
        async with http_session.post(
            CFG.n8n_webhook_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=CFG.n8n_timeout),
        ) as resp:
            if resp.status >= 300:
                error_body = (await resp.text(errors="replace"))[:500]
                logger.warning(
                    "n8n вернул ошибку (вебхук %s): статус %s, ответ: %s",
                    CFG.n8n_webhook_url,
                    resp.status,
                    error_body or "(пусто)",
                )
                return None
            logger.info(
                "n8n ответил успешно (вебхук %s, статус %s)",
                CFG.n8n_webhook_url,
                resp.status,
            )
            return await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Ошибка обращения к n8n (вебхук %s): %s",
            CFG.n8n_webhook_url,
            exc,
        )
        return None

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
    # ушли бы в n8n как «входящее ЛС» и породили бы бесконечный цикл
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

    response = await request_suggestions(payload)
    suggestions = (response or {}).get("suggestions") or []
    suggestions = [
        s.strip() for s in suggestions if isinstance(s, str) and s.strip()
    ][: CFG.max_suggestions]

    if not suggestions:
        await notify_owner(
            f"⚠️ Не удалось получить варианты ответа для {payload['peer_name']}.\n"
            "Проверьте n8n (workflow_ai_responder) и ключ Gemini/Groq."
        )
        return

    # Всегда сохраняем варианты — по ним n8n (или inline-кнопки) командуют отправку
    PENDING[(payload["peer_id"], payload["message_id"])] = {
        "suggestions": suggestions,
        "peer_name": payload["peer_name"],
    }

    if CFG.ai_mode == "userbot":
        await show_native_buttons(payload, suggestions)
    elif CFG.ai_mode == "bot_chat":
        await show_bot_chat_buttons(payload, suggestions)
    else:
        logger.info(
            "Варианты получены (режим bot) — кнопки рисует Telegram-бот через n8n"
        )


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
            f"✏️ Отредактируйте это сообщение — после правки оно уйдёт собеседнику.\n\n{variant}",
        )
        EDIT_CTX[sent.id] = (peer_id, peer_name)
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
    """Правка сообщения в служебном чате -> отправка собеседнику (флоу «Редактировать»)."""
    if message.chat.id != service_chat_id:
        return
    ctx = EDIT_CTX.pop(message.id, None)
    if ctx is None or not message.text:
        return
    peer_id, peer_name = ctx
    await client.send_message(peer_id, message.text)
    logger.info("Отправлено отредактированное сообщение собеседнику id=%s", peer_id)
    await message.reply(f"✅ Отправлено собеседнику ({peer_name}).")

# ---------------------------------------------------------------------------
# Telegram Bot (@myaccounttbot): long-polling и кнопки в личном чате с владельцем
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "🤖 Пульт AI-автоответчика.\n\n"
    "Когда вам пишут в ЛС, сюда приходят варианты ответа с кнопками:\n"
    "• [1] [2] [3] — отправить выбранный вариант собеседнику\n"
    "• ✏️ Ред. — прислать черновик: ответьте на него своим текстом\n"
    "• ⏭ Пропустить — ничего не отправлять\n\n"
    "Команды:\n"
    "/help — эта справка\n"
    "/cancel — отменить текущее редактирование"
)


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

    key = (peer_id, msg_id)
    if key in IN_FLIGHT:
        await bot_api.answer_callback_query(cb_id, "⏳ Отправка уже выполняется…")
        return

    entry = PENDING.get(key) or {}
    items = entry.get("suggestions") or []
    peer_name = entry.get("peer_name") or str(peer_id)

    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    base = message.get("text") or ""

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
            "✏️ Отредактируйте черновик и пришлите текст <b>ответом на это сообщение</b> "
            "(или /cancel для отмены).\n\nЧерновик:\n" + esc_html(variant),
            parse_mode="HTML",
        )
        EDIT_CTX[sent["message_id"]] = (peer_id, peer_name)
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
    """Сообщения владельца в чате с ботом: правка черновика ответом, команды."""
    if (msg.get("from") or {}).get("id") != owner_id:
        return  # бот игнорирует посторонних
    text = (msg.get("text") or "").strip()
    reply_to = msg.get("reply_to_message") or {}
    bot_msg_id = reply_to.get("message_id")

    if text.startswith("/"):
        cmd = text.split()[0].lower()
        if cmd == "/cancel":
            ctx = EDIT_CTX.pop(bot_msg_id, None) if bot_msg_id else None
            if ctx:
                await bot_edit_with_status(owner_id, bot_msg_id, "", f"⏭ Отменено ({ctx[1]})")
                await bot_api.send_message(owner_id, "Отменено ✅")
            else:
                await bot_api.send_message(owner_id, "Сейчас нет активного редактирования для отмены.")
            return
        if cmd in ("/start", "/help"):
            await bot_api.send_message(owner_id, HELP_TEXT)
        return

    if not text or not bot_msg_id:
        return  # простое сообщение без ответа на черновик — игнорируем

    ctx = EDIT_CTX.pop(bot_msg_id, None)
    if ctx is None:
        return
    peer_id, peer_name = ctx
    try:
        await client.send_message(peer_id, text)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить отредактированный текст собеседнику id=%s", peer_id)
        EDIT_CTX[bot_msg_id] = ctx  # вернём контекст, чтобы можно было повторить
        await bot_api.send_message(
            owner_id, "⚠️ Не удалось отправить (проверьте доступ к собеседнику). Ответьте ещё раз."
        )
        return
    logger.info("Отправлен отредактированный текст собеседнику id=%s", peer_id)
    await bot_edit_with_status(
        owner_id, bot_msg_id, "", f"✅ Отправлено для {peer_name}: \"{text}\""
    )


# ---------------------------------------------------------------------------
# HTTP-сервер: Callback-команды от n8n (POST /api/command)
# ---------------------------------------------------------------------------


async def api_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "ai-responder-userbot"})


def _resolve_variant(body: dict[str, Any]) -> Optional[str]:
    """Берёт text из тела команды, либо вариант из PENDING по peer_id+message_id+index."""
    text = (body.get("text") or "").strip()
    if text:
        return text
    entry = PENDING.get((body.get("peer_id"), body.get("message_id"))) or {}
    items = entry.get("suggestions") or []
    idx = body.get("index") or 0
    if idx < len(items):
        return items[idx]
    return None


async def api_command(request: web.Request) -> web.Response:
    if request.headers.get("X-Api-Key", "") != CFG.callback_api_key:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json body"}, status=400)

    command = body.get("command")
    logger.info("Callback-команда от n8n: %s (peer=%s)", command, body.get("peer_id"))

    if command == "send_reply":
        peer_id = body.get("peer_id")
        variant = _resolve_variant(body)
        if not peer_id or not variant:
            return web.json_response(
                {"ok": False, "error": "peer_id и text (или index) обязательны"}, status=400
            )
        await client.send_message(peer_id, variant, reply_to_message_id=body.get("message_id"))
        PENDING.pop((body.get("peer_id"), body.get("message_id")), None)
        return web.json_response({"ok": True, "sent_to": peer_id})

    if command == "edit_reply":
        peer_id = body.get("peer_id")
        variant = _resolve_variant(body)
        if not peer_id or not variant:
            return web.json_response(
                {"ok": False, "error": "peer_id и text (или index) обязательны"}, status=400
            )
        entry = PENDING.get((body.get("peer_id"), body.get("message_id"))) or {}
        peer_name = entry.get("peer_name") or str(peer_id)
        sent = await client.send_message(
            service_chat_id,
            f"✏️ Отредактируйте это сообщение — после правки оно уйдёт собеседнику.\n\n{variant}",
        )
        EDIT_CTX[sent.id] = (peer_id, peer_name)
        PENDING.pop((body.get("peer_id"), body.get("message_id")), None)
        return web.json_response({"ok": True, "editable_message_id": sent.id})

    if command == "skip":
        logger.info("Пропущено (команда от n8n)")
        return web.json_response({"ok": True})

    if command == "status":
        return web.json_response({
            "ok": True,
            "status": "running",
            "ai_mode": CFG.ai_mode,
            "service_chat_id": service_chat_id,
            "n8n_webhook_url": CFG.n8n_webhook_url,
        })

    return web.json_response({"ok": False, "error": f"unknown command: {command}"}, status=400)

# ---------------------------------------------------------------------------
# Pyrogram-клиент и регистрация обработчиков
# ---------------------------------------------------------------------------

client = Client(CFG.session_name, api_id=CFG.api_id, api_hash=CFG.api_hash, workdir=".")


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
        # Используем long polling -> сбрасываем вебхук, если он был зарегистрирован
        # (например, n8n Workflow 2). Если токеном пользуется ещё что-то — будет 409.
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

    app = web.Application()
    app.router.add_get("/health", api_health)
    app.router.add_post("/api/command", api_command)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CFG.callback_host, CFG.callback_port)
    await site.start()
    logger.info(
        "HTTP-сервер (команды от n8n) слушает http://%s:%s/api/command",
        CFG.callback_host, CFG.callback_port,
    )

    try:
        await idle()
    finally:
        if poller_task is not None:
            poller_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller_task
        await runner.cleanup()
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
