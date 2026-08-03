# 🔧 Полная настройка «с нуля»

## 1. Проект и окружение Python

```bash
mkdir -p ~/telegram-ai-responder
cd ~/telegram-ai-responder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 2. Настройка `.env`

```ini
API_ID=611335
API_HASH=e86032b40b0213197262024220b333a2
N8N_WEBHOOK_URL=http://localhost:5678/webhook/telegram-in
CALLBACK_API_KEY=придумайте-длинный-секрет
BOT_TOKEN=1234567890:AAA...BBB   # токен управляющего бота @myaccounttbot
MY_TELEGRAM_ID=512345678         # ваш user id (у @userinfobot); можно пусто
SERVICE_CHAT_ID=me
AI_MODE=bot_chat                 # кнопки в личном чате с ботом (рекомендуется)
```

## 3. Первый запуск юзербота (авторизация)

```bash
python userbot.py
```

Pyrogram попросит:
1. **номер телефона** (в формате +7…) — придёт код в Telegram,
2. **код подтверждения**,
3. **пароль 2FA**, если он включён.

Создастся файл сессии `ai_responder.session`. **Никогда не коммитьте его в git.**
Если аккаунт уже запущен в другом месте — Telegram может принудительно завершить
ту сессию; это нормально.

> ⚠️ Учётная запись должна быть **не заблокирована** для авторизации API.
> Использование юзерботов — на ваш риск, соблюдайте правила Telegram
> (не спамьте, не превышайте лимиты).

## 4. Ключи AI (бесплатно)

- **Gemini:** https://aistudio.google.com/ → **Get API key** → скопировать ключ.
  Модели Free Tier: `gemini-3.5-flash`, `gemini-3.1-flash-lite` (проверьте
  актуальный список в консоли AI Studio).
- **Groq:** https://console.groq.com/ → API Keys → Create API Key (карта не нужна).
  Модели: `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`.

## 5. Telegram-бот (BotFather) — пульт управления

1. В [@BotFather](https://t.me/BotFather): `/newbot` → имя → токен
   (например, **@myaccounttbot**). Токен впишите в `.env` как `BOT_TOKEN`.
2. Напишите своему боту любой текст (например, `/start`) — так вы создадите
   с ним личный чат. **Никакая группа не нужна**: варианты ответа с кнопками
   бот будет присылать прямо в этот чат.
3. Узнайте свой **user id**: отправьте сообщение [@userinfobot](https://t.me/userinfobot)
   и возьмите число из ответа. Впишите его в `.env` как `MY_TELEGRAM_ID`
   (можно не заполнять — тогда возьмётся id вашего аккаунта).

> Режим `AI_MODE=bot` (кнопки через n8n) по-прежнему требует приватную группу
> с ботом и публичный URL: создайте группу, добавьте бота и себя, а id группы
> (начинается с `-100…`) впишите в `SERVICE_CHAT_ID`.

## 6. n8n

### Локально (Ubuntu/Docker)

```bash
docker run -d --name n8n \
  -p 5678:5678 \
  -e N8N_HOST=localhost \
  -v n8n_data:/home/node/.n8n \
  n8nio/n8n
```

Откройте `http://localhost:5678`.

### Публичный URL (нужен для вебхуков Telegram-бота)

Telegram должен достучаться до n8n. Варианты:
- **ngrok:** `ngrok http 5678` → адрес вида `https://xxxx.ngrok-free.app`
- **cloudflared:** `cloudflared tunnel --url http://localhost:5678`
- **tailscale funnel**, frp, или бесплатный хостинг (например, Render/Railway с Dockerfile).

В n8n: **Settings → n8n Host/Webhook URL** укажите публичный адрес
(`https://xxxx.ngrok-free.app`).

### Переменные окружения n8n

**Settings → Environment variables** (или в `docker run -e …`):

> **Режим `bot_chat`:** n8n нужен только для генерации вариантов (Workflow 1).
> Для резервирования добавьте `GROQ_API_KEY`, а для уведомления о 429 —
> `TELEGRAM_BOT_TOKEN` и `OWNER_ID` (sendMessage не конфликтует с long polling
> юзербота). `SERVICE_CHAT_ID` и `USERBOT_CALLBACK_KEY` в этом режиме не нужны;
> **Workflow 2 деактивируйте** — нажатия кнопок обрабатывает сам юзербот
> (long polling), иначе будет `409 Conflict` по токену бота.

