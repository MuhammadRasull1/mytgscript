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

Структура кода:
  - userbot.py — инициализация, обработчики, точка входа (этот файл);
  - services/roles_manager.py — роли собеседников (/mom /dad /role /unrole);
  - services/search_service.py — интернет-поиск (/inter /uninter);
  - services/ai_service.py — системные промпты, генерация ИИ, история диалога;
  - services/con_handler.py — команда /con (генератор текстов);
  - services/shared.py — общие объекты/хелперы (реестр для сервисов);
  - bot_api.py — клиент Bot API для long-polling.

Запуск:
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env   # отредактируйте под себя
    python userbot.py      # первый запуск: телефон, код из Telegram, пароль 2FA
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
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

# --- Сервисные модули (роли, поиск, ИИ, /con) ---
from services.ai_service import (
    EditCtx,
    Intent,
    _dialog_history_block,
    _push_dialog,
    detect_direct_send_intent,
    detect_delete_intent,
    generate_direct_send_text,
    generate_suggestions,
    refine_draft,
    rewrite_draft,
)
from services.con_handler import (
    GenCtx,
    _handle_gen_callback,
    _recipient_status_html,
    _refine_inline_keyboard,
    _resolve_contact,
    bot_edit_with_status,
    handle_con_command,
)
from services.roles_manager import (
    _handle_role_command,
    _remove_user_role,
    _role_prompt_suffix,
    _set_user_role,
)
from services.search_service import (
    _internet_prompt_suffix,
    _set_internet_flag,
    _web_search_context,
)
from services.shared import describe_media, esc_html, esc_md, shared

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

# message_id сообщения-черновика (доработки) -> контекст правки
EDIT_CTX: dict[int, EditCtx] = {}
# Ключи (peer_id, message_id), для которых отправка уже идёт (защита от двойного клика)
IN_FLIGHT: set[tuple[int, int]] = set()

# message_id сообщения-результата /con -> контекст генератора
GEN_CTX: dict[int, GenCtx] = {}

# Preview contexts for direct send (preview_msg_id -> {target, text, target_name})
DIRECT_SEND_CTX: dict[int, dict] = {}

# Активные черновики прямой отправки ("current" -> {target, text, msg_id}).
# Пока черновик активен, любое простое сообщение в чате управления
# интерпретируется как пожелание переписать текст черновика.
ACTIVE_DRAFT: dict[Any, dict] = {}
# Sent message contexts for delete button (sent_msg_id -> {recipient_chat_id, recipient_msg_id})
SENT_MSG_CTX: dict[int, dict] = {}

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
# Обработка входящих ЛС
# ---------------------------------------------------------------------------

# Папка для временных файлов медиа (фото/ГС), отправляемых в Gemini
MEDIA_TMP_DIR = os.path.join("data", "tmp")


def _cleanup_temp_file(path: Optional[str]) -> None:
    """Удаляет временный файл медиа (безопасно, при любом исходе)."""
    if path:
        with contextlib.suppress(OSError):
            os.remove(path)


def _media_extension(mime: Optional[str]) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "video/mp4": ".mp4",
    }.get(mime or "", ".bin")


