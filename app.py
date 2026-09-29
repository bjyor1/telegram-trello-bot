import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request
from openai import OpenAI
from pydantic import BaseModel, Field


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("brad-bot")

app = Flask(__name__)
PENDING_ACTIONS: dict[int, tuple[str, dict]] = {}


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
    # Telegram rejects messages longer than 4096 characters. Keep headroom and
    # split on line boundaries so large board summaries still arrive.
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > 3800:
            chunks.append(current.rstrip())
            current = ""
        while len(line) > 3800:
            chunks.append(line[:3800])
            line = line[3800:]
        current += line
    if current.strip():
        chunks.append(current.rstrip())
    for chunk in chunks or [text]:
        telegram_api("sendMessage", json={"chat_id": chat_id, "text": chunk})


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
        model=os.getenv("OPENAI_TASK_MODEL", "gpt-6-luna"),
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


def trello_request(method: str, path: str, **kwargs):
    params = kwargs.pop("params", {})
    params.update({"key": env("TRELLO_KEY"), "token": env("TRELLO_TOKEN")})
    response = requests.request(
        method, f"https://api.trello.com/1{path}", params=params, timeout=30, **kwargs
    )
    response.raise_for_status()
    return response.json()


def pipeline_board() -> dict:
    return trello_request("GET", f"/boards/{os.getenv('TRELLO_BOARD_SHORTLINK', 'MPjZR28c')}", params={"fields": "name"})


def pipeline_cards() -> list[dict]:
    return trello_request(
        "GET",
        f"/boards/{os.getenv('TRELLO_BOARD_SHORTLINK', 'MPjZR28c')}/cards",
        params={"fields": "name,idList,url,due,closed"},
    )


def card_checklists(card_id: str) -> list[dict]:
    return trello_request(
        "GET", f"/cards/{card_id}/checklists", params={"checkItems": "true"}
    )


def pipeline_checklists() -> list[dict]:
    return trello_request(
        "GET",
        f"/boards/{os.getenv('TRELLO_BOARD_SHORTLINK', 'MPjZR28c')}/checklists",
        params={
            "fields": "name,idCard",
            "checkItems": "all",
            "checkItem_fields": "name,state,due,idChecklist",
        },
    )


def all_pipeline_items() -> list[dict]:
    items = []
    cards = {card["id"]: card for card in pipeline_cards()}
    for checklist in pipeline_checklists():
        card = cards.get(checklist.get("idCard"))
        if not card:
            continue
        for item in checklist.get("checkItems", []):
            items.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "state": item.get("state", "incomplete"),
                    "due": item.get("due"),
                    "card_id": card["id"],
                    "card_name": card["name"],
                    "checklist_id": checklist["id"],
                    "checklist_name": checklist["name"],
                    "url": card.get("url"),
                }
            )
    return items


def item_due(item: dict) -> date | None:
    if not item.get("due"):
        return None
    return datetime.fromisoformat(item["due"].replace("Z", "+00:00")).date()


def work_summary() -> str:
    today = datetime.now(ZoneInfo(os.getenv("BOT_TIMEZONE", "Australia/Melbourne"))).date()
    items = [i for i in all_pipeline_items() if i["state"] != "complete"]
    overdue = sorted(
        (i for i in items if item_due(i) and item_due(i) < today),
        key=item_due,
    )
    due_today = sorted(
        (i for i in items if item_due(i) == today), key=lambda i: i["card_name"]
    )
    inbox = [i for i in items if i["card_name"].strip().upper() == "INBOX BOT"]
    upcoming = sorted(
        (i for i in items if item_due(i) and today < item_due(i) <= today + timedelta(days=7)),
        key=item_due,
    )

    def lines(group):
        return [
            f"• {i['name']} — {i['card_name']}"
            + (f" — {item_due(i).isoformat()}" if item_due(i) else " — no date")
            for i in group
        ]

    out = [f"PIPELINE work brief — {today.isoformat()}"]
    for label, group in (("OVERDUE", overdue), ("DUE TODAY", due_today), ("INBOX BOT", inbox), ("NEXT 7 DAYS", upcoming)):
        out.append(f"\n{label} ({len(group)})")
        out.extend(lines(group[:10]) or ["• None"])
        if len(group) > 10:
            out.append(f"• …and {len(group) - 10} more")
    return "\n".join(out)


def parse_requested_date(text: str) -> str | None:
    timezone = ZoneInfo(os.getenv("BOT_TIMEZONE", "Australia/Melbourne"))
    today = datetime.now(timezone).date()
    lower = text.lower()
    if "today" in lower:
        return today.isoformat()
    if "tomorrow" in lower:
        return (today + timedelta(days=1)).isoformat()
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", lower)
    if match:
        return match.group(1)
    weekdays = {name.lower(): idx for idx, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}
    for name, idx in weekdays.items():
        if name in lower:
            delta = (idx - today.weekday()) % 7 or 7
            return (today + timedelta(days=delta)).isoformat()
    return None


def update_checkitem(item: dict, due_date: str) -> None:
    trello_request(
        "PUT",
        f"/cards/{item['card_id']}/checkItem/{item['id']}",
        params={"due": f"{due_date}T23:59:00.000Z"},
    )


