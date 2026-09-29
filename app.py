import logging
import os
import re
import tempfile
import unicodedata
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Literal
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request
from openai import OpenAI
from pydantic import BaseModel, Field


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("brad-bot")

app = Flask(__name__)
PENDING_ACTIONS: dict[int, dict] = {}


class Task(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    due_date: str | None = Field(
        default=None,
        description="ISO date YYYY-MM-DD, or null when the user gave no deadline",
    )


class TaskBatch(BaseModel):
    tasks: list[Task] = Field(min_length=1, max_length=20)


class BoardAction(BaseModel):
    action: Literal["move", "set_due", "complete", "create"]
    query: str | None = None
    scope: Literal["single", "all_matching", "inbox_all"] = "single"
    target_card: str | None = None
    title: str | None = None
    reference_query: str | None = None
    due_date: str | None = None
    offset_days: int | None = None


class BoardActionBatch(BaseModel):
    actions: list[BoardAction] = Field(min_length=1, max_length=30)


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
    inbox = [
        i
        for i in items
        if i["card_name"].strip().upper() == "INBOX BOT" and not item_due(i)
    ]

    def lines(group):
        return [
            f"• {i['name']} — {i['card_name']}"
            + (f" — {item_due(i).isoformat()}" if item_due(i) else " — no date")
            for i in group
        ]

    out = [f"PIPELINE work brief — {today.isoformat()}"]
    for label, group in (
        ("OVERDUE", overdue),
        ("DUE TODAY", due_today),
        ("INBOX BOT — NO DATE", inbox),
    ):
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
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    month_match = re.search(
        r"\b(" + "|".join(month_names) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b",
        lower,
    )
    if month_match:
        year = int(month_match.group(3) or today.year)
        resolved = date(year, month_names[month_match.group(1)], int(month_match.group(2)))
        if not month_match.group(3) and resolved < today:
            resolved = date(year + 1, resolved.month, resolved.day)
        return resolved.isoformat()
    weeks_match = re.search(r"\b(one|two|three|\d+)\s+weeks?\b", lower)
    if weeks_match:
        word_numbers = {"one": 1, "two": 2, "three": 3}
        weeks = word_numbers.get(weeks_match.group(1), int(weeks_match.group(1)) if weeks_match.group(1).isdigit() else 0)
        return (today + timedelta(weeks=weeks)).isoformat()
    weekdays = {name.lower(): idx for idx, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}
    for name, idx in weekdays.items():
        if name in lower:
            delta = (idx - today.weekday()) % 7 or 7
            return (today + timedelta(days=delta)).isoformat()
    return None


def repair_missing_action_dates(batch: BoardActionBatch, original_text: str) -> None:
    segments = [segment.strip() for segment in re.split(r"[\n.]+", original_text) if segment.strip()]
    for action in batch.actions:
        if action.due_date or action.offset_days is not None or action.action not in {"move", "set_due", "create"}:
            continue
        query = action.query or action.reference_query or action.target_card or ""
        query_words = set(normalized_words(query))
        best_segment = None
        best_overlap = -1
        for segment in segments:
            segment_words = set(normalized_words(segment))
            overlap = len(query_words & segment_words)
            if action.scope == "inbox_all" and "inbox" in segment.lower():
                overlap += 10
            if overlap > best_overlap and parse_requested_date(segment):
                best_segment = segment
                best_overlap = overlap
        if best_segment:
            action.due_date = parse_requested_date(best_segment)


def looks_like_board_command(text: str) -> bool:
    lower = text.lower()
    signals = (
        "tick complete",
        "mark complete",
        "push all",
        "move all",
        "set another checklist",
        "move the due date",
        "change the due date",
    )
    action_lines = sum(
        1
        for line in text.splitlines()
        if line.strip().lower().startswith(("move ", "push ", "tick ", "mark ", "set ", "create "))
    )
    return action_lines > 1 or any(signal in lower for signal in signals)


def looks_like_bot_output(text: str) -> bool:
    lower = text.lower()
    markers = (
        "proposed trello changes:",
        "pipeline work brief",
        "reply ‘confirm’ to apply",
        "reply 'confirm' to apply",
        "captured ",
        "i need clarification before i can safely apply the batch:",
    )
    bullet_due_lines = sum(
        1
        for line in text.splitlines()
        if line.strip().startswith(("•", "✓")) and (" due " in line.lower() or " — " in line)
    )
    return any(marker in lower for marker in markers) or bullet_due_lines >= 2


def requests_confirmation(text: str) -> bool:
    lower = text.lower().strip()
    if lower in {"yes", "confirm", "confirmed", "y", "/confirm"}:
        return True
    # Handles a pasted/quoted preview followed by Confirm without treating all
    # of the quoted checklist lines as new task captures.
    return looks_like_bot_output(text) and bool(re.search(r"(?:^|\n)\s*confirm(?:ed)?\s*$", lower))


def parse_board_actions(text: str, card_names: list[str]) -> BoardActionBatch:
    timezone = ZoneInfo(os.getenv("BOT_TIMEZONE", "Australia/Melbourne"))
    now = datetime.now(timezone)
    client = OpenAI()
    return client.responses.parse(
        model=os.getenv("OPENAI_TASK_MODEL", "gpt-6-luna"),
        input=[
            {
                "role": "system",
                "content": (
                    "Convert Trello instructions into ordered structured actions. "
                    "A user may call checklist items 'cards' or 'stuff'. "
                    "Every date refers to a checklist-item due date; never create or update a Trello card due date. "
                    "Use move for moving checklist items to another card, set_due for date changes, "
                    "complete for ticking items complete, and create for a new checklist item. "
                    "Use scope=inbox_all for all items from INBOX or INBOX BOT; use all_matching for "
                    "phrases like all JBS MISO items. If a short query is an account/card name such as "
                    "Transcendia or JBS MISO, use all_matching because it means every incomplete checklist "
                    "item on that account card. Otherwise use single. Preserve short identifying "
                    "phrases in query. For create-on-that-card instructions, put the prior item phrase "
                    "in reference_query. Resolve explicit dates to YYYY-MM-DD using the local time. "
                    "For relative durations such as two weeks, set offset_days. Never invent an action, "
                    "destination, title, or date. Example: 'Move all cards from INBOX to personal and "
                    "assign a due date to all of October 29th' is one move action with scope=inbox_all, "
                    "target_card=personal, and due_date set to October 29 of the relevant year. Example: "
                    "'Transcendia push to October 29th' is set_due with query=Transcendia and "
                    "scope=all_matching. Example: 'Tick complete on ask Ricky for bills. Set another "
                    "checklist item on that card for Friday to follow up again' is a complete action plus "
                    "a create action referencing the same original item."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Local time: {now.isoformat()}\n"
                    f"Existing card names: {card_names}\n"
                    f"Instructions:\n{text}"
                ),
            },
        ],
        text_format=BoardActionBatch,
    ).output_parsed


def normalized_words(value: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    ignored = {
        "all", "the", "one", "items", "item", "stuff", "card", "cards",
        "on", "for", "up", "to", "please", "again",
    }
    synonyms = {
        "ask": "contact",
        "chase": "contact",
        "contact": "contact",
        "follow": "contact",
        "call": "contact",
        "email": "contact",
        "prep": "prepare",
        "pre": "prepare",
        "prepare": "prepare",
    }
    words = re.findall(r"[a-z0-9]+", folded.lower())
    return [synonyms.get(word, word) for word in words if word not in ignored]


def normalized_text(value: str) -> str:
    return " ".join(normalized_words(value))


def item_match_score(query: str, item: dict) -> float:
    needle = normalized_text(query)
    item_name = normalized_text(item["name"])
    card_name = normalized_text(item["card_name"])
    combined = f"{item_name} {card_name}".strip()
    if needle == item_name:
        return 1.0
    if needle and needle in item_name:
        return 0.96
    query_words = set(needle.split())
    combined_words = set(combined.split())
    overlap = len(query_words & combined_words) / max(len(query_words), 1)
    sequence = max(
        SequenceMatcher(None, needle, item_name).ratio(),
        SequenceMatcher(None, needle, combined).ratio(),
    )
    return max(sequence, overlap * 0.92)


def resolve_items(query: str | None, scope: str, items: list[dict]) -> tuple[list[dict], str | None]:
    incomplete = [item for item in items if item["state"] != "complete"]
    if scope == "inbox_all":
        matches = [item for item in incomplete if item["card_name"].strip().upper() == "INBOX BOT"]
        return matches, None if matches else "No incomplete items were found on INBOX BOT."
    if not query:
        return [], "An item description is missing."
    if not normalized_words(query):
        return [], f"‘{query}’ is too vague to match safely."
    ranked = sorted(
        ((item_match_score(query, item), item) for item in incomplete),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if scope == "all_matching":
        candidates = [item for score, item in ranked if score >= 0.72]
        return candidates, None if candidates else f"No items matched ‘{query}’."
    if not ranked or ranked[0][0] < 0.58:
        return [], f"No item matched ‘{query}’."
    if len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08:
        return [ranked[0][1]], None
    close = [item for score, item in ranked if ranked[0][0] - score < 0.08]
    return [], f"‘{query}’ closely matched {len(close)} items; include more of the item name."


def resolve_card(query: str | None, cards: list[dict]) -> tuple[dict | None, str | None]:
    if not query:
        return None, "A destination card is missing."
    needle = normalized_text(query)
    open_cards = [card for card in cards if not card.get("closed")]
    exact = [card for card in open_cards if normalized_text(card["name"]) == needle]
    if exact:
        return exact[0], None
    partial = [card for card in open_cards if needle in normalized_text(card["name"])]
    if len(partial) == 1:
        return partial[0], None
    ranked = sorted(
        (
            (SequenceMatcher(None, needle, normalized_text(card["name"])).ratio(), card)
            for card in open_cards
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if ranked and ranked[0][0] >= 0.72 and (
        len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08
    ):
        return ranked[0][1], None
    return None, f"Destination ‘{query}’ did not uniquely match a card."


def action_due(action: BoardAction) -> str | None:
    if action.due_date:
        return action.due_date
    if action.offset_days is not None:
        timezone = ZoneInfo(os.getenv("BOT_TIMEZONE", "Australia/Melbourne"))
        return (datetime.now(timezone).date() + timedelta(days=action.offset_days)).isoformat()
    return None


def build_action_plan(batch: BoardActionBatch, items: list[dict], cards: list[dict]) -> tuple[list[dict], list[str]]:
    plans = []
    errors = []
    previous_items: list[dict] = []
    for action in batch.actions:
        query = action.reference_query if action.action == "create" else action.query
        matches = []
        error = None
        # A short account/card name used with set_due means all incomplete
        # checklist items on that card, even if the model marked it as single.
        if action.action == "set_due" and action.scope == "single" and query:
            account_card, card_error = resolve_card(query, cards)
            if not card_error and account_card:
                matches = [
                    item
                    for item in items
                    if item["state"] != "complete" and item["card_id"] == account_card["id"]
                ]
                if not matches:
                    error = f"No incomplete checklist items were found on ‘{account_card['name']}’."
        if not matches and not error:
            matches, error = resolve_items(query, action.scope, items)
        if action.action == "create" and not matches and previous_items and not action.reference_query:
            matches, error = previous_items[-1:], None
        if error:
            errors.append(error)
            continue
        due = action_due(action)
        if action.action in {"move", "set_due", "create"} and not due:
            errors.append(f"No due date was supplied for ‘{action.query or action.title or 'new item'}’. ")
            continue
        target = None
        if action.action == "move":
            target, error = resolve_card(action.target_card, cards)
            if error:
                errors.append(error)
                continue
        if action.action == "create" and not action.title:
            errors.append("A new checklist item was requested without a title.")
            continue
        plans.append({"action": action.action, "items": matches, "target": target, "title": action.title, "due": due})
        if matches:
            previous_items = matches
    return plans, errors


def plan_preview(plans: list[dict]) -> str:
    lines = ["Proposed Trello changes:"]
    for plan in plans:
        names = ", ".join(f"‘{item['name']}’" for item in plan["items"][:3])
        if len(plan["items"]) > 3:
            names += f" and {len(plan['items']) - 3} more"
        if plan["action"] == "move":
            lines.append(f"• Move {names} to {plan['target']['name']}; due {plan['due']}")
        elif plan["action"] == "set_due":
            lines.append(f"• Set {names} due {plan['due']}")
        elif plan["action"] == "complete":
            lines.append(f"• Complete {names}")
        else:
            lines.append(f"• Create ‘{plan['title']}’ on {plan['items'][0]['card_name']}; due {plan['due']}")
    lines.append("\nReply ‘confirm’ to apply all changes, or ‘cancel’. ")
    return "\n".join(lines)


def update_checkitem(item: dict, due_date: str) -> None:
    trello_request(
        "PUT",
        f"/cards/{item['card_id']}/checkItem/{item['id']}",
        params={"due": f"{due_date}T23:59:00.000Z"},
    )


def complete_checkitem(item: dict) -> None:
    trello_request(
        "PUT",
        f"/cards/{item['card_id']}/checkItem/{item['id']}",
        params={"state": "complete"},
    )


def create_followup(reference_item: dict, title: str, due_date: str) -> None:
    trello_request(
        "POST",
        f"/checklists/{reference_item['checklist_id']}/checkItems",
        params={
            "name": title,
            "pos": "top",
            "due": f"{due_date}T23:59:00.000Z",
        },
    )


def execute_action_plans(plans: list[dict]) -> str:
    completed = 0
    failed = []
    for plan in plans:
        if plan["action"] == "move":
            for item in plan["items"]:
                try:
                    move_item(item, plan["target"], plan["due"])
                    completed += 1
                except Exception:
                    logger.exception("Batch move failed for %s", item["name"])
                    failed.append(item["name"])
        elif plan["action"] == "set_due":
            for item in plan["items"]:
                try:
                    update_checkitem(item, plan["due"])
                    completed += 1
                except Exception:
                    logger.exception("Batch due-date update failed for %s", item["name"])
                    failed.append(item["name"])
        elif plan["action"] == "complete":
            for item in plan["items"]:
                try:
                    complete_checkitem(item)
                    completed += 1
                except Exception:
                    logger.exception("Batch completion failed for %s", item["name"])
                    failed.append(item["name"])
        elif plan["action"] == "create":
            try:
                create_followup(plan["items"][0], plan["title"], plan["due"])
                completed += 1
            except Exception:
                logger.exception("Batch create failed for %s", plan["title"])
                failed.append(plan["title"])
    result = f"Applied {completed} Trello change{'s' if completed != 1 else ''}."
    if failed:
        result += f" {len(failed)} failed: " + ", ".join(failed[:5])
        if len(failed) > 5:
            result += f" and {len(failed) - 5} more"
        result += ". Check those items in Trello before retrying."
    return result


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
    if chat_id and requests_confirmation(text):
        pending = PENDING_ACTIONS.pop(chat_id, None)
        if not pending:
            return "There is no pending Trello change to confirm."
        if pending["kind"] == "batch":
            return execute_action_plans(pending["plans"])
        data = pending["data"]
        if pending["kind"] == "date":
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

    if looks_like_board_command(text):
        cards = pipeline_cards()
        batch = parse_board_actions(text, [card["name"] for card in cards])
        if not batch:
            return "I couldn’t parse those Trello instructions. Nothing was changed."
        repair_missing_action_dates(batch, text)
        plans, errors = build_action_plan(batch, all_pipeline_items(), cards)
        if errors:
            details = "\n".join(f"• {error}" for error in errors)
            preview = plan_preview(plans) + "\n\n" if plans else ""
            return preview + "I need clarification before I can safely apply the batch:\n" + details
        if chat_id is None:
            return plan_preview(plans)
        PENDING_ACTIONS[chat_id] = {"kind": "batch", "plans": plans}
        return plan_preview(plans)

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
        PENDING_ACTIONS[chat_id] = {"kind": "date", "data": {"item": item, "due": due}}
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
            PENDING_ACTIONS[chat_id] = {"kind": "date", "data": {"item": item, "due": due}}
            return f"That item is already on {target['name']}. I’ll set it due {due}. Reply ‘confirm’ to apply it."
        if chat_id is None:
            return "I found the item and destination, but I need Telegram confirmation before moving it."
        PENDING_ACTIONS[chat_id] = {
            "kind": "move",
            "data": {"item": item, "target": target, "due": due},
        }
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

        lower_text = text.lower()
        if looks_like_board_command(text) or any(
            phrase in lower_text
            for phrase in (
                "what do i need to work on",
                "what should i work on",
                "anything urgent",
                "what's outstanding",
                "whats outstanding",
            )
        ):
            send_telegram(chat_id, "Checking PIPELINE…")

        command_response = handle_trello_command(text, chat_id)
        if command_response:
            send_telegram(chat_id, command_response)
            return jsonify(ok=True)

        if looks_like_bot_output(text):
            send_telegram(
                chat_id,
                "That looks like text from one of my own Trello summaries, so I did not capture it as new tasks. "
                "Send only ‘confirm’ to approve a pending change.",
            )
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
