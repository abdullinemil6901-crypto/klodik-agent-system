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
                          "url": "https://example.com/2", "fit": 60, "zone": "grey",
                          "source": "hh", "contact": ""})

    def test_item_with_source_and_contact(self):
        digest = ('---\nitems:\n  - {id: JV-0009, title: "Роль — Компания", '
                  'url: "https://t.me/chan/5", fit: 70, zone: work, '
                  'source: telegram, contact: "@author"}\n---\nтело')
        item = bot.parse_items(digest)[0]
        self.assertEqual(item["source"], "telegram")
        self.assertEqual(item["contact"], "@author")

    def test_no_front_matter(self):
        self.assertEqual(bot.parse_items("# просто текст"), [])


class TestCardKeyboard(unittest.TestCase):
    def test_card_has_apply_link_and_decision_buttons(self):
        item = bot.parse_items(DIGEST)[1]
        rows = bot.card_keyboard(item)["inline_keyboard"]
        self.assertEqual(rows[0][0]["url"], "https://example.com/2")
        self.assertEqual(rows[1][0]["callback_data"], "w:JV-0002")
        self.assertEqual(rows[1][1]["callback_data"], "s:JV-0002")
        for button in rows[1]:
            self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_telegram_card_links_to_author(self):
        item = {"id": "JV-0009", "title": "Роль — Компания", "url": "https://t.me/chan/5",
                "fit": 70, "zone": "work", "source": "telegram", "contact": "@author"}
        rows = bot.card_keyboard(item)["inline_keyboard"]
        self.assertEqual(rows[0][0]["text"], "Написать автору")
        self.assertEqual(rows[0][0]["url"], "https://t.me/author")

    def test_decided_card_keeps_only_apply_link(self):
        item = bot.parse_items(DIGEST)[1]
        rows = bot.card_keyboard(item, decided=True)["inline_keyboard"]
        self.assertEqual(len(rows), 1)
        self.assertIn("url", rows[0][0])


class TestSplitCards(unittest.TestCase):
    def test_summary_and_cards(self):
        body = ("# Вакансии\n\n4 свежие вакансии\n\n"
                "**[Роль А](https://example.com/1)**\nКомпания А · 85%\n\n"
                "**[Роль Б](https://example.com/2)**\nКомпания Б · 60%")
        summary, cards = bot.split_cards(body)
        self.assertIn("4 свежие вакансии", summary)
        self.assertNotIn("**[", summary)
        self.assertEqual(len(cards), 2)
        self.assertTrue(cards[0].startswith("**[Роль А]"))


class TestJournal(unittest.TestCase):
    def test_decision_row_appended(self):
        item = bot.parse_items(DIGEST)[1]
        with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as handle:
            bot.append_decision(handle.name, item, "w")
            line = Path(handle.name).read_text(encoding="utf-8")
        self.assertIn("| JV-0002 |", line)
        self.assertIn("| Компания Б | AI Automation | 60 | telegram | к отправке |", line)


def make_ctx(chat_id=42, journal=None, resume_dir=None, extra_chats=()):
    return {"token": "t", "chat_id": chat_id, "digest_dir": None,
            "journal": journal, "resume_dir": resume_dir, "timeout": 1,
            "extra_chats": set(extra_chats),
            "items": bot.parse_items(DIGEST), "decided": set()}


