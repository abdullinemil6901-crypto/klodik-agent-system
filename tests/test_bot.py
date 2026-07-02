"""Тесты интерактивного бота: парсер items, клавиатура, авторизация, решения.

Запуск: python3 -m unittest discover tests
Сеть замокана — api_call подменяется, реальный токен не нужен.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import telegram_bot as bot
import telegram_digest as delivery

DIGEST = """---
run_id: 2026-07-02-0900
stats: найдено 20 · порог 1 · серая зона 2
items:
  - {id: JV-0001, title: "Интегратор агентов — Компания А", url: "https://example.com/1", fit: 85, zone: work}
  - {id: JV-0002, title: "AI Automation — Компания Б", url: "https://example.com/2", fit: 60, zone: grey}
  - {id: JV-0003, title: "n8n-разработчик — Компания В", url: "https://example.com/3", fit: 55, zone: grey}
---
# Вакансии — 2026-07-02

- JV-0001 · [Интегратор агентов — Компания А](https://example.com/1) · fit **85%**
"""


class TestParseItems(unittest.TestCase):
    def test_items_parsed(self):
        items = bot.parse_items(DIGEST)
        self.assertEqual([i["id"] for i in items], ["JV-0001", "JV-0002", "JV-0003"])
        self.assertEqual(items[1],
                         {"id": "JV-0002", "title": "AI Automation — Компания Б",
                          "url": "https://example.com/2", "fit": 60, "zone": "grey"})

    def test_no_front_matter(self):
        self.assertEqual(bot.parse_items("# просто текст"), [])


class TestKeyboard(unittest.TestCase):
    def test_only_grey_zone(self):
        keyboard = bot.build_keyboard(bot.parse_items(DIGEST))
        rows = keyboard["inline_keyboard"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0]["callback_data"], "w:JV-0002")
        self.assertEqual(rows[0][1]["callback_data"], "s:JV-0002")
        for row in rows:
            for button in row:
                self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_decided_removed(self):
        keyboard = bot.build_keyboard(bot.parse_items(DIGEST), decided={"JV-0002"})
        self.assertEqual(len(keyboard["inline_keyboard"]), 1)

    def test_all_decided_no_keyboard(self):
        keyboard = bot.build_keyboard(bot.parse_items(DIGEST),
                                      decided={"JV-0002", "JV-0003"})
        self.assertIsNone(keyboard)


class TestJournal(unittest.TestCase):
    def test_decision_row_appended(self):
        item = bot.parse_items(DIGEST)[1]
        with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as handle:
            bot.append_decision(handle.name, item, "w")
            line = Path(handle.name).read_text(encoding="utf-8")
        self.assertIn("| JV-0002 |", line)
        self.assertIn("| Компания Б | AI Automation | 60 | telegram | к отправке |", line)


def make_ctx(chat_id=42, journal=None):
    return {"token": "t", "chat_id": chat_id, "digest_dir": None,
            "journal": journal, "timeout": 1,
            "items": bot.parse_items(DIGEST), "decided": set()}


class TestAuthorization(unittest.TestCase):
    def test_foreign_chat_ignored(self):
        ctx = make_ctx(chat_id=42)
        with mock.patch.object(bot, "api_call") as api:
            bot.handle_message(ctx, {"chat": {"id": 999}, "text": "/digest"})
            bot.handle_callback(ctx, {"id": "cb", "data": "w:JV-0002",
                                      "message": {"chat": {"id": 999}, "message_id": 1}})
        api.assert_not_called()

    def test_first_start_binds_chat(self):
        ctx = make_ctx(chat_id=None)
        with mock.patch.object(bot, "api_call") as api, \
                mock.patch.object(bot, "save_binding") as save:
            bot.handle_message(ctx, {"chat": {"id": 42}, "text": "/start"})
        self.assertEqual(ctx["chat_id"], 42)
        save.assert_called_once_with(42)
        api.assert_called_once()

    def test_unbound_ignores_non_start(self):
        ctx = make_ctx(chat_id=None)
        with mock.patch.object(bot, "api_call") as api:
            bot.handle_message(ctx, {"chat": {"id": 42}, "text": "привет"})
        self.assertIsNone(ctx["chat_id"])
        api.assert_not_called()


class TestCallback(unittest.TestCase):
    def test_decision_writes_journal_and_updates_markup(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as handle:
            ctx = make_ctx(journal=handle.name)
            calls = []
            with mock.patch.object(bot, "api_call",
                                   side_effect=lambda t, m, p, s: calls.append((m, p))):
                bot.handle_callback(ctx, {"id": "cb1", "data": "s:JV-0003",
                                          "message": {"chat": {"id": 42}, "message_id": 7}})
            journal_text = Path(handle.name).read_text(encoding="utf-8")
        self.assertIn("| JV-0003 |", journal_text)
        self.assertIn("пропущена", journal_text)
        methods = [m for m, _ in calls]
        self.assertEqual(methods, ["editMessageReplyMarkup", "answerCallbackQuery"])
        edit_payload = calls[0][1]
        remaining = edit_payload["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(remaining), 1)  # осталась только JV-0002
        self.assertIn("JV-0003", calls[1][1]["text"])

    def test_stale_button_answered_gracefully(self):
        ctx = make_ctx()
        calls = []
        with mock.patch.object(bot, "api_call",
                               side_effect=lambda t, m, p, s: calls.append((m, p))):
            bot.handle_callback(ctx, {"id": "cb2", "data": "w:JV-9999",
                                      "message": {"chat": {"id": 42}, "message_id": 7}})
        self.assertEqual([m for m, _ in calls], ["answerCallbackQuery"])


class TestChatIdFallback(unittest.TestCase):
    def test_digest_config_uses_binding_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            binding = Path(tmp) / "bot_binding.json"
            binding.write_text('{"chat_id": 4242}', encoding="utf-8")
            env = {"TELEGRAM_BOT_TOKEN": "t"}
            with mock.patch.object(delivery, "BINDING_FILE", binding), \
                    mock.patch.object(delivery.os, "environ", env):
                config = delivery.resolve_config(None)
        self.assertEqual(config["chat_id"], 4242)


if __name__ == "__main__":
    unittest.main()