| Переменная | Значение |
|---|---|
| `GEMINI_API_KEY` | ключ Gemini (Free Tier) |
| `GEMINI_MODEL` | `gemini-1.5-flash` (по умолчанию) |
| `GROQ_API_KEY` | ключ Groq — резервная модель `llama-3.3-70b-versatile` (https://console.groq.com/) |
| `TELEGRAM_BOT_TOKEN` | токен бота из BotFather |
| `SERVICE_CHAT_ID` | id группы (служебный чат) |
| `OWNER_ID` | ваш user id (фильтр нажатий в Workflow 2) |
| `USERBOT_CALLBACK_KEY` | тот же секрет, что `CALLBACK_API_KEY` в `.env` |

### Импорт workflow

**Workflow 1** (`n8n/workflow_ai_responder.json`): генерация вариантов с
резервированием Gemini → Groq.
**Workflow 2** (`n8n/workflow_send_callbacks.json`): обработка кнопок.

> **Обновление Workflow 1:** удалите старую версию и импортируйте новую (URL
> вебхука `/webhook/telegram-in` не изменится), затем задайте в окружении n8n
> `GEMINI_API_KEY`, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN` и `OWNER_ID`
> (см. таблицу выше). В старом JSON ключ Gemini был захардкожен прямо в URL —
> уберите его из ноды (ключ мог утечь в git-историю; при необходимости
> перевыпустите его в AI Studio).

Импорт: n8n → **… → Import from File**. В Workflow 2 выберите Telegram-аккаунт
(создаётся с тем же токеном бота) и укажите вебхук бота: **… → Settings →
Telegram Trigger → Use existing / Set up** — n8n сам зарегистрирует `setWebhook`.

**Активируйте оба workflow** (переключатель Active). Workflow 2 при активации
зарегистрирует вебхук бота на вашем публичном URL.

## 7. Тест

1. `curl` вебхука (см. `JSON_FORMAT.md`).
2. Напишите себе в ЛС со второго аккаунта/телефона.
3. В личном чате с ботом должны появиться кнопки **[1] [2] [3]** → нажмите
   нужную; сообщение отредактируется на `✅ Отправлено для …`.
4. В логах юзербота: `Callback-команда от n8n: send_reply`.

## 8. Запуск юзербота как службы (systemd)

```ini
# /etc/systemd/system/ai-responder.service
[Unit]
Description=Telegram AI Responder userbot
After=network-online.target

[Service]
WorkingDirectory=/home/mansurov/telegram-ai-responder
ExecStart=/home/mansurov/telegram-ai-responder/.venv/bin/python userbot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai-responder
journalctl -u ai-responder -f
```

## 9. Устранение неполадок

| Проблема | Решение |
|---|---|
| Юзербот не видит ЛС | Проверьте, что сессия не упала; фильтры исключают только свои и сервисные сообщения |
| n8n не отвечает на вебхук | Workflow 1 должен быть **Active**; проверьте `N8N_WEBHOOK_URL` |
| Бот не шлёт кнопки | Проверьте `TELEGRAM_BOT_TOKEN`, `SERVICE_CHAT_ID`, публичный URL |
| Нажатие кнопки ничего не делает | Workflow 2 активен? Юзербот запущен? `USERBOT_CALLBACK_KEY` совпадает с `CALLBACK_API_KEY`? |
| «Контекст устарел» | Юзербот перезапущен, варианты в памяти потеряны — это ожидаемо (см. раздел про хранение) |
| Кнопки не появляются в чате с ботом | Проверьте `BOT_TOKEN`, `AI_MODE=bot_chat`, и что вы написали боту `/start` |
| `409 Conflict` в логах бота | Тем же токеном пользуется ещё что-то (например, n8n Workflow 2) — деактивируйте лишнего получателя |
| Gemini 429 | Обрабатывается автоматически: запрос уходит на резервную Groq, вам приходит уведомление (нужны `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN` и `OWNER_ID` в окружении n8n) |