def find_item(query: str) -> dict | None:
    query = query.strip().lower()
    items = [i for i in all_pipeline_items() if i["state"] != "complete"]
    exact = [i for i in items if i["name"].lower() == query]
    if exact:
        return exact[0]
    matches = [i for i in items if query in i["name"].lower()]
    return matches[0] if len(matches) == 1 else None


def find_card(query: str) -> dict | None:
    query = query.strip().lower()
    cards = [c for c in pipeline_cards() if not c.get("closed")]
    exact = [c for c in cards if c["name"].lower() == query]
    if exact:
        return exact[0]
    matches = [c for c in cards if query in c["name"].lower()]
    return matches[0] if len(matches) == 1 else None


def move_item(item: dict, target_card: dict, due_date: str) -> None:
    checklists = card_checklists(target_card["id"])
    if not checklists:
        raise RuntimeError(f"Target card {target_card['name']} has no checklist")
    target_checklist = checklists[0]
    created = trello_request(
        "POST",
        f"/checklists/{target_checklist['id']}/checkItems",
        params={
            "name": item["name"],
            "pos": "top",
            "due": f"{due_date}T23:59:00.000Z",
        },
    )
    try:
        trello_request(
            "DELETE",
            f"/cards/{item['card_id']}/checkItem/{item['id']}",
        )
    except Exception:
        logger.exception("Created destination item %s but could not remove source", created)
        raise


def handle_trello_command(text: str, chat_id: int | None = None) -> str | None:
    lower = text.lower().strip()
    if chat_id and lower in {"yes", "confirm", "y", "/confirm"}:
        pending = PENDING_ACTIONS.pop(chat_id, None)
        if not pending:
            return "There is no pending Trello change to confirm."
        action, data = pending
        if action == "date":
            update_checkitem(data["item"], data["due"])
            return f"Updated ‘{data['item']['name']}’ on {data['item']['card_name']} to {data['due']}."
        move_item(data["item"], data["target"], data["due"])
        return f"Moved ‘{data['item']['name']}’ from {data['item']['card_name']} to {data['target']['name']} and set it due {data['due']}."
    if chat_id and lower in {"no", "cancel", "/cancel"}:
        PENDING_ACTIONS.pop(chat_id, None)
        return "Cancelled. Nothing was changed in Trello."
    if lower in {"/today", "/work", "/brief"} or any(
        phrase in lower
        for phrase in ("what do i need to work on", "what should i work on", "anything urgent", "what's outstanding", "whats outstanding")
    ):
        return work_summary()

    if lower.startswith(("push ", "move the due date", "change the due date")):
        due = parse_requested_date(text)
        if not due:
            return "Tell me the new date, for example: ‘Push Send proposal to next Monday.’"
        cleaned = re.sub(r"\b(today|tomorrow|next\s+\w+|20\d{2}-\d{2}-\d{2})\b", "", text, flags=re.I)
        cleaned = re.sub(r"^(push|move the due date|change the due date)\s+", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s+(to|until|out)\s*$", "", cleaned, flags=re.I).strip(" .")
        item = find_item(cleaned)
        if not item:
            return "I couldn’t uniquely identify that checklist item. Include a few more words from its exact name."
        if chat_id is None:
            return "I found the item, but I need Telegram confirmation before changing Trello."
        PENDING_ACTIONS[chat_id] = ("date", {"item": item, "due": due})
        return f"I’ll change ‘{item['name']}’ on {item['card_name']} to {due}. Reply ‘confirm’ to apply it."

    if lower.startswith("move "):
        due = parse_requested_date(text)
        if not due:
            return "I need a due date before moving an item. Try: ‘Move X to Arcosa, due Friday.’"
        match = re.match(r"move\s+(.+?)\s+to\s+(.+?)(?:,?\s+(?:due|on|for)\s+.+)?$", text, re.I)
        if not match:
            return "Try: ‘Move Send proposal to Arcosa, due Friday.’"
        item = find_item(match.group(1))
        target = find_card(match.group(2))
        if not item or not target:
            return "I couldn’t uniquely identify both the checklist item and destination card."
        if item["card_id"] == target["id"]:
            if chat_id is None:
                return "I found the item, but I need Telegram confirmation before changing Trello."
            PENDING_ACTIONS[chat_id] = ("date", {"item": item, "due": due})
            return f"That item is already on {target['name']}. I’ll set it due {due}. Reply ‘confirm’ to apply it."
        if chat_id is None:
            return "I found the item and destination, but I need Telegram confirmation before moving it."
        PENDING_ACTIONS[chat_id] = ("move", {"item": item, "target": target, "due": due})
        return f"I’ll move ‘{item['name']}’ from {item['card_name']} to {target['name']} and set it due {due}. Reply ‘confirm’ to apply it."
    return None


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
                "I can capture tasks, brief you on PIPELINE, and update Trello.\n\n"
                "Try: ‘What do I need to work on today?’ or ‘Push Send proposal to next Monday.’",
            )
            return jsonify(ok=True)
        if not text and message.get("voice"):
            temp_path = download_voice(message["voice"]["file_id"])
            text = transcribe_voice(temp_path)
        if not text:
            send_telegram(chat_id, "Send me text or a voice note and I’ll capture it.")
            return jsonify(ok=True)

        command_response = handle_trello_command(text, chat_id)
        if command_response:
            send_telegram(chat_id, command_response)
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
