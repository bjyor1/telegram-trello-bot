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
