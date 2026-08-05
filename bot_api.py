#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Минимальный клиент Telegram Bot API (HTTP + long polling) на aiohttp.

Нужен для режима AI_MODE=bot_chat: сообщение с вариантами ответа и настоящими
inline-кнопками [1] [2] [3] отправляется через Bot API в личный чат владельца
с ботом (@myaccounttbot), а нажатия кнопок ловятся здесь же через getUpdates
(long polling) — публичный URL и вебхук для этого не нужны.
"""

from __future__ import annotations

import asyncio

# Совместимость с Python 3.14: pyrogram (в т.ч. через services.shared) при импорте
# вызывает asyncio.get_event_loop(), который без активного цикла кидает RuntimeError.
# Инициализируем цикл ДО импорта pyrogram и services.
try:
    asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import contextlib
import logging
import os

from aiohttp import web
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from services.shared import shared

logger = logging.getLogger("bot_api")

BOT_API_URL = "https://api.telegram.org/bot{token}/{method}"

# Обновления, которые мы умеем обрабатывать: нажатия кнопок и сообщения владельца
ALLOWED_UPDATES = ["callback_query", "message"]

# Флаг для предотвращения повторного запуска веб-сервера в одном процессе
_healthcheck_running = False

# Папка для временных файлов медиа (фото/ГС), передаваемых в ai_service
MEDIA_TMP_DIR = os.path.join("data", "tmp")

# Реестр ещё не удалённых временных медиафайлов: crash-guard'ы чистят их в finally
_ACTIVE_TEMP_FILES: set[str] = set()

# Реальный обработчик сообщений регистрируется из userbot.py
# (register_message_processor). Сигнатура:
#   async (message: dict, raw_text: str, media_path: str|None, media_mime: str|None) -> None
_message_processor: Optional[Callable[..., Awaitable[None]]] = None


def register_message_processor(fn: Callable[..., Awaitable[None]]) -> None:
    """Регистрирует фактическую логику обработки сообщений (из userbot.py)."""
    global _message_processor
    _message_processor = fn


def cleanup_temp_file(path: Optional[str]) -> None:
    """Безопасно удаляет временный файл медиа из MEDIA_TMP_DIR (при любом исходе)."""
    if path:
        _ACTIVE_TEMP_FILES.discard(path)
        with contextlib.suppress(OSError):
            os.remove(path)


def _media_extension(mime: Optional[str]) -> str:
    return {
        "image/jpeg": ".jpg",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
    }.get(mime or "", ".bin")


async def _notify_owner(text: str) -> None:
    """Отправляет сообщение владельцу: через shared.notify_owner или Bot API."""
    fn = getattr(shared, "notify_owner", None)
    if fn is not None:
        with contextlib.suppress(Exception):
            await fn(text)
        return
    owner_id = getattr(shared, "owner_id", None)
    client = getattr(shared, "bot_api", None)
    if owner_id and client is not None:
        with contextlib.suppress(Exception):
            await client.send_message(owner_id, text)


class BotApiError(RuntimeError):
    """Ошибка Telegram Bot API (сеть, статус, поле ok:false)."""


class BotApiClient:
    """Тонкая обёртка над Bot API: вызовы методов + long polling."""

    def __init__(self, token: str, http_session: aiohttp.ClientSession) -> None:
        self._token = token
        self._session = http_session

    async def call(self, method: str, timeout: int = 70, **params: Any) -> Any:
        """Вызывает метод Bot API (POST, JSON). Возвращает поле result.

        timeout должен быть заметно больше таймаута long polling в getUpdates
        (25 c), иначе aiohttp рвёт соединение раньше, чем Telegram ответит.
        """
        url = BOT_API_URL.format(token=self._token, method=method)
        try:
            async with self._session.post(
                url,
                json=params or None,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise BotApiError(f"{method}: сеть недоступна ({exc})") from exc
        if not payload.get("ok"):
            raise BotApiError(f"{method}: {payload.get('description') or payload}")
        return payload.get("result")

    # --- Часто используемые методы Bot API ---

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def delete_webhook(self, drop_pending_updates: bool = False) -> dict:
        """Сбрасывает вебхук — мы используем long polling (getUpdates)."""
        return await self.call("deleteWebhook", drop_pending_updates=drop_pending_updates)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[dict] = None,
        parse_mode: Optional[str] = None,
    ) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        if parse_mode is not None:
            params["parse_mode"] = parse_mode
        return await self.call("sendMessage", **params)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[dict] = None,
        parse_mode: Optional[str] = None,
    ) -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        if parse_mode is not None:
            params["parse_mode"] = parse_mode
        return await self.call("editMessageText", **params)

    async def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> dict:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            params["text"] = text
        return await self.call("answerCallbackQuery", **params)

    async def get_file(self, file_id: str) -> dict:
        """Возвращает file_path для file_id (для скачивания медиа)."""
        return await self.call("getFile", file_id=file_id)

    async def download_file(self, file_path: str, dest: str) -> None:
        """Скачивает файл по пути из getFile в локальный файл dest."""
        url = f"https://api.telegram.org/file/bot{self._token}/{file_path}"
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status >= 400:
                    raise BotApiError(f"file download: HTTP {resp.status}")
                with open(dest, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(65536):
                        fh.write(chunk)
        except aiohttp.ClientError as exc:
            raise BotApiError(f"file download: сеть недоступна ({exc})") from exc

    async def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> list:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", **params, allowed_updates=ALLOWED_UPDATES)

    async def download_media(self, message: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        """Скачивает фото/ГС/аудио из сообщения Bot API в MEDIA_TMP_DIR.

        - photo -> photo.jpg  (media_mime="image/jpeg")
        - voice -> voice.ogg  (media_mime="audio/ogg")
        - audio -> audio.<ext>

        Возвращает (путь, mime_type) или (None, None), если медиа нет или
        скачивание не удалось (в этом случае файл чистится сразу, бот НЕ падает).
        """
        photos = message.get("photo") or []
        if photos:
            file_id = photos[-1].get("file_id")
            mime = "image/jpeg"
            name = "photo.jpg"
        elif message.get("voice"):
            file_id = message["voice"].get("file_id")
            mime = message["voice"].get("mime_type") or "audio/ogg"
            name = "voice.ogg"
        elif message.get("audio"):
            file_id = message["audio"].get("file_id")
            mime = message["audio"].get("mime_type") or "audio/mpeg"
            name = f"audio{_media_extension(mime)}"
        else:
            return None, None
        if not file_id:
            logger.info("Получено медиа (%s) без file_id — пропускаем", mime)
            return None, None

        path: Optional[str] = None
        try:
            os.makedirs(MEDIA_TMP_DIR, exist_ok=True)
            path = os.path.join(MEDIA_TMP_DIR, name)
            cleanup_temp_file(path)  # страховка от «хвоста» прошлого запуска
            file_info = await self.get_file(file_id)
            file_path = (file_info or {}).get("file_path")
            if not file_path:
                raise BotApiError("getFile не вернул file_path")
            await self.download_file(file_path, path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Скачивание медиа (%s) не удалось (%s) — пропускаем", mime, exc)
            cleanup_temp_file(path)
            return None, None
        _ACTIVE_TEMP_FILES.add(path)
        return path, mime


async def bot_handle_message(message: dict[str, Any]) -> None:
    """Главный предохранитель (Crash Guard) для входящих сообщений бота.

    Безопасно достаёт текст (текст или подпись к медиа), скачивает фото/ГС/аудио
    в MEDIA_TMP_DIR и передаёт media_path/media_mime в ai_service через
    зарегистрированный обработчик (register_message_processor). Любая ошибка
    логируется и отправляется владельцу («⚠️ Ошибка обработки медиа: …»),
    процесс bot_api.py НЕ падает и НЕ перезапускается. В finally ВСЕГДА удаляется
    временный файл из MEDIA_TMP_DIR.
    """
    media_path: Optional[str] = None
    try:
        owner_id = getattr(shared, "owner_id", None)
        sender_id = (message.get("from") or {}).get("id")
        if owner_id is not None and sender_id != owner_id:
            return  # чужие сообщения не обрабатываем и не скачиваем их медиа

        # 1) Безопасное извлечение текста: текст или подпись (caption) к медиа
        raw_text = (message.get("text") or message.get("caption") or "").strip()
        if not raw_text and not (
            message.get("photo") or message.get("voice") or message.get("audio")
        ):
            return  # нет ни текста, ни медиа — обрабатывать нечего

        # 2) Скачивание фото/ГС/аудио в MEDIA_TMP_DIR (photo.jpg / voice.ogg)
        client = getattr(shared, "bot_api", None)
        media_mime: Optional[str] = None
        if client is not None:
            media_path, media_mime = await client.download_media(message)

        # 3) Текстовые проверки/команды и передача media_path/media_mime в ai_service
        processor = _message_processor
        if processor is not None:
            await processor(message, raw_text, media_path, media_mime)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка обработки медиа: %s", exc)
        with contextlib.suppress(Exception):
            await _notify_owner(f"⚠️ Ошибка обработки медиа: {exc}")
    finally:
        # ВСЕГДА удаляем временный файл из MEDIA_TMP_DIR
        cleanup_temp_file(media_path)


class BotApiPoller:
    """Long-polling цикл getUpdates с отслеживанием offset и паузой при сбоях."""

    def __init__(
        self,
        client: BotApiClient,
        on_update: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._client = client
        self._on_update = on_update

    async def run(self) -> None:
        offset = 0
        backoff = 1.0
        while True:
            try:
                updates = await self._client.get_updates(offset=offset)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except BotApiError as exc:
                logger.error(
                    "getUpdates: %s. Если этим токеном пользуется ещё что-то "
                    "(например, n8n Workflow 2 «Telegram Trigger» или другой бот) — "
                    "возникнет конфликт 409; деактивируйте лишнего получателя.",
                    exc,
                )
                await asyncio.sleep(10)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("getUpdates: сеть/сервер (%s), пауза %.0f с", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            for update in updates:
                # offset двигаем ДО обработки: «битое» обновление не должно
                # зацикливать опрос навсегда
                offset = update.get("update_id", 0) + 1
                try:
                    await self._on_update(update)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Ошибка обработки обновления %s", update.get("update_id")
                    )


async def healthcheck_server() -> None:
    """HTTP-сервер заглушка для Render (healthcheck на aiohttp).

    Функция никогда не завершается сама по себе: даже если сервер уже запущен
    (флаг _healthcheck_running) или порт занят (OSError), мы всё равно уходим в
    await asyncio.Event().wait(). Иначе корутина завершилась бы раньше времени
    и сломала бы lifecycle userbot.main() на Render.
    """
    global _healthcheck_running

    port = int(os.getenv("PORT", 8123))

    if _healthcheck_running:
        logger.info("Healthcheck сервер уже запущен, пропускаем.")
    else:
        app = web.Application()

        async def handler(request):  # noqa: ARG001
            return web.Response(text="OK", status=200)

        app.router.add_get("/", handler)
        app.router.add_get("/health", handler)
        app.router.add_get("/healthcheck", handler)

        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, host="0.0.0.0", port=port)
            await site.start()
            _healthcheck_running = True
            logger.info("Healthcheck server successfully started on port %s", port)
        except OSError as exc:
            logger.warning("Порт %s уже занят (%s). Сервер заглушка уже работает.", port, exc)

    # Держим сервер запущенным в фоновом режиме — никогда не выходим
    await asyncio.Event().wait()


if __name__ == "__main__":
    # При запуске bot_api.py напрямую, запускаем userbot.main()
    # Это нужно для Render, который запускает bot_api.py как основной скрипт.
    import userbot

    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(userbot.main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")    