class TestResumeIntake(unittest.TestCase):
    def test_long_text_saved_as_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(resume_dir=tmp)
            calls = []
            with mock.patch.object(bot, "api_call",
                                   side_effect=lambda t, m, p, s: calls.append(p)):
                bot.handle_message(ctx, {"chat": {"id": 42},
                                         "text": "Опыт работы: " + "х" * 400})
            saved = Path(tmp) / "master_resume_raw.md"
            self.assertTrue(saved.is_file())
            self.assertIn("Опыт работы", saved.read_text(encoding="utf-8"))
            self.assertEqual(calls[0]["text"], bot.RESUME_SAVED)

    def test_short_text_gets_help_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(resume_dir=tmp)
            with mock.patch.object(bot, "api_call"):
                bot.handle_message(ctx, {"chat": {"id": 42}, "text": "привет"})
            self.assertFalse((Path(tmp) / "master_resume_raw.md").exists())


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

    def test_extra_chat_allowed_for_callbacks(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as handle:
            ctx = make_ctx(chat_id=42, journal=handle.name, extra_chats={77})
            calls = []
            with mock.patch.object(bot, "api_call",
                                   side_effect=lambda t, m, p, s: calls.append((m, p))):
                bot.handle_callback(ctx, {"id": "cb5", "data": "w:JV-0003",
                                          "message": {"chat": {"id": 77}, "message_id": 3}})
            journal_text = Path(handle.name).read_text(encoding="utf-8")
        self.assertIn("| JV-0003 |", journal_text)
        self.assertTrue(calls)

    def test_old_message_button_falls_back_to_journal(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as handle:
            ctx = make_ctx(journal=handle.name)
            ctx["items"] = []  # бот перезапущен, карточек в памяти нет
            calls = []
            with mock.patch.object(bot, "api_call",
                                   side_effect=lambda t, m, p, s: calls.append((m, p))):
                bot.handle_callback(ctx, {"id": "cb6", "data": "w:JV-0001",
                                          "message": {"chat": {"id": 42}, "message_id": 4}})
            journal_text = Path(handle.name).read_text(encoding="utf-8")
        self.assertIn("| JV-0001 |", journal_text)
        self.assertIn("к отправке", journal_text)
        self.assertIn("в работу", calls[-1][1]["text"])

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
        remaining = calls[0][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(remaining), 1)  # осталась только ссылка «Откликнуться»
        self.assertIn("url", remaining[0][0])
        self.assertIn("Компания В", calls[1][1]["text"])

    def test_interview_request_journaled(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as handle:
            ctx = make_ctx(journal=handle.name)
            calls = []
            with mock.patch.object(bot, "api_call",
                                   side_effect=lambda t, m, p, s: calls.append((m, p))):
                bot.handle_callback(ctx, {"id": "cb3", "data": "i:JV-0002",
                                          "message": {"chat": {"id": 42}, "message_id": 8}})
            journal_text = Path(handle.name).read_text(encoding="utf-8")
        self.assertIn("| JV-0002 |", journal_text)
        self.assertIn("интервью", journal_text)
        self.assertIn("интервью", calls[-1][1]["text"])

    def test_interview_without_card_context_still_journaled(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as handle:
            ctx = make_ctx(journal=handle.name)
            ctx["items"] = []  # бот перезапущен, карточек в памяти нет
            calls = []
            with mock.patch.object(bot, "api_call",
                                   side_effect=lambda t, m, p, s: calls.append((m, p))):
                bot.handle_callback(ctx, {"id": "cb4", "data": "i:JV-0009",
                                          "message": {"chat": {"id": 42}, "message_id": 9}})
            journal_text = Path(handle.name).read_text(encoding="utf-8")
        self.assertIn("| JV-0009 |", journal_text)
        self.assertEqual([m for m, _ in calls], ["answerCallbackQuery"])

    def test_stale_button_answered_gracefully(self):
        ctx = make_ctx()
        calls = []
        with mock.patch.object(bot, "api_call",
                               side_effect=lambda t, m, p, s: calls.append((m, p))):
            bot.handle_callback(ctx, {"id": "cb2", "data": "w:JV-9999",
                                      "message": {"chat": {"id": 42}, "message_id": 7}})
        self.assertEqual([m for m, _ in calls], ["answerCallbackQuery"])


class TestPipeline(unittest.TestCase):
    JOURNAL = """# журнал
| ID | Дата | Компания | Роль | fit% | Канал | Статус | Следующий шаг |
|---|---|---|---|---|---|---|---|
| JV-0001 | 2026-07-02 | Интегратор | MolecularMeal | 85 | hh | пакет готов | — |
| JV-0002 | 2026-07-02 | Инженер | Ayo | 75 | hh | пакет готов | — |
| JV-0001 | 2026-07-03 | — | — | — | telegram | отправлена | follow-up |
"""

    def test_last_status_wins_and_grouped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(self.JOURNAL)
        text = bot.pipeline_text(make_ctx(journal=handle.name))
        self.assertIn("📤 Отправлены", text)
        self.assertIn("🛠 В работе", text)
        # JV-0001 после «отправлена» не должен висеть в «в работе»
        in_work = text.split("📤")[0]
        self.assertNotIn("JV-0001", in_work)

    def test_sent_action_journaled(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as handle:
            ctx = make_ctx(journal=handle.name)
            calls = []
            with mock.patch.object(bot, "api_call",
                                   side_effect=lambda t, m, p, s: calls.append((m, p))):
                bot.handle_callback(ctx, {"id": "cb7", "data": "d:JV-0002",
                                          "message": {"chat": {"id": 42}, "message_id": 5}})
            journal_text = Path(handle.name).read_text(encoding="utf-8")
        self.assertIn("отправлена", journal_text)
        self.assertIn("follow-up", calls[-1][1]["text"])


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
