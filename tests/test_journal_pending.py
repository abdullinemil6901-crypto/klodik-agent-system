"""Тесты сканера журнала откликов (scripts/journal_pending.py)."""

import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import journal_pending as jp

TODAY = datetime.date(2026, 7, 6)

JOURNAL = """\
# job_log — журнал откликов

| ID | Дата | Компания | Роль | fit% | Канал | Статус | Следующий шаг |
|---|---|---|---|---|---|---|---|
| JV-0001 | `[ГГГГ-ММ-ДД]` | `[...]` | `[...]` | `[NN]` | `[hh / ...]` | `[найдена / ...]` | `[...]` |
| JV-0002 | 2026-07-02 | Компания А | Роль А | 82 | telegram | к отправке | собрать пакет отклика |
| JV-0003 | 2026-06-30 | Компания Б | Роль Б | 77 | hh | отправлена | — |
| JV-0004 | 2026-07-05 | Компания В | Роль В | 71 | hh | отправлена | — |
| JV-0005 | 2026-07-03 | Компания Г | Роль Г | 90 | telegram | интервью | подготовить разбор интервью |
| JV-0006 | 2026-07-01 | Компания Д | Роль Д | 55 | hh | пропущена | — |
| JV-0007 | 2026-06-28 | Компания Е | Роль Е | 80 | hh | follow-up | напомнить |
"""


class ParseJournalTest(unittest.TestCase):
    def test_skips_header_separator_and_placeholder_rows(self):
        rows = jp.parse_journal(JOURNAL)
        self.assertEqual(
            {r["id"] for r in rows},
            {"JV-0002", "JV-0003", "JV-0004", "JV-0005", "JV-0006", "JV-0007"},
        )

    def test_last_row_of_same_id_wins(self):
        text = JOURNAL + (
            "| JV-0002 | 2026-07-04 | Компания А | Роль А | 82 "
            "| telegram | к отправке | — |\n")
        rows = {r["id"]: r for r in jp.parse_journal(text)}
        self.assertEqual(rows["JV-0002"]["step"], "—")
        self.assertEqual(rows["JV-0002"]["date"], datetime.date(2026, 7, 4))


class PendingTasksTest(unittest.TestCase):
    def setUp(self):
        self.tasks = jp.pending_tasks(jp.parse_journal(JOURNAL), TODAY)

    def test_package_queue_from_decision_step(self):
        self.assertEqual([r["id"] for r in self.tasks["packages"]], ["JV-0002"])

    def test_interview_queue_from_step_prefix(self):
        self.assertEqual([r["id"] for r in self.tasks["interviews"]], ["JV-0005"])

    def test_followup_by_age_and_explicit_status(self):
        ids = {r["id"] for r in self.tasks["followups"]}
        # JV-0003 — отправлена 6 дней назад; JV-0007 — статус follow-up;
        # JV-0004 — отправлена вчера, ещё рано.
        self.assertEqual(ids, {"JV-0003", "JV-0007"})

    def test_followup_threshold_configurable(self):
        tasks = jp.pending_tasks(jp.parse_journal(JOURNAL), TODAY, followup_days=1)
        self.assertIn("JV-0004", {r["id"] for r in tasks["followups"]})


class FormatReportTest(unittest.TestCase):
    def test_report_lists_all_queues(self):
        tasks = jp.pending_tasks(jp.parse_journal(JOURNAL), TODAY)
        report = jp.format_report(tasks, TODAY)
        self.assertIn("Пакеты отклика (1)", report)
        self.assertIn("Разборы к интервью (1)", report)
        self.assertIn("Follow-up (2)", report)
        self.assertIn("6 дн. без ответа", report)

    def test_empty_journal_reports_no_tasks(self):
        tasks = jp.pending_tasks([], TODAY)
        self.assertEqual(jp.format_report(tasks, TODAY),
                         "Необработанных задач в журнале нет.")


class CliTest(unittest.TestCase):
    def test_missing_journal_exits_1(self):
        self.assertEqual(jp.main(["/nonexistent/job_log.md"]), 1)


if __name__ == "__main__":
    unittest.main()
