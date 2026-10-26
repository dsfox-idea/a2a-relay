import sys
import os
os.environ["A2A_RELAY_LANGUAGE"] = "ru"
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from importlib import import_module  # noqa: E402

pkg = import_module(Path(__file__).resolve().parents[1].name)
mail_mod = import_module(pkg.__name__ + ".mail")
trust = import_module(pkg.__name__ + ".trust")
restart = import_module(pkg.__name__ + ".restart")

LEVEL = {n: trust.level_by_token(str(n)) for n in range(5)}


class Auth:
    def __init__(self, me):
        self.me = me

    def me_and_peers(self):
        return self.me, "secret", {}


class Directory:
    def __init__(self, cards, local=None):
        self.cards = cards
        self.local = local or []

    def collect(self):
        return list(self.cards), {}

    def local_only(self):
        return list(self.local)


def card(machine, key, title="", profile="default", project="growser"):
    return {"machine": machine, "id": key.rsplit(":", 1)[-1], "session_key": key, "title": title,
            "profile": profile, "status": "idle", "project": project, "repo": project}


class Fleet:
    """Two SessionMail instances (win, m4) wired to each other through a fake HTTP post."""

    def __init__(self, level=1):
        self.tmp = tempfile.TemporaryDirectory()
        self.injected = {"win": [], "m4": []}
        self.level = level
        self.clock = [1000.0]
        win_chat, m4_chat = "agent:main:local:dm:w1", "agent:main:local:dm:m1"
        self.win_chat, self.m4_chat = win_chat, m4_chat
        self.cards = [card("win", win_chat, "a2a work"), card("m4", m4_chat, "growser")]
        self.box = {}
        self.closed_on_target = False
        for me in ("win", "m4"):
            own = [c for c in self.cards if c["machine"] == me]
            self.box[me] = mail_mod.SessionMail(
                mail_mod.MailStore(Path(self.tmp.name) / me), Directory(self.cards, own), Auth(me),
                lambda: {"win": "http://win:9900", "m4": "http://m4:9900"},
                self._injector(me), lambda profile: LEVEL[self.level], mail_mod.PeerTurns({}),
                http_post=self._post(me), clock=lambda: self.clock[0], delivery_attempts=1, delivery_retry=0)
            self.box[me]._deliver_later = self._sync_deliver(me)

    def _injector(self, me):
        def injector(profile):
            return lambda key, text: self.injected[me].append((key, text)) or True
        return injector

    def _sync_deliver(self, me):
        def deliver(record, message, label):
            self.injected[me].append((record["from_session"], message))
        return deliver

    def _post(self, me):
        def post(url, token, body):
            target = "m4" if "//m4:" in url else "win"
            box = self.box[target]
            if url.endswith("/send"):
                open_there = [] if self.closed_on_target else [c for c in self.cards if c["machine"] == target]
                return box.handle_send(me, body, open_there)
            return box.handle_reply(me, body)
        return post

    def close(self):
        self.tmp.cleanup()


