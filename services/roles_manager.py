# -*- coding: utf-8 -*-
"""Роли собеседников (мама/папа/кастомные) из data/user_roles.json.

Команды /mom /dad /role /unrole. Общие объекты (bot_api, owner_id) и хелперы
(_normalize_ref, esc_html) берутся из services.shared.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from services.con_handler import _extract_con_recipient
from services.shared import esc_html, shared

USER_ROLES_FILE = os.path.join("data", "user_roles.json")
USER_ROLES: dict[str, dict[str, str]] = {}


def _load_user_roles() -> None:
    """Загружает роли контактов из JSON: {'@username'|'id': {'role': ..., 'instruction': ...}}."""
    global USER_ROLES
    try:
        with open(USER_ROLES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            USER_ROLES = {
                str(k): v for k, v in data.items() if isinstance(v, dict)
            }
        else:
            USER_ROLES = {}
    except (FileNotFoundError, ValueError, OSError):
        USER_ROLES = {}


def _save_user_roles() -> None:
    os.makedirs(os.path.dirname(USER_ROLES_FILE) or ".", exist_ok=True)
    with open(USER_ROLES_FILE, "w", encoding="utf-8") as fh:
        json.dump(USER_ROLES, fh, ensure_ascii=False, indent=2)


def _peer_role(peer: Any) -> Optional[dict[str, str]]:
    """Возвращает роль собеседника из USER_ROLES или None."""
    uname = getattr(peer, "username", None)
    if uname:
        entry = USER_ROLES.get("@" + str(uname).lower())
        if entry:
            return entry
    return USER_ROLES.get(str(getattr(peer, "id", "")))


def _role_prompt_suffix(peer: Any) -> str:
    """Дополнение к системному промпту по роли собеседника (мама/папа/кастом)."""
    role = _peer_role(peer)
    if not role:
        return ""
    kind = role.get("role")
    if kind == "mom":
        return (
            " ВАЖНО: это твоя МАМА — роль назначена. Ты отвечаешь от лица СЫНА. "
            "ВСЕГДА отвечай СТРОГО на узбекском языке, уважительно, на «Siz», "
            "обращаясь «Ойи/Мама». Не начинай каждое сообщение с обращения — "
            "обращайся редко и естественно."
        )
    if kind == "dad":
        return (
            " ВАЖНО: это твой ПАПА — роль назначена. Ты отвечаешь от лица СЫНА. "
            "Обращайся «Папа/Пап» (или «ада/отта» на узбекском) с сыновьим "
            "уважением, сохраняя язык его сообщения: написал по-русски — отвечай "
            "по-русски, по-узбекски — по-узбекски. Не начинай каждое сообщение с "
            "обращения — обращайся редко и естественно («Да, пап», «Хорошо, сделаю»)."
        )
    if kind == "custom":
        instr = (role.get("instruction") or "").strip()
        if instr:
            return f" Дополнительное правило для этого собеседника: {instr}"
    return ""


async def _set_user_role(ref: str, kind: str, instruction: str = "") -> None:
    """Назначает роль контакту (mom/dad/custom) и сохраняет в JSON."""
    canonical = shared._normalize_ref(ref)
    if not canonical:
        await shared.bot_api.send_message(
            shared.owner_id, "Некорректный контакт. Формат: @username или 123456789"
        )
        return
    if kind == "custom":
        if not instruction:
            await shared.bot_api.send_message(
                shared.owner_id, "Укажите инструкцию: /role @username <инструкция>"
            )
            return
        USER_ROLES[canonical] = {"role": "custom", "instruction": instruction}
    else:
        USER_ROLES[canonical] = {"role": kind}
    _save_user_roles()
    label = {"mom": "МАМА 👩", "dad": "ПАПА 👨", "custom": "кастомная роль 🎭"}[kind]
    await shared.bot_api.send_message(
        shared.owner_id,
        f"Роль установлена для <b>{esc_html(canonical)}</b>: {label}."
        + (f"\nИнструкция: {esc_html(instruction)}" if instruction else ""),
        parse_mode="HTML",
    )


async def _remove_user_role(ref: str) -> None:
    canonical = shared._normalize_ref(ref)
    if not canonical:
        await shared.bot_api.send_message(
            shared.owner_id, "Некорректный контакт. Формат: /unrole @username или /unrole 123456789"
        )
        return
    if canonical not in USER_ROLES:
        await shared.bot_api.send_message(shared.owner_id, f"{canonical} не имеет роли.")
        return
    USER_ROLES.pop(canonical, None)
    _save_user_roles()
    await shared.bot_api.send_message(
        shared.owner_id, f"Роль снята для <b>{esc_html(canonical)}</b>.", parse_mode="HTML"
    )


async def _handle_role_command(arg: str) -> None:
    """Команда /role @username <инструкция>: назначает кастомную роль."""
    target, _, instruction = _extract_con_recipient(arg)
    if target is None or not instruction:
        await shared.bot_api.send_message(
            shared.owner_id,
            "Формат: /role @username <инструкция>\n"
            "Например: /role @friend_nick Отвечай дерзко, на сленге, мы друзья",
        )
        return
    ref = str(target) if isinstance(target, int) else target
    await _set_user_role(ref, "custom", instruction)


_load_user_roles()
