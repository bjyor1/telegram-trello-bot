import os
import logging

from flask import Flask, request, jsonify
import requests

# --- Config ---

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TRELLO_KEY = os.environ.get("TRELLO_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")
TRELLO_CHECKLIST_ID = os.environ.get("TRELLO_CHECKLIST_ID")

if not all([TELEGRAM_BOT_TOKEN, TRELLO_KEY, TRELLO_TOKEN, TRELLO_CHECKLIST_ID]):
    raise RuntimeError("Missing one or more required environment variables")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TRELLO_API_BASE = "https://api.trello.com/1"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


# --- Helper functions ---

def add_checkitem_to_trello(task_text: str) -> dict:
    """Add a checklist item to the configured Trello checklist."""
    url = f"{TRELLO_API_BASE}/checklists/{TRELLO_CHECKLIST_ID}/checkItems"
    params = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "name": task_text,
        "pos": "top",  # or "bottom"
    }

    resp = requests.post(url, params=params)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        logger.error("Error from Trello API: %s - response: %s", e, resp.text)
        raise

    return resp.json()


def send_telegram_message(chat_id: int, text: str) -> None:
    """Send a message back to a Telegram chat."""
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    resp = requests.post(url, json=payload)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        logger.error("Error sending Telegram message: %s - response: %s", e, resp.text)


# --- Webhook endpoint ---

@app.route("/telegram-webhook", methods=["GET", "POST"])
def telegram_webhook():
    if request.method == "GET":
        return "Telegram webhook endpoint is live", 200

    update = request.get_json(force=True, silent=True)
    logger.info("Received update: %s", update)

    if not update:
        return jsonify({"ok": False, "description": "No update payload"}), 400

    message = update.get("message") or update.get("edited_message")
    if not message:
        # Ignore non-message updates
        return jsonify({"ok": True}), 200

    chat_id = message["chat"]["id"]
    text = message.get("text")

    if not text:
        send_telegram_message(chat_id, "Please send plain text tasks only for now.")
        return jsonify({"ok": True}), 200

    text = text.strip()

    # Handle simple commands
    if text.startswith("/start"):
        send_telegram_message(
            chat_id,
            "Hey! Send me any message and I'll add it as a checklist item in Trello."
        )
        return jsonify({"ok": True}), 200

    # Otherwise, treat the text as a task
    task_text = text

    try:
        trello_response = add_checkitem_to_trello(task_text)
        item_name = trello_response.get("name", task_text)
        send_telegram_message(chat_id, f"Added to Trello checklist: {item_name}")
    except Exception:
        logger.exception("Failed to add checklist item")
        send_telegram_message(
            chat_id,
            "Oops, I couldn't add that to Trello. Check configuration/logs."
        )

    return jsonify({"ok": True}), 200


@app.route("/", methods=["GET"])
def index():
    return "Telegram → Trello bot is running", 200


if __name__ == "__main__":
    # Local dev only; Render will use gunicorn.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
