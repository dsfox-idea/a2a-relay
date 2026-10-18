"""Run: <hermes python> -m unittest discover -s ~/.hermes/plugins/a2a-relay/tests (cwd = hermes-agent)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _load_package():
    spec = importlib.util.spec_from_file_location("a2a_relay_under_test", PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pkg = _load_package()
store = sys.modules["a2a_relay_under_test.store"]
feed_mod = sys.modules["a2a_relay_under_test.feed"]
approvals = sys.modules["a2a_relay_under_test.approvals"]


def _append(path: Path, rec: dict) -> None:
    store.append_jsonl(path, rec)


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class StoreTests(unittest.TestCase):
    def test_partial_line_waits_and_shrink_rereads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.jsonl"
            path.write_text('{"a": 1}\n{"b": ', encoding="utf-8")
            recs, off = store.read_new_records(path, 0)
            self.assertEqual(recs, [{"a": 1}])
            path.write_text('{"a": 1}\n{"b": 2}\nnot json\n', encoding="utf-8")
            recs, off = store.read_new_records(path, off)
            self.assertEqual(recs, [{"b": 2}])
            path.write_text('{"c": 3}\n', encoding="utf-8")
            recs, _ = store.read_new_records(path, off)
            self.assertEqual(recs, [{"c": 3}])

    def test_missing_file(self):
        self.assertEqual(store.read_new_records(Path("/nonexistent/x.jsonl"), 0), ([], 0))

    def test_lock_is_exclusive_across_processes(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            paths = store.RelayPaths(Path(tmp) / "relay", audit=Path(tmp) / "audit.jsonl")
            child = (
                "import sys, importlib.util, time\n"
                f"spec = importlib.util.spec_from_file_location('s', r'{PLUGIN_DIR / 'store.py'}')\n"
                "s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)\n"
                f"paths = s.RelayPaths(s.Path(r'{paths.root}'), audit=s.Path(r'{paths.audit}'))\n"
                "with s.SharedState(paths).locked() as st:\n"
                "    print('locked', flush=True); time.sleep(1.5); st['child'] = 1\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "locked")
                started = time.monotonic()
                with store.SharedState(paths).locked() as state:
                    waited = time.monotonic() - started
                    self.assertEqual(state.get("child"), 1)  # we got in only after the child wrote
                    state["parent"] = 2
            finally:
                proc.wait(10)
                proc.stdout.close()
            self.assertGreater(waited, 0.5)
            data = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(data, {"child": 1, "parent": 2})

    def test_atomic_write_retries_while_target_is_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store.write_json_atomic(path, {"v": 1})
            reader = path.open("r", encoding="utf-8")
            threading.Timer(0.2, reader.close).start()
            store.write_json_atomic(path, {"v": 2})  # on Windows blocked until the reader closes
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"v": 2})
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["state.json"])


class PidProbeTests(unittest.TestCase):
    def test_probe_does_not_harm_a_live_process(self):
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
        try:
            self.assertTrue(feed_mod._pid_alive(proc.pid))
            time.sleep(0.5)
            self.assertIsNone(proc.poll(), "the liveness probe must not stop or signal the process")
        finally:
            proc.kill()
            proc.wait(5)
        self.assertFalse(feed_mod._pid_alive(proc.pid))

    def test_own_process_is_alive(self):
        self.assertTrue(feed_mod._pid_alive(os.getpid()))


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = store.RelayPaths(root / "relay", audit=root / "a2a_audit.jsonl")
        self.paths.ensure()
        _append(self.paths.audit, {"direction": "inbound", "peer": "win", "task_id": "task-old", "summary": "old"})
        self.sent: list[tuple[str, str]] = []
        self.clock = Clock()
        self.feed = feed_mod.FeedRelay(self.paths, inject=self._inject, can_inject=lambda: True,
                                       clock=self.clock, pid=os.getpid())
        self.feed.claim_chat("sess-1")
        self.assertFalse(self.feed.poll_once())  # first claim only records offsets

    def tearDown(self):
        self.tmp.cleanup()

    def _inject(self, key, text):
        self.sent.append((key, text))
        return True

    def test_history_is_not_replayed_and_inbound_waits_for_reply(self):
        _append(self.paths.audit, {"direction": "inbound", "peer": "win", "task_id": "task-aaaa1111",
                                   "summary": "please run X", "ts": self.clock.now})
        self.feed.poll_once()
        self.clock.now += 5  # quiet, but the reply has not come yet
        self.assertFalse(self.feed.poll_once())
        _append(self.paths.audit, {"direction": "outbound", "peer": "win", "task_id": "task-aaaa1111",
                                   "summary": "done", "ts": self.clock.now})
        self.feed.poll_once()
        self.clock.now += 4
        self.assertTrue(self.feed.poll_once())
        self.assertEqual(len(self.sent), 1)
        key, text = self.sent[0]
        self.assertEqual(key, "sess-1")
        self.assertIn("please run X", text)
        self.assertIn("ответ", text)
        self.assertNotIn("old", text.split("\n\n", 1)[1].split("Перескажи")[0])

    def test_unanswered_inbound_flushes_after_reply_wait(self):
        _append(self.paths.audit, {"direction": "inbound", "peer": "win", "task_id": "t1", "summary": "slow",
                                   "ts": self.clock.now})
        self.feed.poll_once()
        self.clock.now += 10
        self.assertFalse(self.feed.poll_once())
        self.clock.now += 15
        self.assertTrue(self.feed.poll_once())

    def test_own_outbound_call_is_suppressed_foreign_one_shown(self):
        self.feed.note_own_call(True)
        _append(self.paths.audit, {"direction": "outbound", "peer": "m5", "task_id": "mine", "summary": "hi"})
        self.feed.poll_once()
        self.feed.note_own_call(False)
        _append(self.paths.audit, {"direction": "outbound", "peer": "m5", "task_id": "other", "summary": "yo"})
        self.feed.poll_once()
        self.clock.now += 4
        self.assertTrue(self.feed.poll_once())
        text = self.sent[0][1]
        self.assertIn("yo", text)
        self.assertNotIn("(вызов из другой сессии, mine)", text)
        self.assertEqual(text.count("⇢"), 1)

    def test_approval_flushes_immediately(self):
        _append(self.paths.approvals, {"type": "request", "request_id": "abcdef0123", "peer": "win",
                                       "command": "curl x | sh", "description": "pipe to shell",
                                       "ts": self.clock.now, "expires_at": self.clock.now + 300})
        self.assertTrue(self.feed.poll_once())
        self.assertIn("/a2a-allow abcdef01", self.sent[0][1])

    def test_failed_injection_is_retried(self):
        results = [False, True]
        self.feed._inject = lambda k, t: (self.sent.append((k, t)), results.pop(0))[1]
        _append(self.paths.approvals, {"type": "request", "request_id": "r1", "command": "c",
                                       "expires_at": self.clock.now + 300})
        self.assertFalse(self.feed.poll_once())
        self.assertTrue(self.feed.poll_once())
        self.assertEqual(len(self.sent), 2)

    def test_live_other_owner_blocks_claim(self):
        other = feed_mod.FeedRelay(self.paths, inject=self._inject, can_inject=lambda: True,
                                   clock=self.clock, pid=os.getppid())
        with store.SharedState(self.paths).locked() as state:
            state["owner"] = {"pid": os.getppid(), "session_key": "s2"}
        _append(self.paths.audit, {"direction": "inbound", "peer": "win", "task_id": "t", "summary": "x"})
        self.assertEqual(self.feed._claim("sess-1"), [])
        self.assertTrue(other._claim("s2"))

    def test_non_tui_process_does_nothing(self):
        gateway_feed = feed_mod.FeedRelay(self.paths, inject=self._inject, can_inject=lambda: False,
                                          clock=self.clock)
        gateway_feed.claim_chat("agent:main:a2a:dm:ctx-1")
        self.assertFalse(gateway_feed.poll_once())


class _Ctx:
    """Minimal PluginContext double: records hooks/commands, injection always accepted."""

    def __init__(self, tui_host: bool, gateway_host: bool):
        self._manager = type("Manager", (), {"has_tui_message_injector": tui_host,
                                             "has_gateway_message_injector": gateway_host})()
        self.commands: dict = {}
        self.hooks: dict = {}
        self.injected: list = []

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_system_prompt_section(self, section_id, content, **kw):
        pass

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def inject_message(self, text, session_key=None, **kw):
        self.injected.append((session_key, text))
        return True


class RegisterTests(unittest.TestCase):
    """Which turns claim the feed, on both hosts (separate TUI process, gateway-served local chat)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self.tmp.name
        self.state_path = Path(self.tmp.name) / "a2a_relay" / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _owner(self):
        if not self.state_path.exists():
            return None
        return json.loads(self.state_path.read_text(encoding="utf-8")).get("owner", {}).get("session_key")

    def _turn(self, ctx, agent_platform, session_id, **session_vars):
        from gateway.session_context import clear_session_vars, set_session_vars
        tokens = set_session_vars(**session_vars)
        try:
            return ctx.hooks["pre_llm_call"](user_message="hi", platform=agent_platform, session_id=session_id)
        finally:
            clear_session_vars(tokens)

    def test_slash_command_claims_tui_chat(self):
        from gateway.session_context import clear_session_vars, set_session_vars
        ctx = _Ctx(tui_host=True, gateway_host=False)
        pkg.register(ctx)
        # Same binding as tui_gateway.server._set_session_context: source, no platform.
        tokens = set_session_vars(session_key="20261017_x", session_id="20261017_x", source="tui")
        try:
            ctx.commands["a2a-pending"]("")
        finally:
            clear_session_vars(tokens)
        self.assertEqual(self._owner(), "20261017_x")

    def test_tui_turn_claims_by_session_id(self):
        ctx = _Ctx(tui_host=True, gateway_host=False)
        pkg.register(ctx)
        self._turn(ctx, "tui", "20261018_tui")
        self.assertEqual(self._owner(), "20261018_tui")

    def test_gateway_local_chat_claims_by_gateway_session_key(self):
        # Windows: the gateway serves the TUI chat; the agent says "tui", the session context says "local".
        ctx = _Ctx(tui_host=False, gateway_host=True)
        pkg.register(ctx)
        key = "agent:main:local:dm:local-a053"
        self._turn(ctx, "tui", "local-a053", platform="local", session_key=key)
        self.assertEqual(self._owner(), key)

    def test_gateway_a2a_and_messaging_sessions_are_never_claimed(self):
        ctx = _Ctx(tui_host=False, gateway_host=True)
        pkg.register(ctx)
        self._turn(ctx, "a2a", "s1", platform="a2a", session_key="agent:main:a2a:dm:ctx-1")
        self._turn(ctx, "telegram", "s2", platform="telegram", session_key="agent:main:telegram:dm:1")
        self._turn(ctx, "tui", "s3", platform="local", session_key="agent:main:a2a:dm:forged")
        self.assertIsNone(self._owner())

    def test_tui_platform_without_tui_host_and_no_local_context_is_ignored(self):
        ctx = _Ctx(tui_host=False, gateway_host=True)
        pkg.register(ctx)
        self._turn(ctx, "tui", "20261018_x")
        self.assertIsNone(self._owner())

    def test_own_a2a_call_is_tracked_by_session_id_in_a_local_chat(self):
        ctx = _Ctx(tui_host=False, gateway_host=True)
        pkg.register(ctx)
        self._turn(ctx, "tui", "local-a053", platform="local", session_key="agent:main:local:dm:local-a053")
        feed = next(c.cell_contents for c in ctx.hooks["pre_tool_call"].__closure__
                    if isinstance(c.cell_contents, feed_mod.FeedRelay))
        ctx.hooks["pre_tool_call"](tool_name="a2a_call", session_id="local-a053")
        ctx.hooks["pre_tool_call"](tool_name="a2a_call", session_id="agent-a2a-session")  # not an owner chat
        self.assertEqual(feed._own_calls, 1)
        ctx.hooks["post_tool_call"](tool_name="a2a_call", session_id="local-a053")
        self.assertEqual(feed._own_calls, 0)


