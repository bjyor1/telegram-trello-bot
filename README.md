# Brad Bot v1

A drop-in upgrade for the existing Flask/Render Telegram → Trello capture bot.

## What it does

- Accepts Telegram text and voice notes.
- Splits a casual brain dump into separate, concise Trello checklist items.
- Extracts only explicit due dates and appends them to checklist-item names.
- Keeps the existing Siri `POST /capture` route.
- Falls back to saving the original text if AI parsing fails.
- Restricts Telegram use to one chat ID when configured.
- Verifies Telegram's webhook secret when configured.
- Answers work-queue questions from the `PIPELINE` board (overdue, today, inbox, and upcoming).
- Updates native Trello checklist due dates from Telegram commands.

This first version deliberately does **not** send email, alter calendars, delete
data, or autonomously reprioritize tasks.

## Render setup

1. Copy these files over the existing app (or deploy this directory as a new service).
2. Keep the existing Trello, Telegram, and `CAPTURE_SECRET` environment values.
3. Add the new values from `.env.example`: `OPENAI_API_KEY`,
   `TELEGRAM_ALLOWED_CHAT_ID`, `TELEGRAM_WEBHOOK_SECRET`, and `BOT_TIMEZONE`.
   Set `TRELLO_BOARD_SHORTLINK=MPjZR28c` for the `PIPELINE` board (this is the
   default if omitted).
4. Use build command `pip install -r requirements.txt`.
5. Use start command `gunicorn app:app`.

To set the Telegram webhook with secret validation:

```bash
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=https://YOUR-RENDER-SERVICE.onrender.com/telegram-webhook" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

## Siri request

`POST /capture` with JSON `{ "task": "Call Smithfield tomorrow" }` and the
existing Siri header:

```text
X-CAPTURE-SECRET: <CAPTURE_SECRET>
```

`Authorization: Bearer <CAPTURE_SECRET>` is also supported.

## Local verification

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
```

## Recommended next step

Telegram examples:

```text
What do I need to work on today?
What is overdue or urgent?
Push Send Arcosa proposal to next Monday.
```

Cross-card inbox moves are the next addition; they should create and verify the
destination item before removing the source item.

