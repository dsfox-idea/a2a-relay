"""Run: cd <hermes-agent> && hermes --run-module unittest discover -s <repo>/tests"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from test_jobs import _load

coder = _load("coder")
coder_tools = _load("coder_tools")
trust = _load("trust")
store = _load("store")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "acpx"


def parse(name: str, exit_code: int):
    parser = coder.TranscriptParser()
    for line in (FIXTURES / f"{name}.ndjson").read_text(encoding="utf-8").splitlines():
        parser.feed(line)
    return parser, parser.outcome(exit_code)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git("init", "-q", cwd=path)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init",
        cwd=path)
    return path


class ParserTests(unittest.TestCase):
    def test_fixtures(self):
        cases = {"ok": (0, coder.DONE, "end_turn"), "denied": (5, coder.DENIED, "end_turn"),
                 "escalated": (5, coder.DENIED, "end_turn"), "timeout": (3, coder.TIMEOUT, "cancelled"),
                 "cancelled": (0, coder.CANCELLED, "cancelled")}
        for name, (code, state, stop) in cases.items():
            parser, outcome = parse(name, code)
            self.assertEqual((outcome.state, outcome.stop_reason), (state, stop), name)
            self.assertEqual(parser.bad_lines, 0, name)
        self.assertEqual(parse("ok", 0)[1].text, "done")
        self.assertIn("denied", parse("denied", 5)[1].text)
        self.assertEqual(parse("denied", 5)[1].failed_tools, 1)
        escalations = parse("escalated", 5)[1].escalations
        self.assertEqual(escalations[0]["toolKind"], "execute")
        self.assertIn("mkdir esc-dir", escalations[0]["toolInput"]["command"])
        self.assertEqual(parse("timeout", 3)[0].error_code, "TIMEOUT")
        self.assertGreaterEqual(parse("ok", 0)[1].tool_calls, 1)

    def test_broken_and_empty_streams(self):
        parser = coder.TranscriptParser()
        for line in ("", "not json", "[1,2]", '{"method":"session/update","params":{"update":"x"}}'):
            parser.feed(line)
        self.assertEqual(parser.bad_lines, 1)
        self.assertEqual(parser.outcome(1).state, coder.FAILED)
        self.assertEqual(parser.outcome(1).text, "")

    def test_exit_codes(self):
        expected = {(0, "end_turn"): coder.DONE, (0, "cancelled"): coder.CANCELLED, (1, ""): coder.FAILED,
                    (2, ""): coder.FAILED, (3, ""): coder.TIMEOUT, (4, ""): coder.NO_SESSION,
                    (5, "end_turn"): coder.DENIED, (130, ""): coder.CANCELLED}
        for (code, stop), state in expected.items():
            self.assertEqual(coder.state_for(code, stop), state, (code, stop))


class CommandTests(unittest.TestCase):
    def test_argv(self):
        cmd = coder.AcpxCommand(("node", "cli.js"), "approve-reads", 900)
        cwd = Path("C:/dev/my project")
        prompt = cmd.prompt("claude", cwd, "s1")
        self.assertEqual(prompt[:4], ["node", "cli.js", "--cwd", str(cwd)])
        for flag in ("--approve-reads", "--json-strict", "--suppress-reads"):
            self.assertIn(flag, prompt)
        self.assertEqual(prompt[prompt.index("--timeout") + 1], "900")
        self.assertEqual(prompt[prompt.index("--non-interactive-permissions") + 1], "deny")
        self.assertEqual(prompt[-5:], ["claude", "-s", "s1", "--file", "-"])
        self.assertEqual(cmd.set_mode("claude", cwd, "s1", "default")[-4:], ["set-mode", "default", "-s", "s1"])
        self.assertEqual(cmd.cancel("opencode", cwd, "s1")[-4:], ["opencode", "cancel", "-s", "s1"])

    def test_windows_cmd_shim_runs_node_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "acpx.CMD"
            cli = Path(tmp) / "node_modules" / "acpx" / "dist" / "cli.js"
            cli.parent.mkdir(parents=True)
            cli.write_text("", encoding="utf-8")
            shim.write_text("", encoding="utf-8")
            which = {"acpx": str(shim), "node": "C:/node.exe"}.get
            self.assertEqual(coder.resolve_acpx("acpx", which), ["C:/node.exe", str(cli)])
            self.assertEqual(coder.resolve_acpx("acpx", {"acpx": "/usr/bin/acpx"}.get), ["/usr/bin/acpx"])


class ConfigTests(unittest.TestCase):
    def test_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coder.json"
            with self.assertRaises(coder.CoderError):
                coder.CoderConfig.load(path)
            path.write_text(json.dumps({"roots": ["~/dev"], "executors": ["claude", "opencode"]}), encoding="utf-8")
            cfg = coder.CoderConfig.load(path)
            self.assertEqual(cfg.default_executor, "claude")
            self.assertEqual(cfg.session_mode("claude"), "default")
            self.assertEqual(cfg.session_mode("opencode"), "")
            path.write_text(json.dumps({"roots": []}), encoding="utf-8")
            with self.assertRaises(coder.CoderError):
                coder.CoderConfig.load(path)
            path.write_text(json.dumps({"roots": ["x"], "permissions": "yolo"}), encoding="utf-8")
            with self.assertRaises(coder.CoderError):
                coder.CoderConfig.load(path)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "dev"
        self.repo = make_repo(self.root / "proj")
        (self.repo / "sub").mkdir()
        self.scope = coder.ProjectScope([self.root])

    def tearDown(self):
        self._tmp.cleanup()

    def test_project_is_the_git_root_of_the_session_cwd(self):
        self.assertEqual(self.scope.project_of(str(self.repo / "sub")), self.repo.resolve())
        if os.name == "nt":
            self.assertEqual(self.scope.project_of(str(self.repo).swapcase()).name.lower(), "proj")

    def test_refusals(self):
        outside = make_repo(Path(self._tmp.name) / "elsewhere")
        (self.root / "plain").mkdir()
        for cwd in ("", "relative/path", str(self.root / "missing"), str(outside), str(self.root / "plain")):
            with self.assertRaises(coder.CoderError, msg=cwd):
                self.scope.project_of(cwd)

    def test_symlink_out_of_the_roots_is_refused(self):
        outside = make_repo(Path(self._tmp.name) / "outside")
        link = self.root / "link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("no symlink permission")
        with self.assertRaises(coder.CoderError):
            self.scope.project_of(str(link))


class FakeRunner:
    def __init__(self, fixture="ok", exit_code=0, gate=None, prepare_code=0):
        self.fixture, self.exit_code, self.gate, self.prepare_code = fixture, exit_code, gate, prepare_code
        self.calls, self.prompts = [], []

    def call(self, argv, timeout=180):
        self.calls.append(argv)
        return (self.prepare_code if "sessions" in argv else 0), "ok"

    def run(self, argv, brief, transcript, parser, on_start, wait_s):
        self.prompts.append((argv, brief))
        if self.gate:
            self.gate.wait(5)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        lines = (FIXTURES / f"{self.fixture}.ndjson").read_text(encoding="utf-8").splitlines()
        transcript.write_text("\n".join(lines), encoding="utf-8")
        for line in lines:
            parser.feed(line)
        return self.exit_code


class FakeWorktree:
    def __init__(self, base: Path):
        self.base, self.created = base, []

    def create(self, project, job_id):
        path = self.base / f"{project.name}-{job_id}"
        path.mkdir(parents=True)
        self.created.append(path)
        return path, f"a2a/{job_id}", "abc123"

    def summary(self, path, base):
        return "Изменения относительно исходного коммита:\n hello.txt | 1 +"


class BoardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = coder.CoderConfig(executors=("claude", "opencode"), roots=(self.tmp,))
        self.sent, self.accept = [], True
        self.worktree = FakeWorktree(self.tmp / "wt")
        self.alive = set()
        self.runner = FakeRunner()

    def tearDown(self):
        self._tmp.cleanup()

    def board(self, runner=None, attempts=3):
        delivery = coder.ChatDelivery(lambda key, text: self.sent.append((key, text)) or self.accept, 0, attempts,
                                      sleep=lambda s: None)
        return coder.CodeBoard(self.tmp / "code", lambda: self.cfg, None, runner=runner or self.runner,
                               worktrees=lambda cfg: self.worktree, pid_alive=lambda pid: pid in self.alive,
                               delivery=delivery, sleep=lambda s: None, resolve=lambda name: ["acpx"])

    def test_success_is_delivered_with_branch_and_diff(self):
        board = self.board()
        job = board.start("create hello.txt", self.tmp / "proj", "chat-1", run_inline=True)
        saved = board.jobs()[0]
        self.assertEqual((saved.state, saved.reply, saved.delivered), (coder.DONE, "done", True))
        key, text = self.sent[0]
        self.assertEqual(key, "chat-1")
        self.assertIn(f"a2a/{job.job_id}", text)
        self.assertIn("hello.txt | 1 +", text)
        argv, brief = self.runner.prompts[0]
        self.assertIn("Do not ask questions", brief)
        self.assertIn(str(self.worktree.created[0]), argv)
        modes = [c for c in self.runner.calls if "set-mode" in c]
        self.assertEqual(modes[0][-4:], ["set-mode", "default", "-s", job.session], "auto mode ignores permissions")
        self.assertTrue(board.transcript(job.job_id).exists())

    def test_denied_reports_the_escalated_command(self):
        board = self.board(FakeRunner("escalated", 5))
        board.start("mkdir", self.tmp / "proj", "chat-1", run_inline=True)
        job = board.jobs()[0]
        self.assertEqual(job.state, coder.DENIED)
        self.assertIn("mkdir esc-dir", job.reply)

    def test_session_failure_is_a_failed_job(self):
        board = self.board(FakeRunner(prepare_code=1))
        board.start("x", self.tmp / "proj", "chat-1", run_inline=True)
        self.assertEqual(board.jobs()[0].state, coder.FAILED)
        self.assertIn("could not open session", board.jobs()[0].reply)

    def test_undeliverable_result_is_flagged(self):
        self.accept = False
        board = self.board()
        board.start("x", self.tmp / "proj", "chat-1", run_inline=True)
        self.assertEqual(len(self.sent), 3)
        self.assertIn("NOT delivered", board.describe(str(self.tmp / "proj")))

    def test_bad_requests(self):
        board = self.board()
        for kwargs in ({"task": "  "}, {"task": "x", "executor": "codex"}, {"task": "x", "session": "nope"}):
            with self.assertRaises(coder.CoderError, msg=kwargs):
                board.start(project=self.tmp / "proj", session_key="k", run_inline=True, **kwargs)
        self.assertEqual(self.worktree.created, [])

    def test_continuing_a_session_reuses_its_worktree(self):
        board = self.board()
        first = board.start("one", self.tmp / "proj", "k", run_inline=True)
        second = board.start("two", self.tmp / "proj", "k", session=first.session, run_inline=True)
        self.assertEqual(second.worktree, first.worktree)
        self.assertEqual(len(self.worktree.created), 1)
        prepares = [c for c in self.runner.calls if "sessions" in c]
        self.assertEqual([c[c.index("sessions") + 1] for c in prepares], ["new", "ensure"])
        with self.assertRaises(coder.CoderError):
            board.start("three", self.tmp / "proj", "k", executor="opencode", session=first.session)

    def test_projects_do_not_see_each_others_jobs(self):
        board = self.board()
        job = board.start("one", self.tmp / "proj", "k", run_inline=True)
        self.assertIn("Unknown", board.describe(str(self.tmp / "other"), job.job_id))
        self.assertIn("Unknown", board.cancel(str(self.tmp / "other"), job.job_id))

    def test_wait_returns_when_the_job_finishes(self):
        gate = threading.Event()
        board = self.board(FakeRunner(gate=gate))
        job = board.start("x", self.tmp / "proj", "", run_inline=False)
        self.assertIn("pending", board.wait(str(self.tmp / "proj"), job.job_id, 0.1))
        gate.set()
        self.assertIn("готово", board.wait(str(self.tmp / "proj"), job.job_id, 5))
        self.assertEqual(self.sent, [], "no chat key: nothing to deliver")

    def test_restart_marks_pending_jobs_interrupted_once(self):
        board = self.board()
        job = board.start("x", self.tmp / "proj", "chat-1", run_inline=True)
        saved = json.loads((self.tmp / "code" / f"{job.job_id}.json").read_text(encoding="utf-8"))
        saved.update(state=coder.PENDING, pid=999999, finished=0.0)
        (self.tmp / "code" / f"{job.job_id}.json").write_text(json.dumps(saved), encoding="utf-8")
        self.sent.clear()
        self.alive = {999999}
        self.assertEqual(self.board().reconcile_after_restart(), [], "owner process still alive")
        self.alive = set()
        lost = self.board().reconcile_after_restart()
        self.assertEqual([j.job_id for j in lost], [job.job_id])
        self.assertEqual(self.board().jobs()[0].state, coder.INTERRUPTED)
        self.assertEqual(self.board().reconcile_after_restart(), [])

    def test_cancel(self):
        board = self.board()
        job = board.start("x", self.tmp / "proj", "k", run_inline=True)
        self.assertIn("already done", board.cancel(str(self.tmp / "proj"), job.job_id))


class FakeProc:
    def __init__(self, stops_on):
        self.stops_on, self.actions, self.stopped = stops_on, [], False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.actions.append("terminate")
        self.stopped = self.stops_on == "terminate"

    def kill(self):
        self.actions.append("kill")
        self.stopped = True


class CancelEscalationTests(BoardTests):
    def test_terminate_then_kill(self):
        for stops_on, expected in (("terminate", ["terminate"]), ("kill", ["terminate", "kill"])):
            now = [0.0]
            board = self.board()
            board._clock = lambda: now[0]
            board._sleep = lambda s: now.__setitem__(0, now[0] + s)
            job = coder.CodeJob("code-1", "claude", str(self.tmp), str(self.tmp), "b", "base", "s", "x", "", 0, 1)
            proc = FakeProc(stops_on)
            board._procs[job.job_id] = proc
            board._cancel(job)
            self.assertEqual(proc.actions, expected)
            self.assertTrue(any("cancel" in c for c in self.runner.calls))


class GateTests(unittest.TestCase):
    def test_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = store.RelayPaths(root=Path(tmp) / "a2a_relay", audit=Path(tmp) / "audit.jsonl")
            paths.ensure()
            policy = trust.TrustPolicy(lambda: paths.root)
            approved = [False]
            gate = trust.TrustGate(policy, trust.CommandRules(lambda: Path(tmp)), paths.trust_journal,
                                   is_a2a=lambda: True, owner_said_yes=lambda: approved[0])
            for level, send_allowed in ((0, True), (1, True), (2, True), (3, False), (4, False)):
                policy.set_level(trust.level_by_token(str(level)))
                for tool in ("code_send", "code_cancel"):
                    blocked = gate.on_pre_tool_call(tool_name=tool, args={"task": "x"}) is not None
                    self.assertEqual(blocked, not send_allowed, (level, tool))
                self.assertIsNone(gate.on_pre_tool_call(tool_name="code_task", args={}), level) if level != 4 else None
            approved[0] = True
            self.assertIsNone(gate.on_pre_tool_call(tool_name="code_send", args={"task": "x"}), "4 after yes = 2")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.relay = self.tmp / "home" / "a2a_relay"
        self.relay.mkdir(parents=True)
        self.root = self.tmp / "dev"
        self.repo = make_repo(self.root / "proj")
        self.cwd = str(self.repo)
        (self.relay / "coder.json").write_text(json.dumps({"roots": [str(self.root)]}), encoding="utf-8")
        self.started = []

        test = self

        class Board:
            def __init__(self, *args, **kwargs):
                pass

            def reconcile_after_restart(self):
                return []

            def start(self, task, project, key, executor="", session="", run_inline=False):
                test.started.append((task, project, key))
                return coder.CodeJob("code-1", "claude", str(project), "wt", "a2a/code-1", "b", "s", task, key, 0, 1)

        self.tools = coder_tools.CodingTools(lambda: self.relay, lambda k, t: True,
                                             owner_chat_key=lambda sid: "chat" if sid == "owner" else "",
                                             session_cwd=lambda: self.cwd, board_factory=Board)

    def tearDown(self):
        self._tmp.cleanup()

    def test_project_comes_from_the_session(self):
        out = self.tools.send({"task": "fix"}, session_id="owner")
        self.assertIn("Started code-1", out)
        self.assertEqual(self.started[0][1:], (self.repo, "chat"))
        self.assertIn("arrive in this chat", out)

    def test_peer_session_is_told_to_poll(self):
        self.assertIn("code_wait", self.tools.send({"task": "fix"}, session_id="a2a-session"))

    def test_session_without_a_project_gets_nothing(self):
        self.cwd = str(self.tmp)
        self.assertIn("outside the allowed roots", self.tools.send({"task": "fix"}, session_id="owner"))
        self.cwd = ""
        self.assertIn("no working directory", self.tools.send({"task": "fix"}, session_id="owner"))
        self.assertEqual(self.started, [])

    def test_no_config_means_off(self):
        (self.relay / "coder.json").unlink()
        self.assertIn("are off", self.tools.send({"task": "fix"}, session_id="owner"))


if __name__ == "__main__":
    unittest.main()