async def _download_media(source: Any) -> tuple[Optional[str], Optional[str]]:
    """Универсальная загрузка медиа (фото/ГС/аудио) в data/tmp.

    Понимает оба режима по типу источника:
    - Pyrogram Message (входящие ЛС): скачивает через message.download(file_name=...);
    - dict сообщения Bot API (режим bot_chat): берёт file_id (photo[-1] / voice /
      audio), запрашивает путь через bot_api.get_file(file_id) и скачивает байты
      по URL в data/tmp.

    Возвращает (путь, mime_type) или (None, None), если медиа нет или скачивание
    не удалось (в этом случае файл чистится сразу и бот НЕ падает).
    """
    is_bot_api = isinstance(source, dict)
    mime: Optional[str] = None
    file_id: Optional[str] = None

    if is_bot_api:
        photos = source.get("photo") or []
        if photos:
            file_id = photos[-1].get("file_id")
            mime = "image/jpeg"
        elif source.get("voice"):
            file_id = source["voice"].get("file_id")
            mime = source["voice"].get("mime_type") or "audio/ogg"
        elif source.get("audio"):
            file_id = source["audio"].get("file_id")
            mime = source["audio"].get("mime_type") or "audio/mpeg"
        else:
            return None, None
        if not file_id:
            logger.info("Получено медиа (%s), нет file_id — пропускаем", mime)
            return None, None
    else:
        if source.photo:
            mime = "image/jpeg"
        elif source.voice:
            mime = getattr(source.voice, "mime_type", None) or "audio/ogg"
        elif source.audio:
            mime = getattr(source.audio, "mime_type", None) or "audio/mpeg"
        else:
            return None, None

    if not mime:
        return None, None

    path: Optional[str] = None
    try:
        os.makedirs(MEDIA_TMP_DIR, exist_ok=True)
        name = f"dl_{uuid.uuid4().hex}{_media_extension(mime)}"
        path = os.path.join(MEDIA_TMP_DIR, name)
        if is_bot_api:
            file_info = await bot_api.get_file(file_id)
            file_path = (file_info or {}).get("file_path")
            if not file_path:
                raise BotApiError("getFile не вернул file_path")
            await bot_api.download_file(file_path, path)
        else:
            downloaded = await source.download(file_name=path)
            if not downloaded:
                logger.info("Получено медиа (%s), скачивание не удалось — пропускаем", mime)
                _cleanup_temp_file(path)
                return None, None
    except Exception:  # noqa: BLE001
        logger.exception("Получено медиа (%s), скачивание упало — пропускаем", mime)
        _cleanup_temp_file(path)
        return None, None
    return path, mime


