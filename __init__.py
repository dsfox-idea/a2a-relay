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
from .feed import TUI_PLATFORMS, FeedRelay
from .identity import SECTION_ID, MachineIdentity
from .replies import ReplyInterpreter
from .store import RelayPaths

logger = logging.getLogger("a2a_relay")

_A2A_TOOLS = frozenset({"a2a_call", "a2a_orchestrate"})
_poller_lock = threading.Lock()
_poller_started = False


def register(ctx) -> None:
    paths = RelayPaths()
    paths.ensure()
    manager = ctx._manager

    def is_tui_process() -> bool:
        return bool(manager.has_tui_message_injector)

    def inject(session_key: str, text: str) -> bool:
        return bool(ctx.inject_message(text, session_key=session_key))

    bridge = ApprovalBridge(paths)
    desk = DecisionDesk(paths)
    replies = ReplyInterpreter(desk)
    feed = FeedRelay(paths, inject=inject, is_tui_process=is_tui_process, on_asked=replies.note_asked)

    def on_pre_llm_call(user_message=None, platform: str = "", **kwargs):
        feed.on_pre_llm_call(platform=platform, **kwargs)
        _ensure_poller(feed)
        if platform in TUI_PLATFORMS and is_tui_process():
            context = replies.on_user_message(user_message)
            if context:
                return {"context": context}
        return None

    def on_pre_tool_call(tool_name: str = "", **_):
        if tool_name in _A2A_TOOLS and is_tui_process():
            feed.note_own_call(True)

    def on_post_tool_call(tool_name: str = "", **_):
        if tool_name in _A2A_TOOLS and is_tui_process():
            feed.note_own_call(False)

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
            feed.on_pre_llm_call(session_id=get_session_env("HERMES_SESSION_KEY", ""), platform=platform)
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


def _ensure_poller(feed: FeedRelay) -> None:
    """Start the feed poller once, lazily: only processes that run TUI turns ever need it."""
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        _poller_started = True
    threading.Thread(target=feed.run, args=(threading.Event(),), daemon=True, name="a2a-relay-feed").start()
    logger.info("a2a-relay: feed poller started")