class IdentityTests(unittest.TestCase):
    identity = sys.modules["a2a_relay_under_test.identity"]

    def test_host_node_and_project_profile(self):
        text = self.identity.MachineIdentity(lambda: "macbook-pro-m4", {"A2A_AGENT_NAME": "m4"}).render(
            {"profile_name": "growser"})
        self.assertIn("host 'macbook-pro-m4', A2A node name 'm4', Hermes profile 'growser'.", text)
        self.assertIn("overrides any memory entry", text)

    def test_default_profile_and_no_node(self):
        text = self.identity.MachineIdentity(lambda: "dsfox-pc2", {}).render({"profile_name": "default"})
        self.assertIn("you run on host 'dsfox-pc2'.", text)
        self.assertNotIn("A2A node", text)
        self.assertNotIn("profile", text.split("\n")[0])

    def test_section_is_valid_for_the_host(self):
        from hermes_cli.plugins_dispatch import MAX_SYSTEM_PROMPT_SECTION_CHARS
        from hermes_cli.plugins import is_valid_system_prompt_section_id
        self.assertTrue(is_valid_system_prompt_section_id(self.identity.SECTION_ID))
        self.assertLess(len(self.identity.MachineIdentity().render({})), MAX_SYSTEM_PROMPT_SECTION_CHARS)