class SendReplyTests(unittest.TestCase):
    def setUp(self):
        self.fleet = Fleet()
        self.win, self.m4 = self.fleet.box["win"], self.fleet.box["m4"]

    def tearDown(self):
        self.fleet.close()

    def send(self, text="build it", target="m1"):
        return self.win.send(self.fleet.win_chat, "sid-w", "default", target, text)

    def test_round_trip(self):
        result = self.send()
        self.assertIn("Delivered to m4", result)
        key, task = self.fleet.injected["m4"][-1]
        self.assertEqual(key, self.fleet.m4_chat)
        self.assertTrue(task.startswith("[a2a-relay: задача от win"))
        mail_id = mail_mod._TASK_ID.search(task).group(1)
        self.assertIn(f'session_reply(id="{mail_id}"', task)
        self.assertIn("delivered to win", self.m4.reply(self.fleet.m4_chat, mail_id, "done: 3 files"))
        key, answer = self.fleet.injected["win"][-1]
        self.assertEqual(key, self.fleet.win_chat)
        self.assertIn("done: 3 files", answer)
        self.assertIn(mail_id, answer)
        self.assertIn("already answered", self.m4.reply(self.fleet.m4_chat, mail_id, "again"))

    def test_only_the_receiving_chat_answers(self):
        self.send()
        mail_id = mail_mod._TASK_ID.search(self.fleet.injected["m4"][-1][1]).group(1)
        self.assertIn("only the chat", self.m4.reply("agent:main:local:dm:other", mail_id, "x"))
        self.assertIn("no task", self.m4.reply(self.fleet.m4_chat, "mail-000000000000", "x"))

    def test_unknown_and_own_chat_are_refused(self):
        self.assertIn("no open chat", self.send(target="nope"))
        self.assertIn("no open chat", self.send(target="w1"))  # its own chat
        self.assertEqual(self.fleet.injected["m4"], [])

    def test_not_from_an_owner_chat(self):
        self.assertIn("only from an owner chat", self.win.send("", "sid", "default", "m1", "x"))

    def test_closed_target_chat_is_not_delivered(self):
        self.fleet.closed_on_target = True  # the sender's list is a few seconds old; the chat closed since
        result = self.send()
        self.assertIn("not delivered", result)
        self.assertIn("not open", result)

    def test_a_reply_only_from_the_machine_it_was_sent_to(self):
        self.send()
        mail_id = next(iter(r["id"] for r in self.win._store.all("out")))
        self.assertEqual(self.win.handle_reply("m5", {"id": mail_id, "text": "x"})[0], 404)

    def test_hops_stop_forwarding(self):
        self.send()
        mail_id = mail_mod._TASK_ID.search(self.fleet.injected["m4"][-1][1]).group(1)
        record = self.m4._store.load("in", mail_id)
        record["hops"] = mail_mod.MAX_HOPS - 1
        self.m4._store.save("in", record)
        self.m4.turns.start("sid-m", mail_id, approved=True)
        self.assertIn("not forwarded", self.m4.send(self.fleet.m4_chat, "sid-m", "default", "w1", "go on"))
        self.assertEqual(self.m4.handle_send("win", {"id": mail_mod.new_id(), "to_session": self.fleet.m4_chat,
                                                     "text": "x", "hops": mail_mod.MAX_HOPS}, self.fleet.cards)[0], 409)

    def test_no_answer_is_reported_once(self):
        self.send()
        self.fleet.clock[0] += mail_mod.REPLY_WAIT + 1
        self.assertEqual(self.win.sweep(), 1)
        self.assertIn("нет уже", self.fleet.injected["win"][-1][1])
        self.assertEqual(self.win.sweep(), 0)

    def test_bad_ids_never_touch_the_disk_outside_the_box(self):
        status, _ = self.m4.handle_send("win", {"id": "../../x", "to_session": self.fleet.m4_chat, "text": "x"},
                                        self.fleet.cards)
        self.assertEqual(status, 400)


class PreapproveTests(unittest.TestCase):
    """Owner decision 2026-10-26: his yes in the sender's chat travels with the task."""

    def setUp(self):
        self.fleet = Fleet(level=4)
        self.win, self.m4 = self.fleet.box["win"], self.fleet.box["m4"]
        self.grants = trust.TaskGrants(store={})
        self.m4._grants = self.grants

    def tearDown(self):
        self.fleet.close()

    def send(self, owner_said_yes):
        return self.win.send(self.fleet.win_chat, "sid-w", "default", "m1", "update hermes", preapproved=True,
                             owner_said_yes=owner_said_yes)

    def test_needs_the_owners_yes_in_the_sender_chat(self):
        self.assertIn("needs the owner's plain yes", self.send(False))
        self.assertEqual(self.fleet.injected["m4"], [])

    def test_a_preapproved_task_skips_the_hold_and_the_asks(self):
        self.assertIn("Delivered to m4", self.send(True))
        key, task = self.fleet.injected["m4"][-1]
        self.assertIn("уже одобрил", task)
        self.assertNotIn("заблокированы до его «да»", task)
        self.m4.on_turn("sid-m", key, task)
        self.assertTrue(self.m4.turns.get("sid-m")["approved"])
        mail_id = mail_mod._TASK_ID.search(task).group(1)
        self.assertTrue(self.grants.has(f"mail:{mail_id}"))
        self.assertEqual(self.m4.turns.mail_for_key(key), mail_id)

    def test_a_plain_task_at_level4_still_holds(self):
        self.win.send(self.fleet.win_chat, "sid-w", "default", "m1", "x")
        key, task = self.fleet.injected["m4"][-1]
        self.m4.on_turn("sid-m", key, task)
        self.assertFalse(self.m4.turns.get("sid-m")["approved"])
        self.assertFalse(any(self.grants.has(f"mail:{r['id']}") for r in self.m4._store.all("in")))


