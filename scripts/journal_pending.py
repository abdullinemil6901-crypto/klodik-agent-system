#!/usr/bin/env python3
"""Задачи из журнала откликов — детерминированный первый шаг прогонов.

Читает журнал (контракт — vault-template/.../job_log.md) и выводит, что ждёт
обработки: пакеты отклика по решениям «В работу», разборы к интервью,
follow-up по откликам без ответа. Журнал append-only, поэтому текущее
состояние вакансии — её последняя строка. Список выполняет LLM-слой рутины
(см. routines/), скрипт сам ничего не меняет и работает без токенов.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

FOLLOWUP_AFTER_DAYS = 4  # docs/pipelines.md §2: отклик без ответа 4+ дней

PACKAGE_STEP = "собрать пакет отклика"
INTERVIEW_STEP_PREFIX = "подготовить разбор"


def parse_journal(text):
    """Строки таблицы журнала → текущее состояние по каждому ID.

    Заголовок, разделитель и плейсхолдеры шаблона отбрасываются по признаку
    «дата не парсится». Повторные строки одного ID перекрывают ранние.
    """
    rows = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in line.strip("|").split("|")]
        if len(cells) != 8:
            continue
        try:
            date = datetime.date.fromisoformat(cells[1])
        except ValueError:
            continue
        rows[cells[0]] = {
            "id": cells[0], "date": date, "company": cells[2],
            "role": cells[3], "fit": cells[4], "channel": cells[5],
            "status": cells[6], "step": cells[7],
        }
    return list(rows.values())


def pending_tasks(rows, today, followup_days=FOLLOWUP_AFTER_DAYS):
    """Три очереди работы: пакеты, разборы, follow-up."""
    packages = [r for r in rows if r["step"] == PACKAGE_STEP]
    interviews = [r for r in rows if r["step"].startswith(INTERVIEW_STEP_PREFIX)]
    followups = [
        r for r in rows
        if r["status"] == "follow-up"
        or (r["status"] == "отправлена"
            and (today - r["date"]).days >= followup_days)
    ]
    return {"packages": packages, "interviews": interviews, "followups": followups}


def format_report(tasks, today):
    lines = [f"# Задачи из журнала — {today.isoformat()}", ""]
    sections = [
        ("Пакеты отклика", tasks["packages"],
         lambda r: f"- {r['id']} — {r['role']}, {r['company']} (fit {r['fit']}, решение {r['date']})"),
        ("Разборы к интервью", tasks["interviews"],
         lambda r: f"- {r['id']} — {r['role']}, {r['company']}"),
        ("Follow-up", tasks["followups"],
         lambda r: f"- {r['id']} — {r['company']}, отправлена {r['date']}, "
                   f"{(today - r['date']).days} дн. без ответа"),
    ]
    total = 0
    for title, rows, render in sections:
        if not rows:
            continue
        total += len(rows)
        lines.append(f"## {title} ({len(rows)})")
        lines.extend(render(r) for r in sorted(rows, key=lambda r: r["date"]))
        lines.append("")
    if total == 0:
        return "Необработанных задач в журнале нет."
    return "\n".join(lines).rstrip()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Необработанные задачи журнала откликов (первый шаг рутин)")
    parser.add_argument("journal", help="путь к журналу откликов (job_log.md)")
    parser.add_argument("--days", type=int, default=FOLLOWUP_AFTER_DAYS,
                        help="дней без ответа до follow-up (по умолчанию %(default)s)")
    parser.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо системной (тесты/отладка)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="машиночитаемый вывод вместо markdown")
    args = parser.parse_args(argv)

    path = Path(args.journal)
    if not path.is_file():
        print(f"журнал не найден: {path}", file=sys.stderr)
        return 1
    today = (datetime.date.fromisoformat(args.today) if args.today
             else datetime.date.today())
    tasks = pending_tasks(parse_journal(path.read_text(encoding="utf-8")),
                          today, followup_days=args.days)
    if args.as_json:
        print(json.dumps(tasks, ensure_ascii=False, default=str, indent=2))
    else:
        print(format_report(tasks, today))
    return 0


if __name__ == "__main__":
    sys.exit(main())