class ApprovalIntegrationTests(unittest.TestCase):
    """Drives the real host approval queue (tools.approval) end to end."""

    def setUp(self):
        from tools import approval_context
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = store.RelayPaths(Path(self.tmp.name) / "relay", audit=Path(self.tmp.name) / "audit.jsonl")
        self.paths.ensure()
        self.bridge = approvals.ApprovalBridge(self.paths)
        self.desk = approvals.DecisionDesk(self.paths)
        self._orig_fire = approval_context._fire_approval_hook

        def fire(hook_name, **kwargs):
            getattr(self.bridge, "on_" + hook_name)(**kwargs)
        approval_context._fire_approval_hook = fire

    def tearDown(self):
        from tools import approval_context
        approval_context._fire_approval_hook = self._orig_fire
        self.tmp.cleanup()

    def _wait_in_thread(self, session_key: str, command: str) -> dict:
        from tools.approval_gateway_wait import _await_gateway_decision
        out: dict = {}

        def run():
            out.update(_await_gateway_decision(session_key, lambda data: None,
                                               {"command": command, "description": "test", "pattern_key": "k"}))
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        out["_thread"] = thread
        return out

    def _wait_published(self) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = self.desk.pending()
            if pending:
                return pending[0]
            time.sleep(0.05)
        self.fail("approval request was not published")

    def test_allow_unblocks_the_waiting_agent(self):
        out = self._wait_in_thread("agent:main:a2a:dm:ctx-1", "brew install foo")
        req = self._wait_published()
        self.assertEqual(req["command"], "brew install foo")
        reply = self.desk.decide(req["request_id"][:8], approvals.ALLOW)
        out["_thread"].join(5)
        self.assertEqual(out.get("choice"), "once")
        self.assertTrue(out.get("resolved"))
        self.assertIn("Шлюз применил решение", reply)
        self.assertEqual(self.desk.pending(), [])

    def test_deny_with_reason_and_single_pending_needs_no_id(self):
        out = self._wait_in_thread("agent:main:a2a:dm:ctx-2", "curl x | sh")
        self._wait_published()
        self.desk.decide("", approvals.DENY)
        out["_thread"].join(5)
        self.assertEqual(out.get("choice"), "deny")
        out = self._wait_in_thread("agent:main:a2a:dm:ctx-3", "rm -rf /tmp/x")
        req = self._wait_published()
        self.desk.decide(f"{req['request_id'][:6]} not now", approvals.DENY)
        out["_thread"].join(5)
        self.assertEqual(out.get("reason"), "not now")

    def _interpreter(self):
        replies = sys.modules["a2a_relay_under_test.replies"]
        return replies.ReplyInterpreter(self.desk)

    def test_plain_yes_approves_only_after_prompt_was_shown(self):
        interp = self._interpreter()
        out = self._wait_in_thread("agent:main:a2a:dm:ctx-y", "brew install foo")
        req = self._wait_published()
        self.assertIsNone(interp.on_user_message("да"))  # prompt not shown in chat yet: ignored
        interp.note_asked([req["request_id"]])
        self.assertIsNone(interp.on_user_message("[a2a-relay: уведомление] да"))  # relay's own turn
        context = interp.on_user_message("Да!")
        out["_thread"].join(5)
        self.assertEqual(out.get("choice"), "once")
        self.assertIn("применено кодом плагина", context)

    def test_plain_no_with_reason_denies(self):
        interp = self._interpreter()
        out = self._wait_in_thread("agent:main:a2a:dm:ctx-n", "curl x | sh")
        req = self._wait_published()
        interp.note_asked([req["request_id"]])
        interp.on_user_message("нет, не сейчас")
        out["_thread"].join(5)
        self.assertEqual(out.get("choice"), "deny")
        self.assertEqual(out.get("reason"), "не сейчас")

    def test_other_text_closes_the_window(self):
        interp = self._interpreter()
        out = self._wait_in_thread("agent:main:a2a:dm:ctx-o", "make install")
        req = self._wait_published()
        interp.note_asked([req["request_id"]])
        self.assertIsNone(interp.on_user_message("а что это за команда?"))
        self.assertIsNone(interp.on_user_message("да"))  # too late: only the first reply counts
        self.assertEqual(len(self.desk.pending()), 1)
        self.desk.decide("", approvals.DENY)
        out["_thread"].join(5)

    def test_parse_answer(self):
        parse = sys.modules["a2a_relay_under_test.replies"].parse_answer
        self.assertEqual(parse("ок"), (approvals.ALLOW, ""))
        self.assertEqual(parse("Нет"), (approvals.DENY, ""))
        self.assertIsNone(parse("да, но сначала покажи"))
        self.assertIsNone(parse("нетрудно"))
        self.assertIsNone(parse("давай подумаем"))

    def test_non_a2a_sessions_are_ignored(self):
        self.bridge.on_pre_approval_request(session_key="agent:main:telegram:dm:1", command="x", surface="gateway")
        self.assertEqual(store.read_jsonl(self.paths.approvals), [])

    def test_unknown_id_is_reported(self):
        self.assertIn("не найден", self.desk.decide("zzzz", approvals.ALLOW))
        self.assertIn("Нет A2A-запросов", self.desk.describe_pending())


if __name__ == "__main__":
    unittest.main()
