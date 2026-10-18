"""Which turn runs in one of the owner's interactive chats on this machine, and the key to inject into it.

Two hosts serve such chats:
* a separate Ink TUI / desktop process (macOS, Linux): the agent's platform is ``tui``/``desktop`` and the
  host's TUI injector routes by the session id the turn reports;
* the messaging gateway itself (Windows): the chat is a ``local`` gateway session, so the injection key is
  the gateway session key (``agent:<profile>:local:...``) and the gateway injector delivers it.

Inbound A2A sessions and messaging chats are never feed targets.
"""

from __future__ import annotations

from typing import Callable

TUI_PLATFORMS = frozenset({"tui", "desktop"})
GATEWAY_CHAT_PLATFORM = "local"
A2A_SESSION_MARKER = ":a2a:"


def _session_env(name: str) -> str:
    from gateway.session_context import get_session_env
    return get_session_env(name, "") or ""


class OwnerChats:
    _MAX_REMEMBERED = 200

    def __init__(self, has_tui_host: Callable[[], bool], has_gateway_host: Callable[[], bool],
                 session_env: Callable[[str], str] = _session_env):
        self._has_tui_host = has_tui_host
        self._has_gateway_host = has_gateway_host
        self._env = session_env
        # Agent session ids seen in owner-chat turns: tool hooks get only the session id, not the platform.
        self._owner_session_ids: dict[str, None] = {}

    def remember(self, session_id: str) -> None:
        if session_id:
            self._owner_session_ids[session_id] = None
            while len(self._owner_session_ids) > self._MAX_REMEMBERED:
                self._owner_session_ids.pop(next(iter(self._owner_session_ids)))

    def is_owner_session(self, session_id: str) -> bool:
        return bool(session_id) and session_id in self._owner_session_ids

    def can_inject(self) -> bool:
        return bool(self._has_tui_host() or self._has_gateway_host())

    def key_for_turn(self, platform: str = "", session_id: str = "") -> str:
        """Injection key of the chat the current turn runs in, or "" when it is not an owner chat."""
        if self._has_tui_host() and platform in TUI_PLATFORMS:
            return session_id or ""
        if self._has_gateway_host() and self._env("HERMES_SESSION_PLATFORM") == GATEWAY_CHAT_PLATFORM:
            key = self._env("HERMES_SESSION_KEY")
            if key and A2A_SESSION_MARKER not in key:
                return key
        return ""