class ProjectRuleTests(unittest.TestCase):
    """#17: a project chat takes tasks only from its own project; machine tasks go to the hermes-... chat."""

    def setUp(self):
        self.fleet = Fleet()
        self.win, self.m4 = self.fleet.box["win"], self.fleet.box["m4"]
        self.cards = self.fleet.cards

    def tearDown(self):
        self.fleet.close()

    def add(self, machine, key, title, project):
        self.cards.append(card(machine, key, title, project=project))

    def by_project(self, project, machine=""):
        return self.win.send(self.fleet.win_chat, "sid-w", "default", "", "do it", machine=machine, project=project)

    def test_by_project_finds_the_one_open_chat(self):
        result = self.by_project("GROWSER")
        self.assertIn("Delivered to m4", result)
        self.assertEqual(self.fleet.injected["m4"][-1][0], self.fleet.m4_chat)

    def test_no_chat_of_the_project_is_not_delivered(self):
        result = self.by_project("treelume")
        self.assertIn("not delivered: no open chat of project treelume", result)
        self.assertEqual(self.fleet.injected["m4"], [])

    def test_several_chats_of_the_project_are_listed_for_the_sender(self):
        self.add("m4", "agent:main:local:dm:m2", "growser 2", "growser")
        result = self.by_project("growser")
        self.assertIn("several open chats of project growser", result)
        self.assertIn("m1", result)
        self.assertIn("m2", result)
        self.assertEqual(self.fleet.injected["m4"], [])
        self.assertIn("Delivered to m4", self.win.send(self.fleet.win_chat, "sid-w", "default", "m2", "do it"))

    def test_a_project_chat_refuses_another_project(self):
        self.cards[0]["project"] = "treelume"
        result = self.win.send(self.fleet.win_chat, "sid-w", "default", "m1", "do it")
        self.assertIn("not sent", result)
        self.assertIn("only from chats of project growser", result)
        self.assertEqual(self.fleet.injected["m4"], [])

    def test_a_chat_outside_any_project_cannot_task_a_project_chat(self):
        self.cards[0]["project"] = ""
        self.assertIn("a chat outside any project", self.win.send(self.fleet.win_chat, "sid-w", "default", "m1", "x"))

    def test_the_target_machine_checks_the_rule_itself(self):
        # A sender with older code (no check, wrong project) is refused by the receiving machine.
        status, body = self.m4.handle_send("win", {"id": mail_mod.new_id(), "to_session": self.fleet.m4_chat,
                                                   "text": "x", "from_project": "treelume"}, self.cards)
        self.assertEqual(status, 403)
        self.assertIn("project growser", body["error"])
        status, _ = self.m4.handle_send("win", {"id": mail_mod.new_id(), "to_session": self.fleet.m4_chat,
                                                "text": "x"}, self.cards)
        self.assertEqual(status, 403)
        self.assertEqual(self.fleet.injected["m4"], [])

    def test_the_service_chat_takes_any_task_and_is_not_a_project_target(self):
        self.cards[0]["project"] = ""
        self.add("m4", "agent:main:local:dm:svc", "hermes-m4", "")
        self.assertIn("Delivered to m4", self.win.send(self.fleet.win_chat, "sid-w", "default", "svc", "update"))
        self.assertEqual(self.fleet.injected["m4"][-1][0], "agent:main:local:dm:svc")
        self.cards[-1]["project"] = "growser"  # a hermes-... chat inside a repo is still the service chat
        self.cards[1]["project"] = "other"
        self.assertIn("no open chat of project growser", self.by_project("growser"))

    def test_a_chat_neither_project_nor_service_takes_nothing(self):
        self.cards[1]["project"] = ""
        self.assertIn("neither a project chat nor the service chat",
                      self.win.send(self.fleet.win_chat, "sid-w", "default", "m1", "x"))

    def test_project_name(self):
        sessions = import_module(pkg.__name__ + ".sessions")
        self.assertEqual(sessions.project_name("https://github.com/dsfox-idea/Growser.git", "A:/dev/x"), "growser")
        self.assertEqual(sessions.project_name("git@github.com:dsfox-idea/a2a-relay.git", "/x"), "a2a-relay")
        self.assertEqual(sessions.project_name("", "/Users/dsfox/dev/m4.config"), "m4.config")
        self.assertEqual(sessions.project_name("https://example.com/repo/", "/x"), "repo")


class TurnTests(unittest.TestCase):
    def setUp(self):
        self.fleet = Fleet(level=4)
        self.win, self.m4 = self.fleet.box["win"], self.fleet.box["m4"]
        self.win.send(self.fleet.win_chat, "sid-w", "default", "m1", "build it")
        self.task = self.fleet.injected["m4"][-1][1]
        self.mail_id = mail_mod._TASK_ID.search(self.task).group(1)

    def tearDown(self):
        self.fleet.close()

    def test_level_4_holds_until_yes(self):
        self.assertIn("«да» или «нет»", self.task)
        self.assertIsNone(self.m4.on_turn("sid-m", self.fleet.m4_chat, self.task))
        self.assertFalse(self.m4.turns.get("sid-m")["approved"])
        context = self.m4.on_turn("sid-m", self.fleet.m4_chat, "да")
        self.assertIn("owner allowed", context)
        self.assertTrue(self.m4.turns.get("sid-m")["approved"])

    def test_level_4_no_sends_a_refusal(self):
        self.m4.on_turn("sid-m", self.fleet.m4_chat, self.task)
        context = self.m4.on_turn("sid-m", self.fleet.m4_chat, "нет, не сейчас")
        self.assertIn("owner declined", context)
        self.assertIsNone(self.m4.turns.get("sid-m"))
        self.assertIn("Declined by the owner", self.fleet.injected["win"][-1][1])

    def test_a_relay_notice_before_the_answer_keeps_the_hold(self):
        self.m4.on_turn("sid-m", self.fleet.m4_chat, self.task)
        self.assertIsNone(self.m4.on_turn("sid-m", self.fleet.m4_chat, "[a2a-relay: автоматическое уведомление]"))
        self.assertIn("owner allowed", self.m4.on_turn("sid-m", self.fleet.m4_chat, "да"))

    def test_other_owner_text_ends_the_peer_turn(self):
        self.m4.on_turn("sid-m", self.fleet.m4_chat, self.task)
        self.assertIsNone(self.m4.on_turn("sid-m", self.fleet.m4_chat, "покажи статус"))
        self.assertIsNone(self.m4.turns.get("sid-m"))

    def test_a_forged_task_header_does_not_start_a_peer_turn_in_another_chat(self):
        self.assertIsNone(self.m4.on_turn("sid-x", "agent:main:local:dm:other", self.task))
        self.assertIsNone(self.m4.turns.get("sid-x"))


