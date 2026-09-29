from unittest.mock import patch

import app as bot


def test_trello_item_name_without_due_date():
    assert bot.trello_item_name(bot.Task(title="Buy coffee beans")) == "Buy coffee beans"


def test_trello_item_name_with_due_date():
    task = bot.Task(title="Send Trey MISO numbers", due_date="2026-09-21")
    assert bot.trello_item_name(task) == "Send Trey MISO numbers — due 2026-09-21"


def test_capture_preserves_original_when_ai_fails():
    with patch.object(bot, "parse_tasks", side_effect=RuntimeError("API unavailable")):
        with patch.object(bot, "add_checkitem_to_trello") as add:
            tasks = bot.capture("Call Smithfield")
    assert [task.title for task in tasks] == ["Call Smithfield"]
    add.assert_called_once()


def test_capture_creates_every_parsed_task():
    parsed = [bot.Task(title="Task one"), bot.Task(title="Task two")]
    with patch.object(bot, "parse_tasks", return_value=parsed):
        with patch.object(bot, "add_checkitem_to_trello") as add:
            tasks = bot.capture("two things")
    assert tasks == parsed
    assert add.call_count == 2


def test_health():
    response = bot.app.test_client().get("/")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_browser_sanity_checks():
    client = bot.app.test_client()
    assert client.get("/capture").status_code == 200
    assert client.get("/telegram-webhook").status_code == 200


def test_matching_tolerates_synonyms_typos_and_accents():
    items = [
        {
            "id": "1",
            "name": "Chase up Ricky for bills",
            "card_name": "Personal",
            "state": "incomplete",
        },
        {
            "id": "2",
            "name": "pre Catherine",
            "card_name": "Sales",
            "state": "incomplete",
        },
    ]
    ricky, error = bot.resolve_items("ask Ricky for bills", "single", items)
    assert error is None
    assert ricky[0]["id"] == "1"
    catherine, error = bot.resolve_items("prep Catherine", "single", items)
    assert error is None
    assert catherine[0]["id"] == "2"
    assert bot.normalized_text("Transcendía") == "transcendia"


def test_short_account_name_targets_all_items_on_card():
    cards = [{"id": "card-1", "name": "Transcendia", "closed": False}]
    items = [
        {"id": "1", "name": "Email Shelley", "card_name": "Transcendia", "card_id": "card-1", "state": "incomplete"},
        {"id": "2", "name": "Research forced curtailment", "card_name": "Transcendia", "card_id": "card-1", "state": "incomplete"},
    ]
    batch = bot.BoardActionBatch(actions=[bot.BoardAction(action="set_due", query="Transcendía", due_date="2026-10-29")])
    plans, errors = bot.build_action_plan(batch, items, cards)
    assert errors == []
    assert len(plans[0]["items"]) == 2


def test_bot_summary_is_never_treated_as_fresh_capture():
    summary = """Proposed Trello changes:
• Set ‘Book flights’ due 2026-10-29
• Complete ‘Call Ricky’

Reply ‘confirm’ to apply all changes, or ‘cancel’."""
    assert bot.looks_like_bot_output(summary)
    assert not bot.requests_confirmation(summary)


def test_pasted_preview_with_confirm_is_confirmation():
    text = """Proposed Trello changes:
• Set ‘Book flights’ due 2026-10-29

Confirm"""
    assert bot.requests_confirmation(text)
