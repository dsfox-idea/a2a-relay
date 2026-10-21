"""a2a-relay: mirror inbound A2A traffic into the active TUI chat and route A2A approval prompts
to the user.

Two processes share ``$HERMES_HOME/a2a_relay/``:
* the messaging gateway (serves inbound A2A) publishes approval prompts and applies decisions;
* the Ink TUI process injects feed messages into the most recently active chat and owns the
  ``/a2a-allow``, ``/a2a-deny`` and ``/a2a-pending`` commands.
"""

from __future__ import annotations

import logging
import threading

from .approvals import ALLOW, DENY, ApprovalBridge, DecisionDesk
from .chats import OwnerChats
from .cli import build_parser, command as cli_command
from .feed import FeedRelay
from .identity import SECTION_ID, MachineIdentity
from .jobs import JobBoard
from .replies import ReplyInterpreter
from .restart import GatewayRestart
from .store import RelayPaths

logger = logging.getLogger("a2a_relay")

_A2A_TOOLS = frozenset({"a2a_call", "a2a_orchestrate"})
_poller_lock = threading.Lock()
_poller_started = False


def register(ctx) -> None:
    paths = RelayPaths()
    paths.ensure()
    manager = ctx._manager

    chats = OwnerChats(has_tui_host=lambda: bool(manager.has_tui_message_injector),
                       has_gateway_host=lambda: bool(manager.has_gateway_message_injector))

    def inject(session_key: str, text: str) -> bool:
        return bool(ctx.inject_message(text, session_key=session_key))

    bridge = ApprovalBridge(paths)
    desk = DecisionDesk(paths)
    replies = ReplyInterpreter(desk)
    feed = FeedRelay(paths, inject=inject, can_inject=chats.can_inject, on_asked=replies.note_asked)

    def on_pre_llm_call(user_message=None, platform: str = "", session_id: str = "", **_):
        key = chats.key_for_turn(platform, session_id)
        if not key:
            return None
        chats.remember(session_id, key)
        feed.claim_chat(key)
        _ensure_poller(feed)
        context = replies.on_user_message(user_message if isinstance(user_message, str) else "")
        return {"context": context} if context else None

    def on_pre_tool_call(tool_name: str = "", session_id: str = "", **_):
        if tool_name in _A2A_TOOLS and chats.is_owner_session(session_id):
            feed.note_own_call(True)

    def on_post_tool_call(tool_name: str = "", session_id: str = "", **_):
        if tool_name in _A2A_TOOLS and chats.is_owner_session(session_id):
            feed.note_own_call(False)

    jobs = JobBoard(inject=inject, on_call=feed.note_own_call)

    def a2a_send(args: dict, session_id: str = "", **_) -> str:
        agent = str(args.get("agent") or "").strip()
        message = str(args.get("message") or "").strip()
        if not agent or not message:
            return "Error: both 'agent' and 'message' are required."
        key = chats.key_for_session(session_id)
        if not key:
            return ("Error: a2a_send works only from the owner's chat (its reply is delivered there). "
                    "In an A2A session use a2a_call.")
        try:
            job = jobs.start(agent, message, key, str(args.get("context_id") or "").strip())
        except ValueError as exc:
            return f"Error: {exc}"
        return (f"Sent to {agent} as {job.job_id}. Do not wait for it and do not call a2a_call for the same "
                f"task: the peer's answer will arrive in this chat as a separate message. a2a_task shows the state.")

    def a2a_task(args: dict, **_) -> str:
        return jobs.describe(str(args.get("job") or "").strip())

    restarter = GatewayRestart(paths.root / "restart_notice.json", inject)

    def gateway_restart(args: dict, session_id: str = "", **_) -> str:
        return restarter.request(announce_to=chats.key_for_session(session_id))

    _register_tools(ctx, a2a_send, a2a_task, gateway_restart)
    restarter.announce_in_background()
    ctx.register_cli_command(name="a2a-relay", help="Check and set up A2A between your machines",
                             setup_fn=build_parser, handler_fn=cli_command,
                             description="doctor: read-only checks with exact fix commands; setup: apply them.")
    ctx.register_system_prompt_section(SECTION_ID, MachineIdentity().render)
    ctx.register_hook("pre_approval_request", bridge.on_pre_approval_request)
    ctx.register_hook("post_approval_response", bridge.on_post_approval_response)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    def claim_chat() -> None:
        """Typing any /a2a-* command makes this chat the feed target, even before its first turn."""
        try:
            from gateway.session_context import get_session_env
            # The TUI binds only ``source`` (not ``platform``) around slash-command handlers.
            platform = get_session_env("HERMES_SESSION_PLATFORM", "") or get_session_env("HERMES_SESSION_SOURCE", "")
            key = chats.key_for_turn(platform, get_session_env("HERMES_SESSION_KEY", ""))
            if key:
                feed.claim_chat(key)
                _ensure_poller(feed)
        except Exception:
            logger.warning("a2a-relay: could not claim the chat from a slash command", exc_info=True)

    def command(fn):
        def handler(raw: str) -> str:
            claim_chat()
            return fn(raw)
        return handler

    ctx.register_command("a2a-allow", handler=command(lambda raw: desk.decide(raw, ALLOW)), args_hint="[id]",
                         description="Approve a pending A2A request")
    ctx.register_command("a2a-deny", handler=command(lambda raw: desk.decide(raw, DENY)),
                         args_hint="[id] [reason]", description="Deny a pending A2A request")
    ctx.register_command("a2a-pending", handler=command(lambda raw: desk.describe_pending()),
                         description="List A2A requests waiting for your decision; makes this chat the A2A feed")
    logger.info("a2a-relay: registered (state in %s)", paths.root)