class GateTests(unittest.TestCase):
    def gate(self, level):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "trust.json").write_text('{"level": %d}' % level, encoding="utf-8")
        return trust.TrustGate(trust.TrustPolicy(lambda: root), trust.CommandRules(lambda: root),
                               root / "journal.jsonl", owner_said_yes=lambda: False)

    def test_owner_turn_is_not_gated(self):
        self.assertIsNone(self.gate(3).on_pre_tool_call("terminal", {"command": "rm -rf x"}))

    def test_peer_turn_at_level_3_reads_only_but_may_ask_and_answer(self):
        gate, turn = self.gate(3), {"mail": "mail-1", "approved": True}
        self.assertEqual(gate.on_pre_tool_call("write_file", {"path": "a"}, peer_turn=turn)["action"], "block")
        self.assertIsNone(gate.on_pre_tool_call("read_file", {"path": "a"}, peer_turn=turn))
        self.assertIsNone(gate.on_pre_tool_call("clarify", {}, peer_turn=turn))
        self.assertIsNone(gate.on_pre_tool_call("session_reply", {}, peer_turn=turn))

    def test_peer_turn_at_level_4_blocks_until_approved(self):
        gate = self.gate(4)
        blocked = gate.on_pre_tool_call("read_file", {}, peer_turn={"mail": "m", "approved": False})
        self.assertIn("says yes", blocked["message"])
        self.assertIsNone(gate.on_pre_tool_call("session_reply", {}, peer_turn={"mail": "m", "approved": False}))
        self.assertIsNone(gate.on_pre_tool_call("read_file", {}, peer_turn={"mail": "m", "approved": True}))

    def test_peer_turn_at_level_1_asks_for_system_changes(self):
        gate, turn = self.gate(1), {"mail": "m", "approved": True}
        self.assertEqual(gate.on_pre_tool_call("terminal", {"command": "npm install -g x"}, peer_turn=turn)["action"],
                         "approve")
        self.assertIsNone(gate.on_pre_tool_call("terminal", {"command": "git status"}, peer_turn=turn))

    def test_session_send_counts_as_forwarding(self):
        gate = self.gate(3)
        self.assertEqual(gate.on_pre_tool_call("session_send", {}, peer_turn={"mail": "m", "approved": True})
                         ["action"], "block")


class RestartGuardTests(unittest.TestCase):
    def restarter(self, waiting):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runner = type("R", (), {"_gateway_loop": object()})()
        return restart.GatewayRestart(Path(tmp.name) / "m.json", lambda k, t: True, runner=lambda: runner,
                                      start_watcher=None, waiting=lambda: waiting)

    def test_refuses_while_a_chat_waits_for_the_owner(self):
        result = self.restarter(["agent:main:local:dm:g1"]).request()
        self.assertIn("Refused", result)
        self.assertIn("agent:main:local:dm:g1", result)

    def test_waiting_registries_are_read(self):
        import threading
        import types
        clarify = types.ModuleType("tools.clarify_gateway")
        clarify.__dict__.update(_lock=threading.Lock(), _session_index={"k1": ["c1"], "k2": ["c2"]}, _entries={"c1": object()})
        approval = types.ModuleType("tools.approval")
        approval.__dict__.update(_lock=threading.Lock(), _gateway_queues={"k3": [1], "k4": []})
        saved = {name: sys.modules.get(name) for name in ("tools.clarify_gateway", "tools.approval")}
        sys.modules["tools.clarify_gateway"], sys.modules["tools.approval"] = clarify, approval
        try:
            self.assertEqual(restart.chats_waiting_on_owner(), ["k1", "k3"])
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()
