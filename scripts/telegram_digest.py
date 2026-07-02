#!/usr/bin/env python3
"""Push-доставка дайджеста из vault в Telegram.

Последний шаг cron-прогона конвейера: читает markdown-дайджест, конвертирует
в Telegram-HTML и отправляет sendMessage. Только stdlib, без зависимостей.
Спека и контракт дайджеста: docs/telegram_bot.md.

Коды выхода: 0 — доставлено или уже было доставлено; 1 — ошибка конфигурации;
2 — доставка не удалась; 3 — дайджест не найден или пуст.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LEN = 4096
STATE_FILE = Path.home() / ".local" / "state" / "klodik" / "sent_digests.json"
BINDING_FILE = Path.home() / ".local" / "state" / "klodik" / "bot_binding.json"
FAILURE_LOG_NAME = "delivery_failures.md"

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_DELIVERY = 2
EXIT_NO_DIGEST = 3

DEFAULT_TIMEOUT_SEC = 10
DEFAULT_RETRIES = 3
MAX_FLOOD_WAITS = 5  # предохранитель от бесконечного цикла по HTTP 429


class DeliveryError(Exception):
    """Ошибка доставки. Текст обязан быть чистым: без токена и без URL запроса."""


# --- Конфигурация ---------------------------------------------------------

def load_env_file(path):
    """Наивный парсер KEY=VALUE: пустые строки и # пропускаются."""
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def resolve_config(env_file):
    """Окружение процесса — основной путь; env-файл добирает недостающее."""
    file_values = {}
    if env_file:
        if not Path(env_file).is_file():
            raise SystemExit(_config_error(f"env-файл не найден: {env_file}"))
        file_values = load_env_file(env_file)

    def get(key, default=None):
        return os.environ.get(key) or file_values.get(key) or default

    token = get("TELEGRAM_BOT_TOKEN")
    chat_id = get("TELEGRAM_CHAT_ID")
    if not chat_id:
        # Фолбэк: чат, привязанный ботом (scripts/telegram_bot.py) через /start
        try:
            chat_id = json.loads(BINDING_FILE.read_text(encoding="utf-8")).get("chat_id")
        except (OSError, ValueError):
            pass
    if not token or not chat_id:
        raise SystemExit(_config_error(
            "не задан TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID (окружение или "
            "--env-file; chat_id также привязывается через /start бота); "
            "шаблон: scripts/telegram.env.example"))
    def numeric(key, default, cast):
        raw = get(key, default)
        try:
            return cast(raw)
        except (TypeError, ValueError):
            raise SystemExit(_config_error(f"{key} должно быть числом, получено: {raw!r}"))

    return {
        "token": token,
        "chat_id": chat_id,
        "timeout": numeric("TELEGRAM_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC, float),
        "retries": numeric("TELEGRAM_RETRIES", DEFAULT_RETRIES, int),
    }


def _config_error(message):
    print(f"ошибка конфигурации: {message}", file=sys.stderr)
    return EXIT_CONFIG


# --- Дайджест: поиск, front-matter ----------------------------------------

def find_latest(directory):
    """Самый свежий .md в папке по mtime; файлы контракта пропускаются."""
    candidates = [
        p for p in Path(directory).glob("*.md")
        if p.name not in ("digest_format.md", FAILURE_LOG_NAME)
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


def split_front_matter(text):
    """Отделяет YAML front-matter. Возвращает (метаданные, тело).

    Разбираются только скалярные пары верхнего уровня (run_id и т.п.) —
    для доставки большего не нужно; items[] читает фаза 2, не мы.
    """
    meta = {}
    if not text.startswith("---\n"):
        return meta, text
    end = text.find("\n---", 4)
    if end == -1:
        return meta, text
    for line in text[4:end].splitlines():
        match = re.match(r"^(\w+):\s*(.+?)\s*$", line)
        if match:
            meta[match.group(1)] = match.group(2)
    body = text[end + 4:].lstrip("\n")
    return meta, body


# --- Конвертация markdown → Telegram-HTML ---------------------------------

def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_html(md):
    """Поддерживается ровно то, что есть в дайджесте: ссылки, **жирный**, # заголовок."""
    links = []

    def stash_link(match):
        links.append((match.group(1), match.group(2)))
        return f"\x00{len(links) - 1}\x00"

    # URL допускает один уровень скобок внутри (Wikipedia-style /wiki/Foo_(bar))
    text = re.sub(r"\[([^\]]+)\]\((https?://(?:[^()\s]|\([^()\s]*\))+)\)", stash_link, md)
    text = escape_html(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    out_lines = []
    for line in text.splitlines():
        if line.startswith("#"):
            line = f"<b>{line.lstrip('#').strip()}</b>"
        out_lines.append(line)
    text = "\n".join(out_lines)

    def restore_link(match):
        label, url = links[int(match.group(1))]
        url = url.replace("&", "&amp;").replace('"', "%22")
        return f'<a href="{url}">{escape_html(label)}</a>'

    return re.sub("\x00(\\d+)\x00", restore_link, text)


def html_to_plain(html):
    """Фолбэк при ошибке разметки: тот же текст без тегов, ссылки как «текст (url)»."""
    text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 (\1)", html)
    text = re.sub(r"</?b>", "", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def split_message(text, limit=MAX_MESSAGE_LEN):
    """Разбиение по границам строк; строка длиннее лимита режется жёстко."""
    parts, current = [], ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


# --- Отправка --------------------------------------------------------------

def _post(payload, token, timeout):
    """Один POST к Bot API. Ошибки переупаковываются без токена и URL."""
    request = urllib.request.Request(
        API_URL_TEMPLATE.format(token=token),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def send_message(text, config, parse_mode="HTML", reply_markup=None):
    """Отправка одного сообщения с ретраями. reply_markup — крючок фазы 2."""
    payload = {
        "chat_id": config["chat_id"],
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    attempts = 0
    flood_waits = 0
    while True:
        try:
            _post(payload, config["token"], config["timeout"])
            return
        except urllib.error.HTTPError as error:
            body = _read_error_body(error)
            if error.code == 429 and flood_waits < MAX_FLOOD_WAITS:
                flood_waits += 1  # попытка не сгорает
                time.sleep(body.get("parameters", {}).get("retry_after", 5))
                continue
            if error.code == 400 and parse_mode:
                # кривая разметка: одноразовый фолбэк в plain text
                send_message(html_to_plain(text), config, parse_mode=None,
                             reply_markup=reply_markup)
                return
            description = body.get("description", "нет описания")
            if error.code >= 500:
                attempts += 1
                if attempts < config["retries"]:
                    time.sleep(2 ** attempts)
                    continue
            raise DeliveryError(f"Bot API HTTP {error.code}: {description}")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            attempts += 1
            if attempts < config["retries"]:
                time.sleep(2 ** attempts)
                continue
            raise DeliveryError(f"сеть недоступна: {type(error).__name__}: {reason}")


def _read_error_body(http_error):
    try:
        return json.loads(http_error.read().decode("utf-8"))
    except (ValueError, OSError):
        return {}


# --- Идемпотентность и журнал сбоев ----------------------------------------

def digest_key(meta, body):
    return meta.get("run_id") or hashlib.sha256(body.encode("utf-8")).hexdigest()


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sent": {}}


def sent_parts(state, key, total):
    """Сколько частей этого дайджеста уже доставлено (для дозаливки после сбоя)."""
    entry = state.get("sent", {}).get(key)
    if isinstance(entry, dict):
        return entry.get("parts_done", 0)
    return total if entry else 0  # старый формат: метка времени = доставлено целиком


def mark_progress(state, key, parts_done, total):
    """Прогресс пишется после каждой части: повторный запуск не шлёт дубли."""
    state.setdefault("sent", {})[key] = {
        "parts_done": parts_done, "total": total, "ts": int(time.time()),
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)  # атомарно: краш не оставит битый JSON


def log_failure(digest_path, reason):
    """Строка о сбое рядом с дайджестом — следующая сессия агента её увидит."""
    log_path = digest_path.parent / FAILURE_LOG_NAME
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"- {stamp} · {digest_path.name} · {reason}\n")


# --- CLI --------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Отправка markdown-дайджеста из vault в Telegram (см. docs/telegram_bot.md)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("digest", nargs="?", help="путь к файлу дайджеста")
    source.add_argument("--latest", metavar="DIR",
                        help="взять самый свежий .md из папки")
    parser.add_argument("--env-file", help="KEY=VALUE файл с секретами вне репозитория")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать сообщение и ключ идемпотентности, не отправлять")
    args = parser.parse_args(argv)

    if args.latest:
        digest_path = find_latest(args.latest)
        if digest_path is None:
            print(f"в папке нет дайджестов: {args.latest}", file=sys.stderr)
            return EXIT_NO_DIGEST
    else:
        digest_path = Path(args.digest)
        if not digest_path.is_file():
            print(f"дайджест не найден: {digest_path}", file=sys.stderr)
            return EXIT_NO_DIGEST

    meta, body = split_front_matter(digest_path.read_text(encoding="utf-8"))
    if not body.strip():
        print(f"дайджест пуст: {digest_path}", file=sys.stderr)
        return EXIT_NO_DIGEST

    key = digest_key(meta, body)
    parts = split_message(md_to_html(body))

    if args.dry_run:
        print(f"ключ идемпотентности: {key}")
        for number, part in enumerate(parts, 1):
            print(f"--- сообщение {number}/{len(parts)} ({len(part)} симв.) ---")
            print(part)
        return EXIT_OK

    state = load_state()
    done = sent_parts(state, key, len(parts))
    if done >= len(parts):
        print(f"уже отправлено (ключ {key}), пропуск")
        return EXIT_OK

    try:
        config = resolve_config(args.env_file)
    except SystemExit as error:
        return error.code

    try:
        for index in range(done, len(parts)):
            send_message(parts[index], config)
            mark_progress(state, key, index + 1, len(parts))
    except DeliveryError as error:
        print(f"доставка не удалась: {error}", file=sys.stderr)
        log_failure(digest_path, str(error))
        return EXIT_DELIVERY

    print(f"доставлено: {digest_path.name}, сообщений: {len(parts) - done}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
