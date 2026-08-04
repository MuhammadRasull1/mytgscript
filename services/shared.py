# -*- coding: utf-8 -*-
"""Разделяемое состояние и текстовые хелперы для сервисных модулей и userbot.py.

Сервисы не импортируют userbot.py напрямую (иначе при запуске `python userbot.py`
модуль загрузился бы второй раз как `userbot`, а не `__main__`). Вместо этого
userbot.py регистрирует общие объекты (client, bot_api, CFG, контексты и т.п.)
в `shared` — сервисы читают их оттуда в момент вызова.
"""
from __future__ import annotations

import html
from typing import Any, Optional

from pyrogram.types import Message


class Shared:
    """Реестр общих объектов/функций; заполняется из userbot.py."""

    # Резолвятся в main()/при инициализации userbot.py
    bot_api: Any = None
    client: Any = None
    owner_id: Optional[int] = None
    service_chat_id: Optional[int] = None
    http_session: Any = None
    bot_user_id: Optional[int] = None
    CFG: Any = None
    logger: Any = None

    # Функции, определяемые в userbot.py (подставляются после определения)
    _normalize_ref = None
    _is_auto_peer = None
    _peer_ref = None
    notify_owner = None

    # Контексты (одни и те же объекты, что и глобалы userbot.py)
    PENDING: dict = {}
    EDIT_CTX: dict = {}
    IN_FLIGHT: set = set()
    GEN_CTX: dict = {}
    DIRECT_SEND_CTX: dict = {}
    SENT_MSG_CTX: dict = {}


shared = Shared()


def esc_html(text: str) -> str:
    """Экранирует спецсимволы HTML (для сообщений чата с ботом, parse_mode=HTML)."""
    return html.escape(str(text), quote=True)


def esc_md(text: str) -> str:
    """Экранирует спецсимволы Markdown (чтобы текст собеседника не ломал разметку)."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def describe_media(message: Message) -> str:
    return str(message.media.value) if message.media else "text"
