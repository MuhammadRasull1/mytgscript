# ⚙️ Структура n8n Workflow

Импортируются два workflow из папки `n8n/`. JSON — рабочий шаблон: после импорта
проверьте узлы в редакторе (n8n мог запросить привязку Telegram-аккаунта).

## Workflow 1: «AI Responder: генерация вариантов ответа»

```
[Вебхук от userbot] → [Собрать промпт] → [Gemini API] → [IF: ошибка Gemini?] ─┬ false → [Подготовить варианты] → [Ответ вебхуку]
                                                                                        └ true  → [Уведомление: лимит Gemini] → [Groq API] → [Подготовить варианты] → [Ответ вебхуку]
```

| Узел | Тип | Назначение |
|---|---|---|
| **Вебхук от userbot** | `webhook` v2 | `POST /webhook/telegram-in`, `responseMode: responseNode` |
| **Собрать промпт** | `code` | Собирает `systemPrompt` (правила стиля: Мама → узбекский кириллицей, Папа → зеркалирование языка, остальные → живой русский с эмодзи) и `userPrompt` (имя собеседника + текст) из тела вебхука. Единственный источник промпта для обеих моделей |
| **Gemini API** | `httpRequest` v4.2 | **Основная модель.** `POST https://generativelanguage.googleapis.com/v1beta/models/{{$env.GEMINI_MODEL || 'gemini-1.5-flash'}}:generateContent?key={{$env.GEMINI_API_KEY}}`; `onError: continueRegularOutput` — при 429/таймауте ошибка попадает в `$json.error` |
| **IF: ошибка Gemini?** | `if` v2 | Ветвление: `$json.error != null`? **false** (выход 0) → варианты готовы; **true** (выход 1) → резервная цепочка |
| **Уведомление: лимит Gemini** | `httpRequest` v4.2 | Bot API `sendMessage` владельцу (`OWNER_ID`): «⚠️ Лимит Gemini (429) исчерпан! …». `onError: continueRegularOutput` — при недоступном токене уведомление тихо пропускается, fallback продолжает работу |
| **Groq API** | `httpRequest` v4.2 | **Запасная модель.** `POST https://api.groq.com/openai/v1/chat/completions`, модель `llama-3.3-70b-versatile`, ключ `$env.GROQ_API_KEY`, тот же промпт (берётся из узла «Собрать промпт»), `response_format: json_object` |
| **Подготовить варианты** | `code` | Универсальный парсер: `candidates` (Gemini) **или** `choices` (Groq); чистит markdown-обёртку, берёт до 3 вариантов. Добавляет `provider` и `fallback_used` |
| **Ответ вебхуку** | `respondToWebhook` v1 | Отдаёт `{ok, error, suggestions, provider, fallback_used}` юзерботу (тот кладёт варианты в память) |

Кнопки (`callback_data` ≤ 64 байт):
```json
{"a":"send","p":<peer_id>,"m":<message_id>,"i":<index>}
{"a":"edit","p":<peer_id>,"m":<message_id>,"i":<index>}
{"a":"skip","p":<peer_id>,"m":<message_id>}
```

> **Режим `AI_MODE=bot_chat`** (по умолчанию, кнопки в личном чате с ботом):
> n8n возвращает `{ok, suggestions, provider, fallback_used}` — варианты и inline-кнопки
> **[1] [2] [3]** отправляет в чат с @myaccounttbot сам юзербот через Bot API,
> а нажатия ловит его встроенный long-polling (`bot_api.py`). **Workflow 2
> не нужен** — деактивируйте его, иначе будет `409 Conflict` по токену бота.
>
> **Режим `AI_MODE=userbot`** (старый, «Избранное»): то же самое, но кнопки
> рисуются в «Избранном» и нажатия обрабатывает сам юзербот. Узел «Бот:
> показать варианты» и Workflow 2 не нужны.

Тест (вход вебхука):
```bash
curl -X POST http://localhost:5678/webhook/telegram-in \
  -H "Content-Type: application/json" \
  -d '{"event":"incoming_message","peer_id":123,"peer_name":"Тест","message_id":1,"text":"Привет!"}'
```

## Workflow 2: «AI Responder: callback-команды (кнопки)»

```
[Telegram Trigger] → [Парсинг callback] ─┬→ [Команда userbot: отправить] → [Ответ боту: ✅] → [Пометить отправленным]
                                         ├→ [Команда userbot: редактировать] → [Ответ боту: ✏️]
                                         └→ [Ответ боту: ⏭]
```

| Узел | Тип | Назначение |
|---|---|---|
| **Telegram Trigger (кнопки)** | `telegramTrigger` v1.1 | Слушает `callback_query` (нужен публичный URL и активный workflow — n8n сам зарегистрирует `setWebhook`) |
| **Парсинг callback** | `code` | Парсит `callback_data`, проверяет `OWNER_ID`, собирает команду. Выходы: 0=send, 1=edit, 2=skip |
| **Команда userbot: …** | `httpRequest` | `POST http://localhost:8123/api/command` с заголовком `X-Api-Key: {{$env.USERBOT_CALLBACK_KEY}}` и телом `{command, peer_id, message_id, index}` |
| **Ответ боту: ✅ / ✏️ / ⏭** | `httpRequest` | `answerCallbackQuery` (тост-уведомление) |
| **Пометить отправленным** | `httpRequest` | `editMessageText` — дописывает «✅ Отправлено» в сообщение бота |

## Переменные окружения n8n

Указываются в **Settings → Environment variables** (или `-e` в docker):

