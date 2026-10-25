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
from .coder_tools import CodingTools
from .feed import FeedRelay, owner_chat_alive
from .hold import TaskHold, level_note
from .identity import SECTION_ID, MachineIdentity
from .jobs import JobBoard
from .replies import ReplyInterpreter
from .restart import GatewayRestart
from .store import RelayPaths
from .trust import CommandRules, TrustCommand, TrustGate, TrustPolicy, current_session_is_a2a

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

    def profile_home():
        # Read per call: a forwarded profile's turn runs under its own HERMES_HOME override.
        from hermes_constants import get_hermes_home
        return get_hermes_home()

    policy = TrustPolicy(lambda: profile_home() / "a2a_relay")
    hold: TaskHold

    def owner_said_yes() -> bool:
        from gateway.session_context import get_session_env
        return hold.is_approved(get_session_env("HERMES_SESSION_CHAT_ID", "") or "")

    gate = TrustGate(policy, CommandRules(profile_home), paths.trust_journal,
                     owner_said_yes=owner_said_yes)
    hold = TaskHold(paths, gate, owner_present=lambda: owner_chat_alive(paths))
    trust_command = TrustCommand(policy, paths.trust_journal)
    bridge = ApprovalBridge(paths, auto_decide=lambda command, keys: (
        gate.auto_decision(command, keys) if current_session_is_a2a() else None))
    desk = DecisionDesk(paths)
    replies = ReplyInterpreter(desk)
    feed = FeedRelay(paths, inject=inject, can_inject=chats.can_inject, on_asked=replies.note_asked)
    mail = _register_mail(ctx, chats, inject, profile_home)

    def on_pre_llm_call(user_message=None, platform: str = "", session_id: str = "", **_):
        if platform == "a2a" or current_session_is_a2a():
            level = gate.level()
            return None if level.neutral_framing else {"context": level_note(level)}
        key = chats.key_for_turn(platform, session_id)
        if not key:
            return None
        chats.remember(session_id, key)
        feed.claim_chat(key)
        _ensure_poller(feed)
        text = user_message if isinstance(user_message, str) else ""
        feed.note_turn(text)  # the relay's own turn: its prompts are on screen now
        context = mail.on_turn(session_id, key, text) or replies.on_user_message(text)
        return {"context": context} if context else None

    def on_pre_tool_call(tool_name: str = "", session_id: str = "", args=None, **_):
        owner = chats.is_owner_session(session_id)
        if tool_name in _A2A_TOOLS and owner:
            feed.note_own_call(True)
        peer_turn = mail.turns.get(session_id) if owner else None
        return gate.on_pre_tool_call(tool_name=tool_name, args=args, peer_turn=peer_turn)

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
        return restarter.request(announce_to=chats.key_for_session(session_id), force=args.get("force") is True)

    _register_tools(ctx, a2a_send, a2a_task, gateway_restart)
    coding = CodingTools(lambda: profile_home() / "a2a_relay", inject, chats.key_for_session)
    coding.register(ctx, _schema)
    coding.reconcile_in_background()
    restarter.announce_in_background()
    ctx.register_cli_command(name="a2a-relay", help="Check and set up A2A between your machines",
                             setup_fn=build_parser, handler_fn=cli_command,
                             description="doctor: read-only checks with exact fix commands; setup: apply them.")
    ctx.register_system_prompt_section(SECTION_ID, MachineIdentity().render)
    ctx.register_hook("pre_approval_request", bridge.on_pre_approval_request)
    ctx.register_hook("post_approval_response", bridge.on_post_approval_response)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("pre_gateway_dispatch", hold.on_pre_gateway_dispatch)
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
    ctx.register_command("a2a-trust", handler=command(trust_command.handle), args_hint="[level|allow <glob>]",
                         description="Show or set this profile's A2A trust level (0 = most autonomous, strict)")
    logger.info("a2a-relay: registered (state in %s)", paths.root)


def _schema(name: str, description: str, properties: dict, required: list) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}


def _register_tools(ctx, a2a_send, a2a_task, gateway_restart) -> None:
    schema = _schema
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
                   "restart another machine, ask its agent (a2a_call) to call this tool. Refuses while a chat waits "
                   "for the owner's answer (a question or an approval): a restart would interrupt it.")
    force = {"type": "boolean", "description": "Restart even though chats wait for the owner; only when the owner "
                                               "agreed to interrupt them."}
    ctx.register_tool(name="gateway_restart", toolset="a2a-relay", handler=gateway_restart, description=restart_doc,
                      emoji="\u267b", schema=schema("gateway_restart", restart_doc, {"force": force}, []))


