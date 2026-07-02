#!/usr/bin/env python3
"""Интерактивный Telegram-бот Клодика — фаза 2 из docs/telegram_bot.md.

Long-polling на stdlib, без зависимостей. Для запуска нужен только
TELEGRAM_BOT_TOKEN: chat_id бот привязывает сам по первому /start
(binding-файл в ~/.local/state/klodik/), чужие чаты игнорирует.

Команды: /start — привязка и справка; /digest — свежий дайджест с кнопками
решений по серой зоне; /status — состояние. Решения по кнопкам дописываются
в журнал откликов (--journal).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import telegram_digest as delivery

API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_SEC = 50
MAX_CALLBACK_DATA = 64  # лимит Telegram на callback_data

# Строка items[] по контракту digest_format.md
ITEM_RE = re.compile(
    r'-\s*\{id:\s*([\w-]+),\s*title:\s*"([^"]*)",\s*url:\s*"([^"]*)",'
    r'\s*fit:\s*(\d+),\s*zone:\s*(\w+)\}')

WELCOME = (
    "Клодик на связи. Чат привязан — сюда будут приходить дайджесты вакансий.\n\n"
    "Команды:\n"
    "/digest — свежий дайджест\n"
    "/status — состояние системы\n\n"
    "Под дайджестом — кнопки по вакансиям, где нужно твоё решение:\n"
    "✅ В работу — готовим пакет отклика (резюме под вакансию + письмо), "
    "в журнале появится статус «к отправке»\n"
    "❌ Пропустить — вакансия помечается «пропущена» и больше не показывается"
)


class BotError(Exception):
    """Текст обязан быть чистым: без токена и без URL запроса."""


# --- Bot API ----------------------------------------------------------------

def api_call(token, method, payload, timeout):
    request = urllib.request.Request(
        API_URL_TEMPLATE.format(token=token, method=method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            description = json.loads(error.read().decode("utf-8")).get("description", "")
        except (ValueError, OSError):
            description = ""
        raise BotError(f"Bot API HTTP {error.code} ({method}): {description}")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise BotError(f"сеть ({method}): {type(error).__name__}: {reason}")


# --- Привязка чата ----------------------------------------------------------

def load_binding():
    try:
        data = json.loads(delivery.BINDING_FILE.read_text(encoding="utf-8"))
        return data.get("chat_id")
    except (OSError, ValueError):
        return None


def save_binding(chat_id):
    delivery.BINDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = delivery.BINDING_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"chat_id": chat_id, "ts": int(time.time())}),
                   encoding="utf-8")
    os.replace(tmp, delivery.BINDING_FILE)


# --- Дайджест: items и клавиатура -------------------------------------------

def parse_items(text):
    """items[] из front-matter дайджеста; без front-matter — пустой список."""
    if not text.startswith("---\n"):
        return []
    end = text.find("\n---", 4)
    if end == -1:
        return []
    return [
        {"id": m[0], "title": m[1], "url": m[2], "fit": int(m[3]), "zone": m[4]}
        for m in ITEM_RE.findall(text[4:end])
    ]


def build_keyboard(items, decided=()):
    """Кнопки решений только по серой зоне; уже решённые исчезают."""
    rows = []
    for item in items:
        if item["zone"] != "grey" or item["id"] in decided:
            continue
        take, skip = f"w:{item['id']}", f"s:{item['id']}"
        if max(len(take.encode()), len(skip.encode())) > MAX_CALLBACK_DATA:
            continue  # контракт digest_format.md требует короткие ID
        # На кнопке — компания, а не внутренний ID: понятно, к какой вакансии
        company = item["title"].partition(" — ")[2] or item["title"]
        rows.append([
            {"text": f"✅ В работу — {company[:24]}", "callback_data": take},
            {"text": "❌ Пропустить", "callback_data": skip},
        ])
    return {"inline_keyboard": rows} if rows else None


def append_decision(journal_path, item, action):
    """Строка решения в журнал откликов (формат job_log.md)."""
    role, _, company = item["title"].partition(" — ")
    status = "к отправке" if action == "w" else "пропущена"
    line = (f"| {item['id']} | {time.strftime('%Y-%m-%d')} | {company or '—'} "
            f"| {role} | {item['fit']} | telegram | {status} | решение из бота |\n")
    with open(journal_path, "a", encoding="utf-8") as journal:
        journal.write(line)


# --- Обработчики ------------------------------------------------------------

def send_digest(ctx):
    digest_path = delivery.find_latest(ctx["digest_dir"]) if ctx["digest_dir"] else None
    if digest_path is None:
        api_call(ctx["token"], "sendMessage",
                 {"chat_id": ctx["chat_id"],
                  "text": "дайджестов пока нет — дождись прогона конвейера"},
                 ctx["timeout"])
        return
    text = digest_path.read_text(encoding="utf-8")
    _, body = delivery.split_front_matter(text)
    ctx["items"] = parse_items(text)
    ctx["decided"] = set()
    parts = delivery.split_message(delivery.md_to_html(body))
    config = {"token": ctx["token"], "chat_id": ctx["chat_id"],
              "timeout": ctx["timeout"], "retries": 3}
    for index, part in enumerate(parts):
        keyboard = build_keyboard(ctx["items"]) if index == len(parts) - 1 else None
        delivery.send_message(part, config, reply_markup=keyboard)


def status_text(ctx):
    lines = [f"чат привязан: {ctx['chat_id']}"]
    digest_path = delivery.find_latest(ctx["digest_dir"]) if ctx["digest_dir"] else None
    if digest_path is not None:
        age_h = (time.time() - digest_path.stat().st_mtime) / 3600
        lines.append(f"последний дайджест: {digest_path.name} ({age_h:.0f} ч назад)")
    else:
        lines.append("дайджестов пока нет")
    lines.append(f"журнал: {ctx['journal'] or 'не подключён'}")
    return "\n".join(lines)


def handle_message(ctx, message):
    chat = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if ctx["chat_id"] is None:
        # Самопривязка: первый /start фиксирует чат, дальше — только он
        if text.startswith("/start") and chat is not None:
            ctx["chat_id"] = chat
            save_binding(chat)
            api_call(ctx["token"], "sendMessage",
                     {"chat_id": chat, "text": WELCOME}, ctx["timeout"])
        return
    if chat != ctx["chat_id"]:
        return  # авторизация: бот публично доступен по имени, чужие чаты — игнор
    if text.startswith("/digest"):
        send_digest(ctx)
        return
    if text.startswith("/status"):
        reply = status_text(ctx)
    elif text.startswith("/start"):
        reply = WELCOME
    else:
        reply = "команды: /digest — свежий дайджест, /status — состояние"
    api_call(ctx["token"], "sendMessage",
             {"chat_id": ctx["chat_id"], "text": reply}, ctx["timeout"])


def handle_callback(ctx, callback):
    chat = callback.get("message", {}).get("chat", {}).get("id")
    if chat != ctx["chat_id"]:
        return
    action, _, item_id = callback.get("data", "").partition(":")
    item = next((i for i in ctx.get("items", []) if i["id"] == item_id), None)
    answer = {"callback_query_id": callback["id"]}
    if action in ("w", "s") and item is not None:
        if ctx["journal"]:
            append_decision(ctx["journal"], item, action)
        ctx.setdefault("decided", set()).add(item_id)
        answer["text"] = f"{item_id}: {'в работу' if action == 'w' else 'пропущена'}"
        keyboard = build_keyboard(ctx["items"], ctx["decided"])
        api_call(ctx["token"], "editMessageReplyMarkup", {
            "chat_id": chat,
            "message_id": callback["message"]["message_id"],
            "reply_markup": keyboard or {"inline_keyboard": []},
        }, ctx["timeout"])
    else:
        answer["text"] = "кнопка устарела — пришли /digest заново"
    api_call(ctx["token"], "answerCallbackQuery", answer, ctx["timeout"])


# --- Цикл -------------------------------------------------------------------

def poll_loop(ctx, once=False):
    offset = 0
    while True:
        try:
            response = api_call(ctx["token"], "getUpdates", {
                "offset": offset,
                "timeout": POLL_TIMEOUT_SEC,
                "allowed_updates": ["message", "callback_query"],
            }, POLL_TIMEOUT_SEC + ctx["timeout"])
        except BotError as error:
            print(f"getUpdates: {error}", file=sys.stderr)
            if once:
                return
            time.sleep(5)
            continue
        for update in response.get("result", []):
            offset = max(offset, update["update_id"] + 1)
            try:
                if "message" in update:
                    handle_message(ctx, update["message"])
                elif "callback_query" in update:
                    handle_callback(ctx, update["callback_query"])
            except BotError as error:
                print(f"обработка апдейта: {error}", file=sys.stderr)
        if once:
            return


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Интерактивный Telegram-бот (см. docs/telegram_bot.md, фаза 2)")
    parser.add_argument("--env-file", help="KEY=VALUE файл с TELEGRAM_BOT_TOKEN вне репозитория")
    parser.add_argument("--digest-dir", help="папка дайджестов в vault (для /digest)")
    parser.add_argument("--journal", help="файл журнала откликов (job_log.md) для записи решений")
    parser.add_argument("--once", action="store_true",
                        help="один цикл getUpdates и выход (отладка)")
    args = parser.parse_args(argv)

    file_values = delivery.load_env_file(args.env_file) if args.env_file else {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or file_values.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ошибка конфигурации: не задан TELEGRAM_BOT_TOKEN "
              "(окружение или --env-file)", file=sys.stderr)
        return 1

    try:
        me = api_call(token, "getMe", {}, 10).get("result", {})
    except BotError as error:
        print(f"токен не работает: {error}", file=sys.stderr)
        return 1

    ctx = {
        "token": token,
        "chat_id": load_binding(),
        "digest_dir": args.digest_dir,
        "journal": args.journal,
        "timeout": 10,
    }
    name = me.get("username", "?")
    if ctx["chat_id"] is None:
        print(f"бот @{name} запущен, чат не привязан: открой t.me/{name} и нажми /start")
    else:
        print(f"бот @{name} запущен, чат привязан: {ctx['chat_id']}")
    poll_loop(ctx, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
