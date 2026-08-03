# 📦 Форматы JSON

## 1. Юзербот → n8n (`POST {N8N_WEBHOOK_URL}`)

Тело запроса (генерирует `userbot.py` при входящем ЛС):

```json
{
  "event": "incoming_message",
  "peer_id": 512345678,
  "peer_name": "Иван Петров",
  "peer_username": "ivan_petrov",
  "message_id": 42,
  "text": "Привет! Как дела?",
  "chat_type": "private",
  "timestamp": 1753977600,
  "is_forwarded": false,
  "media_type": "text"
}
```

| Поле | Тип | Описание |
|---|---|---|
| `peer_id` | int | id собеседника (кому отправлять ответ) |
| `peer_name` | string | имя/название собеседника |
| `peer_username` | string \| null | юзернейм, если есть |
| `message_id` | int | id исходного сообщения (для `reply_to_message_id`) |
| `text` | string | текст (для вложений — caption; иначе пусто) |
| `media_type` | string | `text`, `photo`, `video`, `voice` и т.п. |
| `timestamp` | int | Unix-время |

Тест:

```bash
curl -X POST http://localhost:5678/webhook/telegram-in \
  -H "Content-Type: application/json" \
  -d '{"event":"incoming_message","peer_id":123,"peer_name":"Тест","message_id":1,"text":"Привет!"}'
```

## 2. n8n → юзербот (ответ на вебхук)

Успех:

```json
{
  "ok": true,
  "error": null,
  "suggestions": [
    "Привет! Всё отлично, а у тебя?",
    "Здравствуй! Рад тебя видеть 🙂",
    "Привет! Что нового?"
  ],
  "provider": "gemini",
  "fallback_used": false
}
```

> `provider` — какая модель ответила: `gemini` или `groq`;
> `fallback_used` — `true`, если сработало резервирование (Gemini дал ошибку
> 429/таймаут, ответ сгенерировала Groq). Юзербот эти поля игнорирует — они
> для диагностики.

Ошибка:

```json
{ "ok": false, "error": "no suggestions", "suggestions": [] }
```

## 3. n8n → юзербот: Callback-команда (`POST /api/command`)

Заголовки: `Content-Type: application/json`, `X-Api-Key: <CALLBACK_API_KEY>`.

### `send_reply` — отправить утверждённый ответ собеседнику

```json
{
  "command": "send_reply",
  "peer_id": 512345678,
  "message_id": 42,
  "index": 1
}
```

- `index` — номер варианта, сохранённого юзерботом (из ответа вебхука).
- Вместо `index` можно передать готовый `text` — тогда юзербот отправит его
  как есть (текст в `callback_data` кнопок Telegram ограничен 64 байтами,
  поэтому в Workflow 2 используется `index`).

Ответ: `{"ok": true, "sent_to": 512345678}`.

### `edit_reply` — создать сообщение для правки в служебном чате

```json
{ "command": "edit_reply", "peer_id": 512345678, "message_id": 42, "index": 0 }
```

Юзербот отправит вариант в служебный чат; после вашей правки сообщения оно
уйдёт собеседнику. Ответ: `{"ok": true, "editable_message_id": 77}`.

### `skip` — пометить пропущенным

```json
{ "command": "skip" }
```

Ответ: `{"ok": true}`.

### `status` — диагностика

```json
{ "command": "status" }
```

Ответ: `{"ok": true, "status": "running", "ai_mode": "bot", "service_chat_id": -100..., "n8n_webhook_url": "..."}`.

Тест:

```bash
curl -X POST http://localhost:8123/api/command \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: change-me-strong-secret" \
  -d '{"command":"status"}'
```

## 4. Telegram-бот: кнопки (Bot API `sendMessage`)

Формат `reply_markup.inline_keyboard`, который n8n отправляет в служебный чат:

