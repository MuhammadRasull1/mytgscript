# -*- coding: utf-8 -*-
"""Команда /con: генерация текстов, выбор варианта, указание получателя,
отправка и доработка черновика (кнопки в чате с ботом).

Общие объекты (bot_api, owner_id, client, GEN_CTX) берутся из services.shared.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from services.ai_service import generate_content
from services.shared import esc_html, shared


@dataclass
class GenCtx:
    """Контекст команды /con (генератор произвольных текстов)."""

    instruction: str            # исходный запрос пользователя
    variants: list[str]         # сгенерированные варианты
    selected: str = ""          # выбранный вариант
    target: Any = None          # контакт для отправки (int id или str username)
    target_name: str = ""       # имя/метка контакта


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
            user = await shared.client.get_users(username)
            display = user.username or user.first_name or f"@{username}"
            return user.id, display
        except Exception:  # noqa: BLE001
            return username, f"@{username}"
    if ref.lstrip("-").isdigit():
        uid = int(ref)
        try:
            user = await shared.client.get_users(uid)
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
        await shared.bot_api.send_message(
            shared.owner_id,
            "Укажите запрос. Примеры:\n"
            "/con @ky_747 поздравь Юсуф ака с ДР на узбекском\n"
            "/con Придумай причину, почему я заболел и не приду сегодня",
        )
        return
    variants = await generate_content(instruction)
    if not variants:
        await shared.bot_api.send_message(
            shared.owner_id,
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
        sent = await shared.bot_api.send_message(
            shared.owner_id, body, parse_mode="HTML", reply_markup={"inline_keyboard": buttons}
        )
    except Exception:  # noqa: BLE001
        shared.logger.exception("Не удалось отправить результат /con")
        return
    shared.GEN_CTX[sent["message_id"]] = GenCtx(
        instruction=instruction,
        variants=variants,
        target=target,
        target_name=target_name,
    )


async def bot_edit_with_status(
    chat_id: int, message_id: int, base: str, status: str
) -> None:
    """Редактирует сообщение бота: убирает inline-кнопки и дописывает статус."""
    new_text = f"{base}\n\n{esc_html(status)}" if base else esc_html(status)
    try:
        await shared.bot_api.edit_message_text(
            chat_id,
            message_id,
            new_text,
            parse_mode="HTML",
            reply_markup={"inline_keyboard": []},
        )
    except Exception:  # noqa: BLE001
        shared.logger.warning("Не удалось отредактировать сообщение бота", exc_info=True)


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
            await shared.bot_api.answer_callback_query(cb_id, "Контекст устарел (возможно, перезапуск)")
            return
        gen.selected = gen.variants[idx]
        await shared.bot_api.answer_callback_query(cb_id, f"Выбран вариант {idx + 1}")
        await shared.bot_api.edit_message_text(
            shared.owner_id,
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
            await shared.bot_api.answer_callback_query(cb_id, "Сначала выберите вариант")
            return
        if gen.target is None:
            # Получатель не указан — запрашиваем отдельным чётким сообщением
            ask = await shared.bot_api.send_message(
                shared.owner_id,
                "🎯 Получатель не указан.\n"
                "Ответьте <b>@username</b> или цифровым ID, кому отправить текст "
                "(например: <b>@ky_747</b> или <b>123456789</b>).",
                parse_mode="HTML",
            )
            shared.GEN_CTX[ask["message_id"]] = gen
            await shared.bot_api.answer_callback_query(cb_id, "Укажите получателя")
            return
        try:
            await shared.client.send_message(gen.target, gen.selected)
        except Exception:  # noqa: BLE001
            shared.logger.exception("Не удалось отправить сгенерированный текст")
            await shared.bot_api.answer_callback_query(cb_id, "⚠️ Не удалось отправить (проверьте контакт)")
            return
        label = gen.target_name or str(gen.target)
        shared.GEN_CTX.pop(message_id, None)
        await shared.bot_api.answer_callback_query(cb_id, "Отправлено ✅")
        await bot_edit_with_status(
            chat_id, message_id, base, f"✅ Отправлено для {label}: \"{gen.selected}\""
        )
        return
    if action == "redit":
        if not gen.selected:
            await shared.bot_api.answer_callback_query(cb_id, "Сначала выберите вариант")
            return
        await shared.bot_api.edit_message_text(
            shared.owner_id,
            message_id,
            f"Текущий текст:\n\n{esc_html(gen.selected)}\n\n"
            f"{_recipient_status_html(gen.target, gen.target_name)}\n\n"
            "Пришлите правку ответом на это сообщение (или укажите получателя).",
            parse_mode="HTML",
            reply_markup={"inline_keyboard": []},
        )
        await shared.bot_api.answer_callback_query(cb_id, "Пришлите правку ответом")
        return
    if action in ("rcancel", "gencancel"):
        shared.GEN_CTX.pop(message_id, None)
        await bot_edit_with_status(chat_id, message_id, base, "❌ Отменено")
        await shared.bot_api.answer_callback_query(cb_id, "Отменено ❌")
        return