def _register_mail(ctx, chats: OwnerChats, inject, profile_home):
    """#5 and #6: sessions_list, session_send, session_reply. Returns the process-wide SessionMail."""
    from pathlib import Path
    from .cli import build_fleet
    from .mail import MailStore, PeerTurns, SessionMail
    from .restart import _gateway_runner
    from .sessions import (FleetAuth, LocalSessions, SessionDirectory, SessionServer, _process_guard,
                           _profile_name, sessions_url)

    def fleet():
        return build_fleet()

    def machine() -> str:
        try:
            return fleet().me
        except ValueError:
            return ""

    def members() -> dict:
        try:
            return fleet().members()
        except ValueError:
            return {}

    def address():
        url = members().get(machine())
        if not url:
            return None
        from urllib.parse import urlsplit
        parts = urlsplit(sessions_url(url))
        return (parts.hostname, parts.port) if parts.hostname and parts.port else None

    local = LocalSessions(machine)
    auth = FleetAuth(fleet)
    directory = SessionDirectory(local, auth, members)
    # Every served profile loads its own plugin copy; the one server and the mail are shared per process,
    # with each profile's injector and trust level registered here.
    home = Path(getattr(ctx._manager, "home_path", None) or profile_home())
    guard = _process_guard()
    with guard.lock:
        guard.injectors[_profile_name(str(home))] = inject
        guard.levels = getattr(guard, "levels", {})
        guard.levels[_profile_name(str(home))] = TrustPolicy(lambda: home / "a2a_relay").level
        if guard.mail is None:
            from hermes_constants import get_default_hermes_root
            levels = guard.levels

            def level_for(profile: str):
                level = levels.get(profile) or levels.get("default")
                return level() if level else TrustPolicy(lambda: home / "a2a_relay").level()

            guard.mail = SessionMail(MailStore(get_default_hermes_root() / "a2a_relay" / "mail"), directory, auth,
                                     members, guard.injectors.get, level_for, PeerTurns(guard.turns))
            threading.Thread(target=_sweep_forever, args=(guard.mail,), daemon=True, name="a2a-relay-mail").start()
    mail = guard.mail
    SessionServer(local, auth, lambda: _process_guard().mail).start_when_gateway(_gateway_runner, address)

    def sessions_list(args: dict, **_) -> str:
        idle = args.get("idle")
        try:
            return directory.describe(machine=str(args.get("machine") or "").strip(),
                                      repo=str(args.get("repo") or "").strip(),
                                      profile=str(args.get("profile") or "").strip(),
                                      idle=idle if isinstance(idle, bool) else None)
        except ValueError as exc:
            return f"Error: {exc}"

    doc = ("List the owner's open Hermes chat sessions on every fleet machine (this one included): machine, "
           "profile, idle/working, repo@branch or cwd, title, session id, last message. Use it to find the "
           "chat that works on a given project before handing it work. Unreachable machines are listed too.")
    text = lambda description: {"type": "string", "description": description}  # noqa: E731
    ctx.register_tool(name="sessions_list", toolset="a2a-relay", handler=sessions_list, description=doc,
                      emoji="\U0001f5c2", schema=_schema("sessions_list", doc, {
                          "machine": text("Only this machine (A2A node name, e.g. m4)."),
                          "repo": text("Only sessions whose git repo name contains this."),
                          "profile": text("Only this Hermes profile (default, growser, ...)."),
                          "idle": {"type": "boolean", "description": "true: only idle chats; false: only busy ones."},
                      }, []))

    def session_send(args: dict, session_id: str = "", **_) -> str:
        return mail.send(chats.key_for_session(session_id), session_id, _profile_name(str(profile_home())),
                         str(args.get("session") or ""), str(args.get("text") or ""),
                         machine=str(args.get("machine") or ""))

    def session_reply(args: dict, session_id: str = "", **_) -> str:
        key = chats.key_for_session(session_id)
        if not key:
            return "Error: session_reply works only in the owner chat that received the task."
        return mail.reply(key, str(args.get("id") or ""), str(args.get("text") or ""))

    send_doc = ("Hand a task to an OPEN owner chat on any fleet machine (find it with sessions_list): it runs there "
                "in a chat Dmitry sees, in that project's context, and its agent answers with session_reply; the "
                "answer arrives in THIS chat as a separate message. Returns at once; do not wait. Prefer it to "
                "a2a_send when a chat already works on the project. A busy chat starts it after its current turn.")
    ctx.register_tool(name="session_send", toolset="a2a-relay", handler=session_send, description=send_doc,
                      emoji="\U0001f4ec", schema=_schema("session_send", send_doc, {
                          "session": text("Target chat: its session id from sessions_list (or a unique tail)."),
                          "text": text("The task, self-contained: the other chat knows nothing of this one."),
                          "machine": text("Optional: that chat's machine (A2A node name), to disambiguate."),
                      }, ["session", "text"]))
    reply_doc = ("Answer a task another chat sent here with session_send (its message names the id): one message "
                 "with the result, or why it cannot be done. Only the chat that received the task can answer it.")
    ctx.register_tool(name="session_reply", toolset="a2a-relay", handler=session_reply, description=reply_doc,
                      emoji="\U0001f4ec", schema=_schema("session_reply", reply_doc, {
                          "id": text("The task id (mail-...) from the task message."),
                          "text": text("The answer for the sender."),
                      }, ["id", "text"]))
    return mail


def _sweep_forever(mail, every: float = 60.0) -> None:
    import time
    while True:
        time.sleep(every)
        try:
            mail.sweep()
        except Exception:
            logger.warning("a2a-relay: mail sweep failed", exc_info=True)


def _ensure_poller(feed: FeedRelay) -> None:
    """Start the feed poller once, lazily: only processes that run TUI turns ever need it."""
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        _poller_started = True
    threading.Thread(target=feed.run, args=(threading.Event(),), daemon=True, name="a2a-relay-feed").start()
    logger.info("a2a-relay: feed poller started")
