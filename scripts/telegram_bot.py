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
MAX_FLOOD_WAITS = 5  # предохранитель от бесконечного цикла по HTTP 429

# ID вакансии из callback_data: что не совпало — в журнал не пишется
ITEM_ID_RE = re.compile(r"[\w-]+")

# Строка items[] по контракту digest_format.md
ITEM_RE = re.compile(
    r'-\s*\{id:\s*([\w-]+),\s*title:\s*"([^"]*)",\s*url:\s*"([^"]*)",'
    r'\s*fit:\s*(\d+),\s*zone:\s*(\w+)\}')

WELCOME = (
    "Клодик на связи. Чат привязан — сюда будут приходить карточки вакансий.\n\n"
    "Как это работает:\n"
    "🔗 Откликнуться — открыть вакансию на площадке\n"
    "✅ В работу — готовлю пакет отклика: резюме, пересобранное под эту вакансию, "
    "и письмо; пришлю сюда\n"
    "❌ Скрыть — вакансия больше не покажется\n\n"
    "Чтобы пакеты собирались из твоих реальных фактов — пришли сюда своё резюме "
    "(файлом или просто текстом), я сохраню его в память системы.\n\n"
    "Команды: /digest — свежие вакансии, /status — состояние."
)

RESUME_SAVED = (
    "Резюме получил и сохранил в память. Теперь совпадения считаются по твоим "
    "реальным фактам, а по кнопке «В работу» соберу резюме под конкретную "
    "вакансию и письмо. Выдуманного опыта не будет — только переупаковка твоего."
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
    flood_waits = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
            except (ValueError, OSError):
                body = {}
            if error.code == 429 and flood_waits < MAX_FLOOD_WAITS:
                # flood-wait: дайджест из многих карточек не падает на середине
                flood_waits += 1
                time.sleep(body.get("parameters", {}).get("retry_after", 5))
                continue
            raise BotError(
                f"Bot API HTTP {error.code} ({method}): {body.get('description', '')}")
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
    os.chmod(tmp, 0o600)
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


def append_decision(journal_path, item, action):
    """Строка решения в журнал откликов (формат job_log.md)."""
    role, _, company = item["title"].partition(" — ")
    status = {"w": "к отправке", "s": "пропущена", "i": "интервью"}[action]
    step = ("собрать пакет отклика" if action == "w"
            else "подготовить разбор интервью" if action == "i" else "—")
    line = (f"| {item['id']} | {time.strftime('%Y-%m-%d')} | {company or '—'} "
            f"| {role} | {item['fit']} | telegram | {status} | {step} |\n")
    with open(journal_path, "a", encoding="utf-8") as journal:
        journal.write(line)


# --- Обработчики ------------------------------------------------------------

def card_keyboard(item, decided=False):
    """Кнопки одной карточки: отклик — ссылкой, решение — колбэками."""
    rows = [[{"text": "🔗 Откликнуться", "url": item["url"]}]]
    if not decided:
        rows.append([
            {"text": "✅ В работу", "callback_data": f"w:{item['id']}"},
            {"text": "❌ Скрыть", "callback_data": f"s:{item['id']}"},
        ])
    return {"inline_keyboard": rows}


def split_cards(body):
    """Тело дайджеста → (сводка, блоки-карточки). Карточка начинается с **[Роль](url)**."""
    blocks = re.split(r"\n(?=\*\*\[)", body)
    summary = blocks[0].strip()
    return summary, [b.strip() for b in blocks[1:] if b.strip()]


def send_digest(ctx, chat_id=None):
    """Стандарт рынка: одна вакансия = одна карточка = отдельное сообщение."""
    chat_id = chat_id or ctx["chat_id"]
    digest_path = delivery.find_latest(ctx["digest_dir"]) if ctx["digest_dir"] else None
    if digest_path is None:
        api_call(ctx["token"], "sendMessage",
                 {"chat_id": chat_id,
                  "text": "свежих вакансий пока нет — пришлю, как появятся"},
                 ctx["timeout"])
        return
    text = digest_path.read_text(encoding="utf-8")
    _, body = delivery.split_front_matter(text)
    # Карточки хранятся per-chat: /digest из двух чатов не перетирает контекст
    items = parse_items(text)
    ctx.setdefault("items_by_chat", {})[chat_id] = items
    ctx["decided"] = set()
    summary, cards = split_cards(body)

    def send(html, keyboard=None):
        payload = {"chat_id": chat_id, "text": html, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        api_call(ctx["token"], "sendMessage", payload, ctx["timeout"])

    send(delivery.md_to_html(summary))
    for card in cards:
        item = next((i for i in items if f"]({i['url']})" in card), None)
        send(delivery.md_to_html(card),
             card_keyboard(item) if item is not None else None)


def status_text(ctx):
    lines = [f"чат привязан: {ctx['chat_id']}"]
    digest_path = delivery.find_latest(ctx["digest_dir"]) if ctx["digest_dir"] else None
    if digest_path is not None:
        age_h = (time.time() - digest_path.stat().st_mtime) / 3600
        lines.append(f"последние вакансии: {age_h:.0f} ч назад")
    else:
        lines.append("вакансий пока не приходило")
    if ctx.get("resume_dir") and any(Path(ctx["resume_dir"]).glob("*")):
        lines.append("резюме: в памяти, совпадения считаются по нему")
    else:
        lines.append("резюме: нет — пришли файлом или текстом, без него пакеты не собрать")
    lines.append(f"журнал откликов: {ctx['journal'] or 'не подключён'}")
    return "\n".join(lines)


def save_resume_text(ctx, text):
    target = Path(ctx["resume_dir"]) / "master_resume_raw.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    return target


def download_document(ctx, document):
    """Файл резюме из Telegram → папка резюме в vault."""
    info = api_call(ctx["token"], "getFile",
                    {"file_id": document["file_id"]}, ctx["timeout"])
    file_path = info.get("result", {}).get("file_path")
    if not file_path:
        raise BotError("getFile: ответ без file_path")
    url = f"https://api.telegram.org/file/bot{ctx['token']}/{file_path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise BotError(f"скачивание файла: {type(error).__name__}")
    # Имени из Telegram не доверяем: только базовое имя, без путей и скрытых файлов
    raw_name = document.get("file_name") or Path(file_path).name
    name = Path(raw_name).name.lstrip(".") or "resume_upload"
    resume_dir = Path(ctx["resume_dir"]).resolve()
    target = (resume_dir / name).resolve()
    if target.parent != resume_dir:
        raise BotError(f"недопустимое имя файла: {raw_name!r}")
    resume_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def allowed_chats(ctx):
    """Привязанный чат + доверенные из TELEGRAM_CHAT_IDS (несколько аккаунтов)."""
    allowed = set(ctx.get("extra_chats", ()))
    if ctx["chat_id"] is not None:
        allowed.add(ctx["chat_id"])
    return allowed


def bind_allowed(ctx, chat, text):
    """Подтверждение владельца при привязке.

    TELEGRAM_OWNER_ID — привязка только из этого чата; TELEGRAM_BIND_SECRET —
    только по «/start <секрет>» (deep-link t.me/<бот>?start=<секрет>). Ничего
    не задано — first-come по первому /start, как раньше.
    """
    if ctx.get("owner_id") is not None and chat != ctx["owner_id"]:
        return False
    secret = ctx.get("bind_secret")
    if secret:
        parts = text.split(maxsplit=1)
        return len(parts) == 2 and parts[1].strip() == secret
    return True


def handle_message(ctx, message):
    chat = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if ctx["chat_id"] is None:
        # Самопривязка: первый прошедший проверку владельца /start фиксирует чат
        if text.startswith("/start") and chat is not None and bind_allowed(ctx, chat, text):
            ctx["chat_id"] = chat
            save_binding(chat)
            api_call(ctx["token"], "sendMessage",
                     {"chat_id": chat, "text": WELCOME}, ctx["timeout"])
        return
    if chat not in allowed_chats(ctx):
        return  # авторизация: бот публично доступен по имени, чужие чаты — игнор
    if text.startswith("/digest"):
        send_digest(ctx, chat_id=chat)
        return
    if text.startswith("/status"):
        reply = status_text(ctx)
    elif text.startswith("/start"):
        reply = WELCOME
    elif message.get("document") and ctx.get("resume_dir"):
        download_document(ctx, message["document"])
        reply = RESUME_SAVED
    elif len(text) >= 400 and not text.startswith("/") and ctx.get("resume_dir"):
        # Длинный текст без команды — это резюме, присланное сообщением
        save_resume_text(ctx, text)
        reply = RESUME_SAVED
    else:
        reply = ("Пришли резюме файлом или текстом — сохраню в память.\n"
                 "Команды: /digest — свежие вакансии, /status — состояние.")
    api_call(ctx["token"], "sendMessage",
             {"chat_id": chat, "text": reply}, ctx["timeout"])


def handle_callback(ctx, callback):
    chat = callback.get("message", {}).get("chat", {}).get("id")
    if chat not in allowed_chats(ctx):
        return
    action, _, item_id = callback.get("data", "").partition(":")
    if not ITEM_ID_RE.fullmatch(item_id):
        item_id = ""  # мусор из callback_data не попадает в журнал
    items = ctx.get("items_by_chat", {}).get(chat, [])
    item = next((i for i in items if i["id"] == item_id), None)
    answer = {"callback_query_id": callback["id"]}
    if action in ("w", "s", "i") and item is not None:
        if ctx["journal"]:
            append_decision(ctx["journal"], item, action)
        ctx.setdefault("decided", set()).add(item_id)
        company = item["title"].partition(" — ")[2] or item["title"]
        replies = {
            "w": f"{company}: беру в работу — соберу резюме под вакансию и письмо, пришлю сюда",
            "s": f"{company}: скрыта, больше не покажу",
            "i": f"{company}: готовлю разбор к интервью — компания, вероятные вопросы, что спросить самому",
        }
        answer["text"] = replies[action]
        # Кнопки решения убираются с карточки, ссылка на отклик остаётся
        api_call(ctx["token"], "editMessageReplyMarkup", {
            "chat_id": chat,
            "message_id": callback["message"]["message_id"],
            "reply_markup": card_keyboard(item, decided=True),
        }, ctx["timeout"])
    elif action in ("w", "s", "i") and item_id and ctx["journal"]:
        # Кнопка со старого сообщения: карточки нет в памяти бота — журналим по ID
        status = {"w": "к отправке", "s": "пропущена", "i": "интервью"}[action]
        step = {"w": "собрать пакет отклика", "s": "—",
                "i": "подготовить разбор"}[action]
        line = (f"| {item_id} | {time.strftime('%Y-%m-%d')} | — | — | — "
                f"| telegram | {status} | {step} |\n")
        with open(ctx["journal"], "a", encoding="utf-8") as journal:
            journal.write(line)
        answer["text"] = {
            "w": "беру в работу — пакет отклика придёт сюда",
            "s": "скрыл, больше не покажу",
            "i": "готовлю разбор к интервью — пришлю сюда",
        }[action]
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
    parser.add_argument("--resume-dir", help="папка vault для присланного резюме")
    parser.add_argument("--once", action="store_true",
                        help="один цикл getUpdates и выход (отладка)")
    args = parser.parse_args(argv)

    file_values = delivery.load_env_file(args.env_file) if args.env_file else {}

    def get(key, default=None):
        return os.environ.get(key) or file_values.get(key) or default

    token = get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ошибка конфигурации: не задан TELEGRAM_BOT_TOKEN "
              "(окружение или --env-file)", file=sys.stderr)
        return 1

    try:
        me = api_call(token, "getMe", {}, 10).get("result", {})
    except BotError as error:
        print(f"токен не работает: {error}", file=sys.stderr)
        return 1

    raw_ids = get("TELEGRAM_CHAT_IDS", "")
    extra_chats = {int(part) for part in raw_ids.split(",") if part.strip().isdigit()}
    owner_raw = get("TELEGRAM_OWNER_ID", "")

    ctx = {
        "token": token,
        "chat_id": load_binding(),
        "extra_chats": extra_chats,
        "owner_id": int(owner_raw) if owner_raw.lstrip("-").isdigit() else None,
        "bind_secret": get("TELEGRAM_BIND_SECRET"),
        "digest_dir": args.digest_dir,
        "journal": args.journal,
        "resume_dir": args.resume_dir,
        "timeout": 10,
    }
    name = me.get("username", "?")
    if ctx["chat_id"] is None:
        start = "/start <TELEGRAM_BIND_SECRET>" if ctx["bind_secret"] else "/start"
        print(f"бот @{name} запущен, чат не привязан: открой t.me/{name} и отправь {start}")
    else:
        print(f"бот @{name} запущен, чат привязан: {ctx['chat_id']}")
    poll_loop(ctx, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
