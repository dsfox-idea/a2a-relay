import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_jobs import _load  # noqa: E402

i18n = _load("i18n")
replies = _load("replies")
mail = _load("mail")
approvals = _load("approvals")

PLUGIN_DIR = Path(__file__).resolve().parents[1]
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


class Lang:
    def __init__(self, code):
        self.code = code

    def __enter__(self):
        self.saved = os.environ.get("A2A_RELAY_LANGUAGE")
        os.environ["A2A_RELAY_LANGUAGE"] = self.code

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop("A2A_RELAY_LANGUAGE", None)
        else:
            os.environ["A2A_RELAY_LANGUAGE"] = self.saved


class CatalogTests(unittest.TestCase):
    def test_every_text_exists_in_both_languages_with_the_same_fields(self):
        field = re.compile(r"{(\w+)}")
        for key in i18n.keys():
            en, ru = i18n._TEXTS[key]
            self.assertTrue(en and ru, key)
            self.assertEqual(set(field.findall(en)), set(field.findall(ru)), key)
            self.assertFalse(CYRILLIC.search(en), f"{key}: Cyrillic in the English text")

    def test_language_selection(self):
        with Lang("ru"):
            self.assertEqual(i18n.language(), "ru")
            self.assertIn("владельца", i18n.t("feed.header"))
        with Lang("pt-BR"):
            self.assertEqual(i18n.language(), "en")  # unknown language: English
        with Lang("RU_ru"):
            self.assertEqual(i18n.language(), "ru")
        with Lang("en"):
            self.assertIn("not a message from the owner", i18n.t("feed.header"))

    def test_no_owner_name_anywhere(self):
        for en, ru in i18n._TEXTS.values():
            self.assertNotIn("Dmitry", en)
            self.assertNotIn("Дмитри", ru)

    def test_no_cyrillic_left_outside_the_catalog(self):
        for path in PLUGIN_DIR.glob("*.py"):
            if path.name == "i18n.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                self.assertFalse(CYRILLIC.search(line), f"{path.name}:{number}: {line.strip()}")


class RecognitionTests(unittest.TestCase):
    def test_own_messages_are_recognised_in_any_language(self):
        for code in ("en", "ru"):
            with Lang(code):
                self.assertTrue(i18n.is_relay_text(i18n.t("feed.header")))
                self.assertTrue(i18n.is_relay_text(i18n.t("restart.back", old=1, new=2, took=3)))
        self.assertFalse(i18n.is_relay_text("да"))
        self.assertFalse(i18n.is_relay_text(None))

    def test_a_task_written_in_one_language_is_found_in_the_other(self):
        record = {"id": "mail-0123456789ab", "from_machine": "win", "from_title": "a chat [x]"}
        level = type("L", (), {"number": 1, "name": "delegate"})()
        for code in ("en", "ru"):
            with Lang(code):
                text = mail.SessionMail.frame(record, "do it", level)
            match = mail._TASK_ID.search(text.split("\n", 1)[0])
            self.assertEqual(match.group(1), "mail-0123456789ab", code)

    def test_yes_and_no_in_both_languages(self):
        for word in ("да", "ок", "yes", "Y", "approve"):
            self.assertEqual(replies.parse_answer(word), (approvals.ALLOW, ""), word)
        self.assertEqual(replies.parse_answer("no, not now"), (approvals.DENY, "not now"))
        self.assertEqual(replies.parse_answer("нет, не сейчас"), (approvals.DENY, "не сейчас"))
        for text in ("yesterday", "nobody", "нетрудно", "yes, but first show me"):
            self.assertIsNone(replies.parse_answer(text), text)


if __name__ == "__main__":
    unittest.main()
