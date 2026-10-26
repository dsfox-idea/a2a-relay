"""Run: cd <hermes-agent> && hermes --run-module unittest discover -s <repo>/tests"""

import os
os.environ["A2A_RELAY_LANGUAGE"] = "ru"
import tempfile
import unittest
from pathlib import Path

from test_trust import FakeQueue, _load  # noqa: E402

consent_mod = _load("consent")
approvals = _load("approvals")
store = _load("store")

CMD = ("ssh -i $K root@188.166.79.105 \"docker exec kinember-api-1 rm -rf /var/photos/orig-backup\"")
ASKED = f"Удалить копии оригиналов на сервере? Команда:\n```\n{CMD}\n```\nОтветь «да» или «нет»."
KEY = "agent:main:local:dm:chat"


def history(*assistant_texts):
    rows = [{"role": "user", "content": "start"}]
    for text in assistant_texts:
        rows.append({"role": "assistant", "content": text})
    return rows + [{"role": "user", "content": "да"}]


class ChatConsentTests(unittest.TestCase):
    def setUp(self):
        self.consent = consent_mod.ChatConsent()

    def test_yes_covers_the_command_shown_right_before_it_once(self):
        self.consent.on_turn(KEY, "да", history(ASKED))
        self.assertFalse(self.consent.take("agent:main:local:dm:other", CMD), "another chat")
        self.assertTrue(self.consent.take(KEY, CMD))
        self.assertFalse(self.consent.take(KEY, CMD), "one prompt per yes")

    def test_whitespace_and_line_continuations_do_not_matter(self):
        shown = "Команда:\nssh -i $K root@188.166.79.105 \\\n  \"docker exec kinember-api-1 rm -rf /var/photos/orig-backup\""
        self.consent.on_turn(KEY, "Да!", history(shown))
        self.assertTrue(self.consent.take(KEY, CMD))

    def test_other_command_no_or_other_text_stay_asked(self):
        self.consent.on_turn(KEY, "да", history(ASKED))
        self.assertFalse(self.consent.take(KEY, CMD.replace("orig-backup", "")), "a shorter path is another command")
        self.assertFalse(self.consent.take(KEY, "rm -rf /"), "not shown")
        for reply in ("нет", "да, но сначала покажи список", "go"):
            self.consent.on_turn(KEY, reply, history(ASKED))
            self.assertFalse(self.consent.take(KEY, CMD), reply)

    def test_only_the_last_agent_message_counts(self):
        self.consent.on_turn(KEY, "да", history(ASKED, "Готово, что дальше?"))
        self.assertFalse(self.consent.take(KEY, CMD))

    def test_a_part_or_a_prefix_of_a_shown_command_never_counts(self):
        self.consent.on_turn(KEY, "да", history("Запущу `rm -rf /tmp/build --verbose` и потом `git push`"))
        for part in ("rm -rf /tmp/build", "rm -rf /", "git push --force", "--verbose` и потом `git push"):
            self.assertFalse(self.consent.take(KEY, part), part)
        self.assertTrue(self.consent.take(KEY, "rm -rf /tmp/build --verbose"))

    def test_a_shown_shell_line_with_a_dollar_prompt(self):
        self.consent.on_turn(KEY, "ок", history("Выполню:\n$ hermes update --yes --no-gateway-restart\nОк?"))
        self.assertTrue(self.consent.take(KEY, "hermes update --yes --no-gateway-restart"))

    def test_too_short_commands_are_never_matched(self):
        self.consent.on_turn(KEY, "да", history("rm -rf x? ls"))
        self.assertFalse(self.consent.take(KEY, "ls"))

    def test_a_new_owner_message_ends_the_consent(self):
        self.consent.on_turn(KEY, "да", history(ASKED))
        self.consent.on_turn(KEY, "а что с логами?", history(ASKED))
        self.assertFalse(self.consent.take(KEY, CMD))


class BridgeConsentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self.paths = store.RelayPaths(root=home / "a2a_relay", audit=home / "a2a_audit.jsonl")
        self.paths.ensure()

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_owners_yes_settles_hermes_prompt_in_his_chat(self):
        queue, consent = FakeQueue(), consent_mod.ChatConsent()
        bridge = approvals.ApprovalBridge(self.paths, queue=queue, auto_decide=lambda c, k, task: None,
                                          task_for_key=lambda key: "", owner_consent=consent.take)
        bridge.on_pre_approval_request(session_key=KEY, command=CMD, surface="gateway")
        self.assertEqual(queue.resolved, [], "no yes yet: Hermes asks")
        consent.on_turn(KEY, "да", history(ASKED))
        bridge.on_pre_approval_request(session_key=KEY, command=CMD, surface="gateway")
        self.assertEqual(queue.resolved, [("once", "req-1", None)])
        self.assertEqual(store.read_jsonl(self.paths.approvals), [])


if __name__ == "__main__":
    unittest.main()