async def handle_incoming(message: Message) -> None:
    """Глобальный предохранитель: любая ошибка при обработке входящего сообщения
    (текст, фото, ГС, команды) логируется, но бот не падает и не перезапускается."""
    try:
        await _handle_incoming(message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при обработке сообщения/медиа: %s", exc)
        return


async def _handle_incoming(message: Message) -> None:
    logger.info(
        f"Детектор: получено сообщение {message.id} от {message.from_user.id if message.from_user else 'unknown'}, me={message.from_user.is_self if message.from_user else False}"
    )
    # Отсечка: не обрабатываем собственные исходящие сообщения (с других устройств)
    if message.from_user and message.from_user.is_self:
        return
    raw_text = (message.text or message.caption or "").strip()
    lower = raw_text.lower()

    # Правка активного черновика ПЕРВОЙ: простое сообщение (не "напиши/отправь/
    # удали" и не команда) в управляющем чате, пока есть активный черновик —
    # переписываем текст и редактируем СТАРУЮ карточку предпросмотра.
    if (
        message.chat.id == service_chat_id
        and lower
        and not lower.startswith(("напиши", "отправь", "удали", "/"))
    ):
        draft = ACTIVE_DRAFT.get("current") or ACTIVE_DRAFT.get(owner_id)
        if draft is not None:
            media_path, media_mime = await _download_media(message)
            try:
                new_text = await rewrite_draft(
                    draft["text"], raw_text,
                    media_path=media_path, media_mime=media_mime,
                )
            finally:
                _cleanup_temp_file(media_path)
            if new_text:
                draft["text"] = new_text
                buttons = [
                    [{"text": "🚀 Отправить в 1 клик", "callback_data": f"dsend|{draft['target']}|{new_text[:80]}"}],
                    [{"text": "✏️ Редактировать", "callback_data": f"dedit|{draft['target']}|{new_text[:80]}"}],
                    [{"text": "❌ Отмена", "callback_data": f"dcancel|{draft['target']}|{new_text[:80]}"}],
                ]
                body = (
                    f"✅ Применил: «{esc_html(raw_text)}»\n\n"
                    "📝 Предпросмотр сообщения для <b>"
                    f"{esc_html(draft['target'])}</b>:\n\n"
                    f"{esc_html(new_text[:500])}"
                )
                try:
                    await client.edit_message_text(
                        service_chat_id, draft["msg_id"], body,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(
                            {"inline_keyboard": buttons}
                        ),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Не удалось отредактировать карточку предпросмотра")
                    await client.send_message(
                        service_chat_id,
                        "⚠️ Не удалось обновить предпросмотр. Попробуйте ещё раз.",
                    )
            else:
                await client.send_message(
                    service_chat_id,
                    "⚠️ Не удалось переписать текст. Попробуйте ещё раз.",
                )
            return  # Завершаем обработку

    intent = detect_direct_send_intent(raw_text)
    if intent is not None and message.chat.id == service_chat_id:
        media_path, media_mime = await _download_media(message)
        try:
            chat_id, target_name = await _resolve_chat(intent.target)
            if chat_id is None:
                await message.reply(
                    f"❌ Не нашёл контакт/чат с именем «{esc_html(intent.target)}»."
                )
                return
            # СНАЧАЛА генерируем текст ИИ и только ПОСЛЕ этого показываем
            # карточку предпросмотра с уже сгенерированным текстом
            generated_text = await generate_direct_send_text(
                intent.text, media_path=media_path, media_mime=media_mime
            )
            send_text = generated_text or intent.text
            buttons = [
                [{"text": "🚀 Отправить в 1 клик", "callback_data": f"dsend|{intent.target}|{send_text[:80]}"}],
                [{"text": "✏️ Редактировать", "callback_data": f"dedit|{intent.target}|{send_text[:80]}"}],
                [{"text": "❌ Отмена", "callback_data": f"dcancel|{intent.target}|{send_text[:80]}"}],
            ]
            body = (
                "📝 Предпросмотр сообщения для <b>"
                f"{esc_html(target_name)}</b>:\n\n"
                f"{esc_html(send_text[:500])}"
            )
            try:
                sent = await client.send_message(
                    service_chat_id, body, parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        {"inline_keyboard": buttons}
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось отправить предпросмотр")
                return
            preview_msg_id = sent.id
            DIRECT_SEND_CTX[preview_msg_id] = {
                "target": intent.target,
                "text": send_text,
                "target_name": target_name,
                "chat_id": chat_id,
            }
            ACTIVE_DRAFT["current"] = {
                "target": intent.target,
                "text": send_text,
                "msg_id": preview_msg_id,
                "chat_id": chat_id,
            }
        finally:
            _cleanup_temp_file(media_path)
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

    media_path, media_mime = await _download_media(message)

    try:
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
            "media_path": media_path,
            "media_mime": media_mime,
        }
        # Роль собеседника (папа/мама/кастомная) + правило интернет-поиска
        role_suffix = _role_prompt_suffix(peer) + _internet_prompt_suffix(peer)
        web_context = await _web_search_context(peer, payload["text"])
        # История диалога (без текущего сообщения) для коротких реплик
        history = _dialog_history_block(peer.id)
        _push_dialog(peer.id, "peer", payload["text"])
        payload["role_suffix"] = role_suffix
        payload["web_context"] = web_context
        payload["history"] = history
        logger.info(
            "Получено ЛС от %s (%s): %s%s",
            payload["peer_id"],
            payload["peer_name"],
            payload["text"][:80] or "(без текста)",
            f" + медиа ({media_mime})" if media_path else "",
        )

        # Автопилот: для контактов из списка ответ уходит сразу, без кнопок
        if _is_auto_peer(peer):
            await handle_auto_reply(payload)
            return

        suggestions = await generate_suggestions(
            payload["text"],
            payload["peer_name"],
            role_suffix,
            web_context,
            history,
            payload["peer_username"],
            media_path=media_path,
            media_mime=media_mime,
        )

        if not suggestions:
            await notify_owner(
                f"⚠️ Не удалось получить варианты ответа для {payload['peer_name']}.\n"
                "Проверьте GEMINI_API_KEY / GROQ_API_KEY."
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
    finally:
        _cleanup_temp_file(media_path)


def _peer_ref(payload: dict[str, Any]) -> str:
    uname = payload.get("peer_username")
    if uname:
        return f"@{uname}"
    return str(payload.get("peer_id"))


async def handle_auto_reply(payload: dict[str, Any]) -> None:
    """Автопилот: генерируем ответ и сразу отправляем собеседнику без кнопок."""
    ref = _peer_ref(payload)
    variants = await generate_suggestions(
        payload["text"],
        payload["peer_name"],
        payload.get("role_suffix") or "",
        payload.get("web_context") or "",
        payload.get("history") or "",
        payload.get("peer_username") or "",
        media_path=payload.get("media_path"),
        media_mime=payload.get("media_mime"),
    )
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
    _push_dialog(payload["peer_id"], "me", answer)
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
    await _safe_answer(cb, "")
    if not cb.data:
        return

    # 1. Кнопки прямого черновика (dsend/dcancel/dedit/ddelete) — их data
    #    НЕ 4-частная, поэтому разбираем до общего парсинга.
    if cb.data.startswith(("dsend|", "dcancel|", "dedit|", "ddelete|")):
        parts = cb.data.split("|")
        action = parts[0]
        preview_msg_id = cb.message.id if cb.message else None

        draft = ACTIVE_DRAFT.get("current") or ACTIVE_DRAFT.get(owner_id)

        if action == "dsend":
            if draft is None:
                await _safe_answer(cb, "Черновик устарел")
                return
            try:
                await client.send_message(draft["chat_id"], draft["text"])
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Не удалось отправить черновик пользователю chat_id=%s",
                    draft.get("chat_id"),
                )
                await _safe_answer(cb, "⚠️ Не удалось отправить")
                return
            _push_dialog(draft["chat_id"], "me", draft["text"])
            DIRECT_SEND_CTX.pop(preview_msg_id, None)
            ACTIVE_DRAFT.pop("current", None); ACTIVE_DRAFT.pop(owner_id, None)
            await _safe_answer(cb, "Отправлено ✅")
            await _replace_buttons_with_status(
                cb, f"✅ Отправлено пользователю {esc_md(draft['target'])}"
            )
            return

        if action == "dcancel":
            DIRECT_SEND_CTX.pop(preview_msg_id, None)
            ACTIVE_DRAFT.pop("current", None); ACTIVE_DRAFT.pop(owner_id, None)
            await _safe_answer(cb, "Отменено ❌")
            await _replace_buttons_with_status(cb, "❌ Отменено")
            return

        if action == "dedit":
            await _safe_answer(cb, "✏️ Режим правки")
            if draft is None:
                await cb.message.reply(
                    "⏳ Черновик устарел (возможно, перезапуск). Создайте новый."
                )
                return
            sent = await client.send_message(
                service_chat_id,
                "✍️ Напиши в чат, что изменить "
                "(например: «сделай вежливее» или «добавь про встречу»)\n\nЧерновик:\n"
                + draft["text"],
            )
            EDIT_CTX[sent.id] = EditCtx(
                peer_id=0, peer_name=draft["target"], peer_msg_id=0,
                original=draft["text"], draft=draft["text"],
            )
            return

        if action == "ddelete":
            try:
                target_chat_id = int(parts[1])
                target_msg_id = int(parts[2])
            except (ValueError, IndexError):
                await _safe_answer(cb, "Неверные данные")
                return
            try:
                await client.delete_messages(target_chat_id, target_msg_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Не удалось удалить сообщение %s в чате %s",
                    target_msg_id, target_chat_id,
                )
                await _safe_answer(cb, "⚠️ Не удалось удалить")
                return
            DIRECT_SEND_CTX.pop(preview_msg_id, None)
            await _safe_answer(cb, "Удалено 🗑")
            await _replace_buttons_with_status(cb, "🗑 Сообщение удалено")
            return

    # 2. Только ЕСЛИ это обычные варианты ответа автоответчика (4 части):
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
        _push_dialog(peer_id, "me", variant)
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
    _push_dialog(ctx.peer_id, "me", refined)
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
    "/mom @username — пометить как МАМА (ответ строго на узбекском, на «Siz»)\n"
    "/dad @username — пометить как ПАПА (ответ на языке папы, уважительно)\n"
    "/role @username <инструкция> — кастомная роль (например: «Отвечай дерзко, "
    "мы друзья»)\n"
    "/unrole @username — снять роль\n"
    "/inter @username — включить интернет-поиск для контакта\n"
    "/uninter @username — выключить интернет-поиск\n"
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


async def bot_handle_update(update: dict[str, Any]) -> None:
    """Точка входа для обновлений из long-polling бота."""
    if "callback_query" in update:
        await bot_handle_callback(update["callback_query"])
    elif "message" in update:
        await bot_handle_message(update["message"])


async def bot_handle_callback(cb: dict[str, Any]) -> None:
    """Нажатие на inline-кнопку в чате с ботом (режим bot_chat).

    В callback_data только action|peer_id|message_id|index (лимит 64 байта),
    текст варианта и имя собеседника берутся из PENDING. После действия кнопки
    заменяются статусом «✅ Отправлено для <Имя>: "текст"».
    """
    cb_id = cb.get("id")
    await bot_api.answer_callback_query(cb_id, "")
    user_id = (cb.get("from") or {}).get("id")
    if user_id != owner_id:
        return
    data = cb.get("data") or ""

    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    base = message.get("text") or ""

    # 1. Кнопки прямого черновика (dsend/dcancel/dedit/ddelete) — их data
    #    НЕ 4-частная, поэтому разбираем до общего парсинга.
    if data.startswith(("dsend|", "dcancel|", "dedit|", "ddelete|")):
        parts = data.split("|")
        action = parts[0]

        draft = ACTIVE_DRAFT.get("current") or ACTIVE_DRAFT.get(owner_id)

        if action == "dsend":
            if draft is None:
                await bot_api.answer_callback_query(cb_id, "Черновик устарел")
                return
            try:
                await client.send_message(draft["chat_id"], draft["text"])
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Не удалось отправить черновик пользователю chat_id=%s",
                    draft.get("chat_id"),
                )
                await bot_api.answer_callback_query(cb_id, "⚠️ Не удалось отправить")
                return
            _push_dialog(draft["chat_id"], "me", draft["text"])
            DIRECT_SEND_CTX.pop(message_id, None)
            ACTIVE_DRAFT.pop("current", None); ACTIVE_DRAFT.pop(owner_id, None)
            await bot_api.answer_callback_query(cb_id, "Отправлено ✅")
            await bot_edit_with_status(
                chat_id, message_id, base,
                f"✅ Отправлено пользователю {esc_html(draft['target'])}",
            )
            return

        if action == "dcancel":
            DIRECT_SEND_CTX.pop(message_id, None)
            ACTIVE_DRAFT.pop("current", None); ACTIVE_DRAFT.pop(owner_id, None)
            await bot_api.answer_callback_query(cb_id, "Отменено ❌")
            await bot_edit_with_status(chat_id, message_id, base, "❌ Отменено")
            return

        if action == "dedit":
            await bot_api.answer_callback_query(cb_id, "✏️ Режим правки")
            if draft is None:
                await bot_api.send_message(
                    owner_id,
                    "⏳ Черновик устарел (возможно, перезапуск). Создайте новый.",
                )
                return
            sent = await bot_api.send_message(
                owner_id,
                "✍️ Напиши в чат, что изменить "
                "(например: «сделай вежливее» или «добавь про встречу»)\n\nЧерновик:\n"
                + esc_html(draft["text"]),
                parse_mode="HTML",
            )
            EDIT_CTX[sent["message_id"]] = EditCtx(
                peer_id=0, peer_name=draft["target"], peer_msg_id=0,
                original=draft["text"], draft=draft["text"],
            )
            return

        if action == "ddelete":
            try:
                target_chat_id = int(parts[1])
                target_msg_id = int(parts[2])
            except (ValueError, IndexError):
                await bot_api.answer_callback_query(cb_id, "Неверные данные")
                return
            try:
                await client.delete_messages(target_chat_id, target_msg_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Не удалось удалить сообщение %s в чате %s",
                    target_msg_id, target_chat_id,
                )
                await bot_api.answer_callback_query(cb_id, "⚠️ Не удалось удалить")
                return
            DIRECT_SEND_CTX.pop(message_id, None)
            await bot_api.answer_callback_query(cb_id, "Удалено 🗑")
            await bot_edit_with_status(chat_id, message_id, base, "🗑 Сообщение удалено")
            return

    # 2. Только ЕСЛИ это обычные варианты ответа автоответчика (4 части):
    try:
        action, peer_id_str, msg_id_str, idx_str = data.split("|", 3)
        peer_id, msg_id, idx = int(peer_id_str), int(msg_id_str), int(idx_str)
    except (ValueError, AttributeError):
        await bot_api.answer_callback_query(cb_id, "Неизвестная кнопка")
        return

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
        _push_dialog(peer_id, "me", variant)
        PENDING.pop(key, None)
        await bot_api.answer_callback_query(cb_id, "Отправлено ✅")
        await bot_edit_with_status(
            chat_id, message_id, base, f"✅ Отправлено для {peer_name}: \"{variant}\""
        )


async def _resolve_chat(target: str) -> Optional[tuple[int, str]]:
    """Пытается найти чат/группу по @username, числовому ID или названию.

    Для имён: проходит по открытым диалогам через async iterator,
    сравнивает target с dialog.chat.first_name, dialog.chat.last_name
    или dialog.chat.title (без учёта регистра и лишних пробелов).

    Возвращает (chat_id, display_name) или None.
    """
    try:
        if target.startswith("@"):
            username = target[1:].lower()
            try:
                user = await client.get_users(username)
                return user.id, user.username or user.first_name or f"@{username}"
            except Exception:  # noqa: BLE001
                return None, ""
        if target.lstrip("-").isdigit():
            chat = await client.get_chat(int(target))
            return chat.id, chat.title or str(chat.id)
        normalized = target.strip().lower()
        async for dialog in client.get_dialogs():
            chat = getattr(dialog, "chat", None)
            if chat is None:
                continue
            for attr in ("first_name", "last_name", "title"):
                val = getattr(chat, attr, None)
                if val and val.strip().lower() == normalized:
                    return chat.id, val.strip()
        return None, ""
    except Exception:  # noqa: BLE001
        return None, ""


async def _handle_direct_send(target: str, text: str) -> Optional[str]:
    """Отправляет текст напрямую в указанный чат/группу.

    Возвращает имя чата при успехе или None при ошибке.
    """
    chat_id, name = await _resolve_chat(target)
    if chat_id is None:
        return None
    try:
        await client.send_message(chat_id, text)
        return name
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить сообщение в чат %s", target)
        return None


async def _handle_delete_intent(intent: dict) -> None:
    """Обрабатывает команду удаления последних сообщений у собеседника."""
    target = intent.get("target", "")
    count = intent.get("count", 1)
    chat_id, target_name = await _resolve_chat(target)
    if chat_id is None:
        await bot_api.send_message(
            owner_id,
            f"❌ Не нашёл контакт/чат с именем «{esc_html(target)}».",
        )
        return
    try:
        history = await client.get_chat_history(chat_id, limit=count)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось получить историю чата %s", chat_id)
        await bot_api.send_message(
            owner_id,
            f"⚠️ Не удалось получить историю чата с {esc_html(target_name)}.",
        )
        return
    messages_to_delete = [m.id for m in history if m.outgoing]
    if not messages_to_delete:
        await bot_api.send_message(
            owner_id,
            f"ℹ️ Нет входящих сообщений для удаления в чате с {esc_html(target_name)}.",
        )
        return
    try:
        await client.delete_messages(chat_id, messages_to_delete)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось удалить сообщения в чате %s", chat_id)
        await bot_api.send_message(
            owner_id,
            f"⚠️ Не удалось удалить сообщения в чате с {esc_html(target_name)}.",
        )
        return
    await bot_api.send_message(
        owner_id,
        f"🗑 Удалено {len(messages_to_delete)} последни"
        f"{'х' if count == 1 else 'х'} сообщени"
        f"{'е' if count == 1 else 'я' if count < 5 else 'й'} "
        f"в чате с {esc_html(target_name)}.",
    )


async def bot_handle_message(msg: dict[str, Any]) -> None:
    """Глобальный предохранитель: любая ошибка при обработке сообщения бота
    (команда, текст, фото, ГС) логируется, но бот не падает и не перезапускается."""
    try:
        await _bot_handle_message(msg)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при обработке сообщения/медиа: %s", exc)
        return


async def _bot_handle_message(msg: dict[str, Any]) -> None:
    """Сообщения владельца в чате с ботом: команды, контакт/правка для /con, правка черновика."""
    if (msg.get("from") or {}).get("id") != owner_id:
        return  # бот игнорирует посторонних
    text = (msg.get("text") or msg.get("caption") or "").strip()
    reply_to = msg.get("reply_to_message") or {}
    bot_msg_id = reply_to.get("message_id")

    # Правка активного черновика ПЕРВОЙ: простое сообщение (не "напиши/отправь/
    # удали" и не команда), пока есть активный черновик — переписываем текст и
    # редактируем СТАРУЮ карточку предпросмотра, не создавая новую.
    lower = text.lower()
    if text and not lower.startswith(("напиши", "отправь", "удали", "/")):
        draft = ACTIVE_DRAFT.get("current") or ACTIVE_DRAFT.get(owner_id)
        if draft is not None:
            media_path, media_mime = await _download_media(msg)
            try:
                new_text = await rewrite_draft(
                    draft["text"], text,
                    media_path=media_path, media_mime=media_mime,
                )
            finally:
                _cleanup_temp_file(media_path)
            if not new_text:
                await bot_api.send_message(
                    owner_id, "⚠️ Не удалось переписать текст. Попробуйте ещё раз."
                )
                return
            draft["text"] = new_text
            buttons = [
                [{"text": "🚀 Отправить в 1 клик", "callback_data": f"dsend|{draft['target']}|{new_text[:80]}"}],
                [{"text": "✏️ Редактировать", "callback_data": f"dedit|{draft['target']}|{new_text[:80]}"}],
                [{"text": "❌ Отмена", "callback_data": f"dcancel|{draft['target']}|{new_text[:80]}"}],
            ]
            body = (
                f"✅ Применил: «{esc_html(text)}»\n\n"
                "📝 Предпросмотр сообщения для <b>"
                f"{esc_html(draft['target'])}</b>:\n\n"
                f"{esc_html(new_text[:500])}"
            )
            try:
                await bot_api.edit_message_text(
                    owner_id, draft["msg_id"], body, parse_mode="HTML",
                    reply_markup={"inline_keyboard": buttons},
                )
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось отредактировать карточку предпросмотра")
                await bot_api.send_message(
                    owner_id, "⚠️ Не удалось обновить предпросмотр. Попробуйте ещё раз."
                )
            return  # Завершаем обработку

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
        if cmd == "/mom":
            if not arg:
                await bot_api.send_message(owner_id, "Использование: /mom @username или /mom 123456789")
                return
            await _set_user_role(arg.split(None, 1)[0], "mom")
            return
        if cmd == "/dad":
            if not arg:
                await bot_api.send_message(owner_id, "Использование: /dad @username или /dad 123456789")
                return
            await _set_user_role(arg.split(None, 1)[0], "dad")
            return
        if cmd == "/role":
            await _handle_role_command(arg)
            return
        if cmd == "/unrole":
            if not arg:
                await bot_api.send_message(owner_id, "Использование: /unrole @username или /unrole 123456789")
                return
            await _remove_user_role(arg.split(None, 1)[0])
            return
        if cmd == "/inter":
            if not arg:
                await bot_api.send_message(owner_id, "Использование: /inter @username или /inter 123456789")
                return
            await _set_internet_flag(arg.split(None, 1)[0], True)
            return
        if cmd == "/uninter":
            if not arg:
                await bot_api.send_message(owner_id, "Использование: /uninter @username или /uninter 123456789")
                return
            await _set_internet_flag(arg.split(None, 1)[0], False)
            return
        await bot_api.send_message(owner_id, "Неизвестная команда. Наберите /help для списка команд.")
        return

    intent = detect_direct_send_intent(text)
    if intent is not None:
        media_path, media_mime = await _download_media(msg)
        try:
            chat_id, target_name = await _resolve_chat(intent.target)
            if chat_id is None:
                await bot_api.send_message(
                    owner_id,
                    f"❌ Не нашёл контакт/чат с именем «{esc_html(intent.target)}».",
                )
                return
            # СНАЧАЛА генерируем текст ИИ и только ПОСЛЕ этого показываем
            # карточку предпросмотра с уже сгенерированным текстом
            generated_text = await generate_direct_send_text(
                intent.text, media_path=media_path, media_mime=media_mime
            )
            send_text = generated_text or intent.text
            buttons = [
                [{"text": "🚀 Отправить в 1 клик", "callback_data": f"dsend|{intent.target}|{send_text[:80]}"}],
                [{"text": "✏️ Редактировать", "callback_data": f"dedit|{intent.target}|{send_text[:80]}"}],
                [{"text": "❌ Отмена", "callback_data": f"dcancel|{intent.target}|{send_text[:80]}"}],
            ]
            body = (
                "📝 Предпросмотр сообщения для <b>"
                f"{esc_html(target_name)}</b>:\n\n"
                f"{esc_html(send_text[:500])}"
            )
            try:
                sent = await bot_api.send_message(
                    owner_id, body, parse_mode="HTML",
                    reply_markup={"inline_keyboard": buttons},
                )
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось отправить предпросмотр")
                return
            preview_msg_id = sent["message_id"]
            DIRECT_SEND_CTX[preview_msg_id] = {
                "target": intent.target,
                "text": send_text,
                "target_name": target_name,
                "chat_id": chat_id,
            }
            ACTIVE_DRAFT["current"] = {
                "target": intent.target,
                "text": send_text,
                "msg_id": preview_msg_id,
                "chat_id": chat_id,
            }
        finally:
            _cleanup_temp_file(media_path)
        return

    # Delete intent: "удали [N] последнее сообщение у/пользователю <ИМЯ>"
    del_intent = detect_delete_intent(text)
    if del_intent is not None:
        await _handle_delete_intent(del_intent)
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

# Регистрация общего состояния/хелперов для сервисных модулей (services.*)
shared.CFG = CFG
shared.logger = logger
shared.client = client
shared.http_session = http_session
shared.service_chat_id = service_chat_id
shared.owner_id = owner_id
shared.bot_api = bot_api
shared.bot_user_id = bot_user_id
shared.PENDING = PENDING
shared.EDIT_CTX = EDIT_CTX
shared.IN_FLIGHT = IN_FLIGHT
shared.GEN_CTX = GEN_CTX
shared.DIRECT_SEND_CTX = DIRECT_SEND_CTX
shared.SENT_MSG_CTX = SENT_MSG_CTX
shared._normalize_ref = _normalize_ref
shared._is_auto_peer = _is_auto_peer
shared._peer_ref = _peer_ref
shared.notify_owner = notify_owner


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
    shared.http_session = http_session
    # Запускаем фоновый HTTP-сервер для healthcheck Render
    asyncio.create_task(healthcheck_server())
    await client.start()
    service_chat_id = client.me.id if CFG.service_chat.lower() == "me" else int(CFG.service_chat)
    shared.service_chat_id = service_chat_id
    owner_id = CFG.owner_id or client.me.id
    shared.owner_id = owner_id
    logger.info("Вошли как %s (id=%s), владелец id=%s", client.me.first_name, client.me.id, owner_id)

    poller_task: Optional[asyncio.Task] = None
    if CFG.ai_mode == "bot_chat":
        if not CFG.bot_token:
            raise SystemExit("AI_MODE=bot_chat требует BOT_TOKEN в .env (токен бота из BotFather)")
        bot_api = BotApiClient(CFG.bot_token, http_session)
        shared.bot_api = bot_api
        try:
            me = await bot_api.get_me()
        except BotApiError as exc:
            logger.error("Не удалось получить getMe по BOT_TOKEN: %s", exc)
            raise SystemExit("Проверьте BOT_TOKEN в .env (токен бота из BotFather)") from exc
        bot_user_id = me.get("id")
        shared.bot_user_id = bot_user_id
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