| Переменная | Пример | Для чего |
|---|---|---|
| `GEMINI_API_KEY` | `AIza…` | ключ Gemini (Free Tier). **Вместо ключа, захардкоженного в URL старого JSON** |
| `GEMINI_MODEL` | `gemini-1.5-flash` | модель Gemini (опционально, есть дефолт) |
| `GROQ_API_KEY` | `gsk_…` | ключ Groq — для резервной модели `llama-3.3-70b-versatile` (https://console.groq.com/) |
| `TELEGRAM_BOT_TOKEN` | `123456:ABC…` | токен бота из BotFather. Нужен и для уведомления о 429 (sendMessage не конфликтует с long-polling юзербота) |
| `SERVICE_CHAT_ID` | `-1001234567890` | служебный чат (группа с ботом и вами) |
| `OWNER_ID` | `512345678` | ваш user id — кому n8n шлёт уведомление о 429 (и фильтр нажатий в Workflow 2) |
| `USERBOT_CALLBACK_KEY` | секрет | должен совпадать с `CALLBACK_API_KEY` в `.env` юзербота |

> В режиме `bot_chat` для генерации вариантов нужны `GEMINI_API_KEY` (и опц.
> `GEMINI_MODEL`), а для **резервирования** — `GROQ_API_KEY` и (для уведомления
> о 429) `TELEGRAM_BOT_TOKEN` + `OWNER_ID`. `SERVICE_CHAT_ID`,
> `USERBOT_CALLBACK_KEY` используются лишь в режиме `bot` (кнопки через n8n).

## 🔁 Резервирование: Gemini → Groq (автоматически, при 429)

В Workflow 1 встроена цепочка резервирования (ноды «IF: ошибка Gemini?»,
«Уведомление: лимит Gemini», «Groq API»):

1. При каждом запросе сначала пробуется **Gemini** (`gemini-1.5-flash`).
2. Если нода вернула ошибку (429 Too Many Requests, таймаут, сеть) —
   `onError: continueRegularOutput` кладёт ошибку в `$json.error`, и нода
   **IF** отправляет запрос в резервную ветку:
   - **Уведомление:** в ваш Telegram (Bot API `sendMessage` на `OWNER_ID`)
     уходит сообщение «⚠️ Лимит Gemini (429) исчерпан! Автоматически
     переключаюсь на резервную ИИ (Groq / Llama 3.3)…». Если токен бота не
     задан — уведомление тихо пропускается (`onError: continueRegularOutput`),
     fallback не ломается.
   - **Groq** (`llama-3.3-70b-versatile`, тот же промпт из узла «Собрать
     промпт») генерирует варианты и отвечает вебхуку как обычно.
3. Следующий запрос снова начинается с Gemini — лимит сбрасывается каждую
   минуту, поэтому Gemini продолжит использоваться, когда лимит отпустит.
4. Если и Groq упал — вебхук получит `{ok:false, suggestions:[]}`, юзербот
   напишет предупреждение в служебный чат.
5. Fallback срабатывает только на **ошибку ноды** (429, таймаут, сеть). Если
   Gemini ответил без ошибки, но варианты не распарсились (некорректный JSON) —
   резервная ветка не включается, вебхук вернёт `{ok:false}`. При желании
   условие ноды IF можно расширить, чтобы fallback срабатывал и в этом случае.

## ⚠️ Особенности и ограничения

- **Память юзербота (PENDING) в RAM**: после перезапуска `userbot.py` старые
  варианты теряются — кнопка вернёт «Контекст устарел». Если нужно переживать
  перезапуски, замените словарь `PENDING` на SQLite/файл (подсказка в коде).
- **В режиме `bot_chat`** после перезапуска старые кнопки остаются в чате,
  но по нажатию вернут «Контекст устарел» и снимутся сами — это ожидаемо.
- **`callback_data` ≤ 64 байт** — поэтому в кнопках только id, а текст варианта
  юзербот достаёт из памяти.
- **Гонка (несущественна)**: ветки «Ответ вебхуку» и «Бот: показать варианты» выполняются параллельно, поэтому кнопки могут появиться на доли секунды раньше, чем юзербот сохранит варианты. Человек кликает на секунды позже — на практике не мешает.
- **Вебхук бота** (Workflow 2) требует публичный URL n8n — локально ngrok:
  `ngrok http 5678` и укажите адрес в **Settings → Webhook URL**, затем
  деактивируйте/активируйте Workflow 2, чтобы n8n перерегистрировал вебхук.
- **Ошибка Gemini (429/ошибка сети)**: автоматически срабатывает резервная
  модель Groq (см. раздел «Резервирование»); владельцу уходит уведомление.
  Если и Groq упал — Workflow 1 вернёт `{ok:false}`, юзербот напишет
  предупреждение в служебный чат.
- **Таймаут**: юзербот ждёт ответ вебхука до `N8N_TIMEOUT_SEC` (90 с).
- **Обработка ошибок**: на HTTP-нодах включён `onError: continueRegularOutput` —
  при сбое Gemini запрос уходит на Groq; если и Groq недоступен, вебхук быстро
  отвечает `{ok:false}`, а при недоступности юзербота кнопка получает тост
  вместо «зависания».
- **Лимиты**: Gemini Free ~10–15 RPM — при 429 запрос автоматически уходит на
  Groq (`llama-3.3-70b-versatile` ~30 RPM / 1000 RPD). При очень большом
  потоке ЛС добавьте в Workflow 1 узел `If` с ограничением частоты или
  «тихие часы».
