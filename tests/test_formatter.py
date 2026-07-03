"""Тесты самой рискованной части доставки — конвертера и обвязки.

Запуск: python3 -m unittest discover tests
Только stdlib (unittest + mock), без pytest — по правилу zero dependencies.
"""

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import telegram_digest as td


class TestMdToHtml(unittest.TestCase):
    def test_escaping(self):
        self.assertEqual(td.md_to_html("a & b < c > d"),
                         "a &amp; b &lt; c &gt; d")

    def test_link(self):
        self.assertEqual(
            td.md_to_html("[Роль — Компания](https://example.com/v/1)"),
            '<a href="https://example.com/v/1">Роль — Компания</a>')

    def test_link_label_escaped(self):
        self.assertEqual(
            td.md_to_html("[C++ & Go](https://example.com)"),
            '<a href="https://example.com">C++ &amp; Go</a>')

    def test_bold_and_heading(self):
        self.assertEqual(td.md_to_html("# Вакансии — 2026-07-02"),
                         "<b>Вакансии — 2026-07-02</b>")
        self.assertEqual(td.md_to_html("fit **85%**"), "fit <b>85%</b>")

    def test_url_ampersand_escaped(self):
        self.assertEqual(
            td.md_to_html("[x](https://example.com/?a=1&b=2)"),
            '<a href="https://example.com/?a=1&amp;b=2">x</a>')

    def test_url_with_parentheses(self):
        self.assertEqual(
            td.md_to_html("[wiki](https://en.wikipedia.org/wiki/Foo_(bar))"),
            '<a href="https://en.wikipedia.org/wiki/Foo_(bar)">wiki</a>')


class TestHtmlToPlain(unittest.TestCase):
    def test_roundtrip(self):
        html = td.md_to_html("# Дайджест\n[Роль](https://example.com) · fit **85%** & <тест>")
        plain = td.html_to_plain(html)
        self.assertEqual(plain,
                         "Дайджест\nРоль (https://example.com) · fit 85% & <тест>")


class TestSplitMessage(unittest.TestCase):
    def test_short_stays_single(self):
        self.assertEqual(td.split_message("a\nb"), ["a\nb"])

    def test_split_on_line_boundaries(self):
        lines = [f"строка {i} " + "x" * 90 for i in range(100)]
        parts = td.split_message("\n".join(lines))
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), td.MAX_MESSAGE_LEN)
        # ни одна строка не разорвана: склейка частей == исходник
        self.assertEqual("\n".join(parts), "\n".join(lines))

    def test_oversized_line_hard_split(self):
        parts = td.split_message("y" * (td.MAX_MESSAGE_LEN + 10))
        self.assertEqual(len(parts), 2)
        self.assertLessEqual(max(len(p) for p in parts), td.MAX_MESSAGE_LEN)


class TestFrontMatter(unittest.TestCase):
    def test_meta_extracted_body_clean(self):
        meta, body = td.split_front_matter(
            "---\nrun_id: 2026-07-02-0800\nstats: 20/5/2\n---\n# Тело\nстрока")
        self.assertEqual(meta["run_id"], "2026-07-02-0800")
        self.assertEqual(body, "# Тело\nстрока")

    def test_no_front_matter_fallback(self):
        meta, body = td.split_front_matter("# Просто дайджест")
        self.assertEqual(meta, {})
        self.assertEqual(body, "# Просто дайджест")


class TestLoadEnvFile(unittest.TestCase):
    def _load(self, content):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(content)
        return td.load_env_file(handle.name)

    def test_quotes_stripped_and_export_prefix(self):
        values = self._load(
            '# комментарий\nTELEGRAM_BOT_TOKEN="123:abc"\n'
            "export TELEGRAM_CHAT_ID='42'\nTELEGRAM_RETRIES=3\n")
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "123:abc")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "42")
        self.assertEqual(values["TELEGRAM_RETRIES"], "3")


class TestDigestKey(unittest.TestCase):
    def test_run_id_wins(self):
        self.assertEqual(td.digest_key({"run_id": "r-1"}, "тело"), "r-1")

    def test_sha256_fallback(self):
        key = td.digest_key({}, "тело")
        self.assertEqual(len(key), 64)
        self.assertEqual(key, td.digest_key({}, "тело"))


class TestTokenSanitization(unittest.TestCase):
    CONFIG = {"token": "SECRET123:TOKEN", "chat_id": "42",
              "timeout": 0.1, "retries": 1}

    def test_network_error_message_has_no_token(self):
        with mock.patch.object(td.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("timed out")):
            with self.assertRaises(td.DeliveryError) as ctx:
                td.send_message("текст", self.CONFIG)
        self.assertNotIn("SECRET123", str(ctx.exception))
        self.assertNotIn("api.telegram.org", str(ctx.exception))

    def test_http_error_message_has_no_token(self):
        error = urllib.error.HTTPError(
            url=f"https://api.telegram.org/bot{self.CONFIG['token']}/sendMessage",
            code=403, msg="Forbidden", hdrs=None,
            fp=mock.Mock(read=lambda: b'{"description": "bot was blocked"}'))
        with mock.patch.object(td.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(td.DeliveryError) as ctx:
                td.send_message("текст", self.CONFIG)
        message = str(ctx.exception)
        self.assertNotIn("SECRET123", message)
        self.assertIn("bot was blocked", message)


class TestMultipartBody(unittest.TestCase):
    def test_fields_file_and_boundaries(self):
        body = td._multipart_body(
            {"chat_id": "42", "caption": "письмо"},
            "резюме.pdf", b"%PDF-1.4 data", "BND")
        self.assertIn(b'name="chat_id"\r\n\r\n42\r\n', body)
        self.assertIn("письмо".encode("utf-8"), body)
        self.assertIn('filename="резюме.pdf"'.encode("utf-8"), body)
        self.assertIn(b"%PDF-1.4 data", body)
        self.assertTrue(body.startswith(b"--BND\r\n"))
        self.assertTrue(body.endswith(b"--BND--\r\n"))


class TestBadRequestFallback(unittest.TestCase):
    def test_400_resends_plain_text(self):
        config = {"token": "t", "chat_id": "1", "timeout": 0.1, "retries": 1}
        bad_request = urllib.error.HTTPError(
            url="x", code=400, msg="Bad Request", hdrs=None,
            fp=mock.Mock(read=lambda: b'{"description": "can\'t parse entities"}'))
        calls = []

        def fake_post(payload, token, timeout):
            calls.append(payload)
            if payload.get("parse_mode"):
                raise bad_request

        with mock.patch.object(td, "_post", side_effect=fake_post):
            td.send_message("<b>жирный</b>", config)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("parse_mode", calls[1])
        self.assertEqual(calls[1]["text"], "жирный")


if __name__ == "__main__":
    unittest.main()
