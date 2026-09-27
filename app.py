import logging
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request
from openai import OpenAI
from pydantic import BaseModel, Field


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("brad-bot")

app = Flask(__name__)


class Task(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    due_date: str | None = Field(
        default=None,
        description="ISO date YYYY-MM-DD, or null when the user gave no deadline",
    )


class TaskBatch(BaseModel):
    tasks: list[Task] = Field(min_length=1, max_length=20)


def env(name: str, required: bool = True) -> str | None:
    value = os.getenv(name)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def telegram_api(method: str, **kwargs):
    token = env("TELEGRAM_BOT_TOKEN")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}", timeout=30, **kwargs
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload["result"]


def send_telegram(chat_id: int, text: str) -> None:
    telegram_api("sendMessage", json={"chat_id": chat_id, "text": text})


def authorized_chat(chat_id: int) -> bool:
    allowed = env("TELEGRAM_ALLOWED_CHAT_ID", required=False)
    return not allowed or str(chat_id) == allowed


def download_voice(file_id: str) -> str:
    file_info = telegram_api("getFile", json={"file_id": file_id})
    file_path = file_info["file_path"]
    token = env("TELEGRAM_BOT_TOKEN")
    response = requests.get(
        f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=30
    )
    response.raise_for_status()
    suffix = os.path.splitext(file_path)[1] or ".ogg"
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(response.content)
        return handle.name
    finally:
        handle.close()


def transcribe_voice(path: str) -> str:
    client = OpenAI(api_key=env("OPENAI_API_KEY"))
    with open(path, "rb") as audio:
        result = client.audio.transcriptions.create(
            model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-transcribe"),
            file=audio,
            prompt=(
                "Personal task capture. Likely terms include Trello, Voltus, "
                "MISO, PJM, ERCOT, Smithfield, Arcosa, Theo, and Bec."
            ),
        )
    return result.text.strip()


def parse_tasks(text: str) -> list[Task]:
    if not env("OPENAI_API_KEY", required=False):
        return [Task(title=text.strip())]

    timezone = os.getenv("BOT_TIMEZONE", "America/Denver")
    now = datetime.now(ZoneInfo(timezone))
    client = OpenAI()
    response = client.responses.parse(
        model=os.getenv("OPENAI_TASK_MODEL", "gpt-6-astra"),
        input=[
            {
                "role": "system",
                "content": (
                    "Turn a casual task dump into distinct actionable tasks. "
                    "Split only genuinely separate actions. Use short verb-first titles. "
                    "Preserve names, companies, programs, places, and important context. "
                    "Resolve explicit relative dates from the supplied local timestamp. "
                    "Never invent a due date. Do not add tasks the user did not request."
                ),
            },
            {
                "role": "user",
                "content": f"Local time: {now.isoformat()}\nTask dump: {text}",
            },
        ],
        text_format=TaskBatch,
    )
    parsed = response.output_parsed
    if not parsed or not parsed.tasks:
        raise RuntimeError("Task parser returned no tasks")
    return parsed.tasks


def trello_item_name(task: Task) -> str:
    return f"{task.title} — due {task.due_date}" if task.due_date else task.title


def add_checkitem_to_trello(task: Task) -> None:
    checklist_id = env("TRELLO_CHECKLIST_ID")
    response = requests.post(
        f"https://api.trello.com/1/checklists/{checklist_id}/checkItems",
        params={
            "key": env("TRELLO_KEY"),
            "token": env("TRELLO_TOKEN"),
            "name": trello_item_name(task),
            "pos": "top",
        },
        timeout=30,
    )
    response.raise_for_status()


def capture(text: str) -> list[Task]:
    text = (text or "").strip()
    if not text:
        raise ValueError("Task text is empty")
    try:
        tasks = parse_tasks(text)
    except Exception:
        logger.exception("AI parsing failed; preserving the original capture")
        tasks = [Task(title=text)]
    for task in tasks:
        add_checkitem_to_trello(task)
    return tasks


def confirmation(tasks: list[Task]) -> str:
    heading = f"Captured {len(tasks)} task" + ("s" if len(tasks) != 1 else "")
    lines = [heading]
    for task in tasks:
        due = f" (due {task.due_date})" if task.due_date else ""
        lines.append(f"✓ {task.title}{due}")
    return "\n".join(lines)


@app.get("/")
def health():
    return jsonify(status="ok", service="brad-bot-v1")


@app.route("/capture", methods=["GET", "POST"])
def siri_capture():
    if request.method == "GET":
        return "Capture endpoint is live", 200
    expected = env("CAPTURE_SECRET")
    supplied = request.headers.get("X-CAPTURE-SECRET")
    if not supplied:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if supplied != expected:
        return jsonify(error="unauthorized"), 401
    try:
        tasks = capture((request.get_json(silent=True) or {}).get("task", ""))
        return jsonify(tasks=[task.model_dump() for task in tasks])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        logger.exception("Siri capture failed")
        return jsonify(error="capture failed"), 502


@app.route("/telegram-webhook", methods=["GET", "POST"])
def telegram_webhook():
    if request.method == "GET":
        return "Telegram webhook endpoint is live", 200
    secret = env("TELEGRAM_WEBHOOK_SECRET", required=False)
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        return jsonify(error="unauthorized"), 401

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return jsonify(ok=True)
    if not authorized_chat(chat_id):
        logger.warning("Ignored Telegram message from unauthorized chat %s", chat_id)
        return jsonify(ok=True)

    temp_path = None
    try:
        text = message.get("text")
        if text and text.strip().startswith("/start"):
            send_telegram(
                chat_id,
                "Send me text or a voice note and I’ll turn it into Trello tasks.",
            )
            return jsonify(ok=True)
        if not text and message.get("voice"):
            temp_path = download_voice(message["voice"]["file_id"])
            text = transcribe_voice(temp_path)
        if not text:
            send_telegram(chat_id, "Send me text or a voice note and I’ll capture it.")
            return jsonify(ok=True)

        tasks = capture(text)
        send_telegram(chat_id, confirmation(tasks))
    except Exception:
        logger.exception("Telegram capture failed")
        send_telegram(chat_id, "I couldn’t save that. Nothing was deleted—please try again.")
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                logger.warning("Could not remove temporary voice file")
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