```json
{
  "chat_id": -1001234567890,
  "text": "💬 Новое сообщение от Иван (id 512345678):\n«Привет!»\n\n🤖 Варианты ответа:\n1. Привет! ...",
  "reply_markup": {
    "inline_keyboard": [
      [
        { "text": "✉️ Отправить: Привет! Всё отлично", "callback_data": "{\"a\":\"send\",\"p\":512345678,\"m\":42,\"i\":0}" },
        { "text": "✏️ Ред.", "callback_data": "{\"a\":\"edit\",\"p\":512345678,\"m\":42,\"i\":0}" }
      ],
      [
        { "text": "⏭ Пропустить", "callback_data": "{\"a\":\"skip\",\"p\":512345678,\"m\":42}" }
      ]
    ]
  }
}
```

> ⚠️ **Важно:** `callback_data` ограничен **64 байтами**. Поэтому в нём передаются
> только короткие поля `a` (action), `p` (peer_id), `m` (message_id), `i` (index),
> а сам текст варианта юзербот достаёт из памяти (куда положил его из ответа вебхука).
> Подробнее о формате `callback_query` — в `N8N_WORKFLOW.md`.

## 5. Режим userbot: кнопки [1] [2] [3] в «Избранном»

Когда `AI_MODE=userbot` (кнопки рисует сам юзербот в «Избранном»), формат
`callback_data` у него свой (проще, чем у бота):

```
send|peer_id|message_id|index     # кнопки [1] [2] [3]
edit|peer_id|message_id|0        # [✏️ Ред.]
skip|peer_id|message_id|0        # [⏭ Пропустить]
```

Текст варианта юзербот берёт из памяти `PENDING[(peer_id, message_id)]`.
После нажатия сообщение редактируется: кнопки убираются, в конец дописывается
статус `✅ Отправлено для <Имя>: "<текст>"` (для skip — `⏭ Пропущено`,
для edit — `✏️ Создано сообщение для правки`).

## 6. Режим bot_chat: кнопки в личном чате с ботом

`AI_MODE=bot_chat` (по умолчанию): сообщение с вариантами и inline-кнопками
отправляет сам юзербот через Bot API (`POST sendMessage` с `chat_id =
MY_TELEGRAM_ID`), поэтому формат `callback_data` у кнопок тот же, что и в
разделе 5 (`send|peer_id|message_id|index` и т.д.).

Нажатие ловит встроенный long-polling (`bot_api.py`): юзербот отвечает
`answerCallbackQuery` (тост), отправляет выбранный текст собеседнику от имени
личного аккаунта и правит сообщение бота (`editMessageText` с пустой
клавиатурой `{"inline_keyboard": []}` + статус `✅ Отправлено для …`).
`sendMessage` в чат с ботом:

```json
{
  "chat_id": 512345678,
  "text": "💬 <b>Иван</b> (512345678) · text\n<i>«Привет!»</i>\n\n🤖 Варианты ответа:\n<b>1.</b> Привет! Всё отлично",
  "parse_mode": "HTML",
  "reply_markup": {
    "inline_keyboard": [
      [{ "text": "[1]", "callback_data": "send|512345678|42|0" },
       { "text": "[2]", "callback_data": "send|512345678|42|1" },
       { "text": "[3]", "callback_data": "send|512345678|42|2" }],
      [{ "text": "✏️ Ред.", "callback_data": "edit|512345678|42|0" },
       { "text": "⏭ Пропустить", "callback_data": "skip|512345678|42|0" }]
    ]
  }
}
```

> ⚠️ В этом режиме n8n (Workflow 2) не должен держать Telegram Trigger на том
> же токене бота — будет `409 Conflict`. Деактивируйте Workflow 2.
>
> ✏️ Ред. работает иначе, чем в разделе 5: бот присылает черновик, а вы
> присылаете исправленный текст **ответом на него**; юзербот отправит его
> собеседнику и пометит сообщение-черновик как `✅ Отправлено …`.
