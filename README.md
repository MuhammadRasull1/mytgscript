# 🤖 AI-автоответчик для личного Telegram

Система, которая ловит входящие ЛС вашего аккаунта, генерирует **AI-варианты ответа**
(Gemini/Groq через n8n) и показывает их с кнопками утверждения в служебном чате.

> 🔒 **Безопасность:** ответы **никогда не отправляются автоматически** — только после
> вашего нажатия на кнопку **«Отправить»**.

## 🏗 Архитектура

```
Собеседник ──ЛС──▶ Telegram (ваш аккаунт)
                        │  Pyrogram (userbot.py)
                        ▼
              POST /webhook/telegram-in
                        │
                        ▼
        ┌─────────────────────────── n8n ───────────────────────────┐
        │  Workflow 1: Webhook → Gemini/Groq → варианты ответа       │
        └────────────────────────────────────────────────────────────┘
                        │
        ответ вебхуку {ok, suggestions}
                        ▼
                userbot сохраняет варианты в памяти (PENDING)
                        │  Bot API (BOT_TOKEN) — sendMessage + inline-кнопки
                        ▼
        📱 Личный чат с ботом @myaccounttbot:
             [1] [2] [3] [✏️ Ред.] [⏭ Пропустить]
                        │
                        │  Вы жмёте кнопку
                        ▼
        userbot (long polling getUpdates) перехватывает нажатие
                        │
                        ▼
        userbot отправляет утверждённый текст собеседнику ✅
        (сообщение в чате с ботом редактируется: «✅ Отправлено…»)
```

## 📁 Структура проекта

```
telegram-ai-responder/
├── userbot.py                      # Pyrogram-клиент: ловит ЛС, шлёт вебхук, кнопки в чате с ботом
├── bot_api.py                      # Клиент Telegram Bot API (sendMessage + long polling getUpdates)
├── requirements.txt                # Зависимости Python
├── .env.example                    # Шаблон настроек (скопировать в .env)
├── .gitignore
├── n8n/
│   ├── workflow_ai_responder.json      # Workflow 1: вебхук → AI → варианты + кнопки
│   └── workflow_send_callbacks.json    # Workflow 2: нажатия кнопок → команды юзерботу
└── docs/
    ├── SETUP.md                    # Пошаговая настройка «с нуля»
    ├── JSON_FORMAT.md              # Форматы JSON и curl-тесты
    └── N8N_WORKFLOW.md             # Структура workflow-ов и переменные n8n
```

## 🚀 Быстрый старт

1. **Установите юзербота:**
   ```bash
   cd ~/telegram-ai-responder
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   python userbot.py        # первый запуск: телефон, код, пароль 2FA
   ```

2. **Получите ключи:** Gemini — [aistudio.google.com](https://aistudio.google.com/)
   (бесплатно), либо Groq — [console.groq.com](https://console.groq.com/) (бесплатно).

3. **Создайте бота-пульт** через [@BotFather](https://t.me/BotFather) → токен
   (например, **@myaccounttbot**). Напишите боту `/start` — варианты ответа
   будут приходить в этот личный чат. В `.env` укажите `BOT_TOKEN` и
   `MY_TELEGRAM_ID` (ваш user id, узнаётся у @userinfobot).

4. **Запустите n8n** и импортируйте два workflow из папки `n8n/`.

5. **Настройте переменные n8n** (см. `docs/N8N_WORKFLOW.md`) и активируйте workflow.

Полная инструкция — в [`docs/SETUP.md`](docs/SETUP.md).

## 📡 Как это работает

1. Собеседник пишет вам в ЛС → юзербот шлёт вебхук в n8n.
2. n8n вызывает Gemini/Groq и возвращает юзерботу до 3 вариантов ответа;
   юзербот через Bot API присылает их **в ваш личный чат с ботом
   @myaccounttbot** с настоящими inline-кнопками **[1] [2] [3]**, [✏️ Ред.]
   и [⏭ Пропустить].
3. Вы жмёте **[1]/[2]/[3]** → выбранный текст уходит собеседнику (как ответ
   на его сообщение), а сообщение в чате с ботом редактируется — кнопки
   убираются, остаётся статус: `✅ Отправлено для <Имя>: "<текст>"`.
4. **✏️ Ред.** → бот присылает черновик; вы отвечаете на него своим текстом,
   и он уходит собеседнику.
5. **⏭ Пропустить** → ничего не отправляется, диалог помечается пропущенным.

## 🔐 Безопасность

- Отправка только по явному действию (кнопка) — автопересылки нет.
- Callback-сервер юзербота защищён заголовком `X-Api-Key`.
- Workflow 2 игнорирует нажатия, если они не от владельца (`OWNER_ID`).
- Сессия `*.session` и `.env` в `.gitignore` — не коммитьте их.
- В режиме `bot_chat` кнопками занимается сам юзербот (long polling) —
  публичный URL для бота не нужен. Публичный URL/ngrok требуется только
  в старом режиме `bot` (кнопки через n8n) — см. `docs/SETUP.md`.

## 🧪 Тестирование без Telegram

```bash
# Проверка вебхука n8n (Workflow 1)
curl -X POST http://localhost:5678/webhook/telegram-in \
  -H "Content-Type: application/json" \
  -d '{"event":"incoming_message","peer_id":123,"peer_name":"Тест","message_id":1,"text":"Привет! Как дела?"}'

# Проверка callback-сервера юзербота
curl -X POST http://localhost:8123/api/command \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: change-me-strong-secret" \
  -d '{"command":"status"}'
```

## 📚 Документация

- [SETUP.md](docs/SETUP.md) — полная настройка
- [JSON_FORMAT.md](docs/JSON_FORMAT.md) — форматы JSON
- [N8N_WORKFLOW.md](docs/N8N_WORKFLOW.md) — структура workflow и переменные n8n
