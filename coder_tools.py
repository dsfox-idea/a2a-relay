"""Tool handlers for coding executors: ``code_send``, ``code_task``, ``code_cancel``, ``code_wait``.

The project always comes from the calling session (its recorded working directory), never from a tool argument,
so a session can only hand work to its own project. Trust levels are enforced by ``TrustGate`` (level 3 blocks
``code_send``/``code_cancel``; level 4 needs the owner's yes first).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from .coder import CodeBoard, CoderConfig, CoderError, MAX_WAIT_SECONDS, ProjectScope

logger = logging.getLogger("a2a_relay")


def _session_cwd() -> str:
    from hermes_cli.plugin_session import session_cwd
    return session_cwd()


class CodingTools:
    def __init__(self, relay_root: Callable[[], Path], inject: Callable[[str, str], bool],
                 owner_chat_key: Callable[[str], str], session_cwd: Callable[[], str] = _session_cwd,
                 board_factory: Callable[..., CodeBoard] = CodeBoard):
        self._relay_root = relay_root
        self._inject = inject
        self._owner_chat_key = owner_chat_key
        self._session_cwd = session_cwd
        self._board_factory = board_factory
        self._boards: dict[str, CodeBoard] = {}
        self._lock = threading.Lock()

    def _config(self) -> CoderConfig:
        return CoderConfig.load(self._relay_root() / "coder.json")

    def board(self) -> CodeBoard:
        """One board per profile home (a forwarded profile's turn runs under its own HERMES_HOME)."""
        root = self._relay_root()
        with self._lock:
            board = self._boards.get(str(root))
            if board is None:
                board = self._board_factory(root / "code", lambda: CoderConfig.load(root / "coder.json"),
                                            self._inject)
                self._boards[str(root)] = board
                board.reconcile_after_restart()
            return board

    def reconcile_in_background(self) -> None:
        """At plugin load: report jobs a previous gateway process left unfinished (delivery retries for minutes)."""
        if not (self._relay_root() / "code").is_dir():
            return
        threading.Thread(target=self.board, daemon=True, name="a2a-relay-code-reconcile").start()

    def _project(self) -> Path:
        return ProjectScope(self._config().roots).project_of(self._session_cwd())

    def send(self, args: dict, session_id: str = "", **_) -> str:
        try:
            project = self._project()
            key = self._owner_chat_key(session_id)
            job = self.board().start(str(args.get("task") or ""), project, key,
                                     executor=str(args.get("executor") or "").strip(),
                                     session=str(args.get("session") or "").strip())
        except CoderError as exc:
            return f"Error: {exc}"
        where = ("the result will arrive in this chat as a separate message; do not wait for it"
                 if key else f"poll code_wait(job='{job.job_id}') until it finishes, then report the result")
        return (f"Started {job.job_id}: {job.executor} works in {job.worktree} on branch {job.branch} "
                f"(session {job.session}); {where}. code_task shows progress, code_cancel stops it.")

    def task(self, args: dict, **_) -> str:
        try:
            return self.board().describe(str(self._project()), str(args.get("job") or "").strip())
        except CoderError as exc:
            return f"Error: {exc}"

    def cancel(self, args: dict, **_) -> str:
        job = str(args.get("job") or "").strip()
        if not job:
            return "Error: 'job' is required."
        try:
            return self.board().cancel(str(self._project()), job)
        except CoderError as exc:
            return f"Error: {exc}"

    def wait(self, args: dict, **_) -> str:
        job = str(args.get("job") or "").strip()
        if not job:
            return "Error: 'job' is required."
        try:
            seconds = float(args.get("seconds") or MAX_WAIT_SECONDS)
        except (TypeError, ValueError):
            seconds = MAX_WAIT_SECONDS
        try:
            return self.board().wait(str(self._project()), job, seconds)
        except CoderError as exc:
            return f"Error: {exc}"

    def register(self, ctx, schema: Callable[..., dict]) -> None:
        text = lambda description: {"type": "string", "description": description}  # noqa: E731
        job = {"job": text("Job id from code_send.")}
        send_doc = ("Hand a coding task to an external coding agent (Claude Code, OpenCode, ...) through acpx. "
                    "It works in YOUR OWN project only (the git repo of this session's working directory), in a "
                    "fresh git worktree on branch a2a/<job>, and returns a job id at once. Write a complete brief: "
                    "the agent cannot ask questions. Pass 'session' from an earlier job to continue its context.")
        tools = (
            ("code_send", self.send, send_doc,
             {"task": text("Complete brief for the coding agent."),
              "executor": text("Optional: executor name from coder.json (default: its default_executor)."),
              "session": text("Optional: session of an earlier job of this project, to continue it.")}, ["task"]),
            ("code_task", self.task, "State of this project's coding jobs; pass a job id for its full result.",
             {"job": text("Optional job id.")}, []),
            ("code_cancel", self.cancel, "Cancel a running coding job of this project.", job, ["job"]),
            ("code_wait", self.wait,
             f"Block up to {MAX_WAIT_SECONDS} s until a coding job finishes and return its state; for sessions "
             "whose result cannot be delivered into a chat (A2A tasks).",
             {**job, "seconds": {"type": "number", "description": f"Max wait, <= {MAX_WAIT_SECONDS}."}}, ["job"]),
        )
        for name, handler, doc, props, required in tools:
            ctx.register_tool(name=name, toolset="a2a-relay", handler=handler, description=doc, emoji="\U0001f6e0",
                              schema=schema(name, doc, props, required))