def _register_tools(ctx, a2a_send, a2a_task, gateway_restart) -> None:
    def schema(name: str, description: str, properties: dict, required: list) -> dict:
        return {"name": name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required}}

    text = lambda description: {"type": "string", "description": description}  # noqa: E731
    send_doc = ("Hand a task to a peer agent WITHOUT waiting: returns a job id at once, and the peer's answer "
                "arrives later in this chat as a separate message. Use it for anything that may take longer "
                "than a few minutes (builds, test runs, investigations); a2a_call blocks and is cut at the "
                "tool timeout (420 s by default), losing the answer.")
    ctx.register_tool(name="a2a_send", toolset="a2a-relay", handler=a2a_send, description=send_doc, emoji="\U0001f4e8",
                      schema=schema("a2a_send", send_doc,
                                    {"agent": text("Configured peer name (from a2a_agents) or a full http(s):// URL."),
                                     "message": text("The task for the peer, in natural language."),
                                     "context_id": text("Optional: context id of an earlier exchange to continue.")},
                                    ["agent", "message"]))
    task_doc = "State of tasks sent with a2a_send from this machine; pass a job id for its full answer."
    ctx.register_tool(name="a2a_task", toolset="a2a-relay", handler=a2a_task, description=task_doc, emoji="\U0001f4e8",
                      schema=schema("a2a_task", task_doc, {"job": text("Optional job id from a2a_send.")}, []))
    restart_doc = ("Restart THIS machine's Hermes gateway safely (to load new plugin code, .env tokens or config). "
                   "Waits for running turns, this one included, then restarts; the new process is up 10-30 s "
                   "after your turn ends, and from your own chat you get a notice when it is back. Never run "
                   "`hermes gateway restart` in the terminal instead: that kills the gateway you run in. To "
                   "restart another machine, ask its agent (a2a_call) to call this tool.")
    ctx.register_tool(name="gateway_restart", toolset="a2a-relay", handler=gateway_restart, description=restart_doc,
                      emoji="\u267b", schema=schema("gateway_restart", restart_doc, {}, []))


def _ensure_poller(feed: FeedRelay) -> None:
    """Start the feed poller once, lazily: only processes that run TUI turns ever need it."""
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        _poller_started = True
    threading.Thread(target=feed.run, args=(threading.Event(),), daemon=True, name="a2a-relay-feed").start()
    logger.info("a2a-relay: feed poller started")
