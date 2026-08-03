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
import logging
from typing import Any, Awaitable, Callable, Optional

import aiohttp

logger = logging.getLogger("bot_api")

BOT_API_URL = "https://api.telegram.org/bot{token}/{method}"

# Обновления, которые мы умеем обрабатывать: нажатия кнопок и сообщения владельца
ALLOWED_UPDATES = ["callback_query", "message"]


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

    async def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> list:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", **params, allowed_updates=ALLOWED_UPDATES)


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
