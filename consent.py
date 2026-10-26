"""The owner's yes in his own chat settles Hermes' next approval prompt for the same command.

Agents often ask in the chat ("delete the old photos on the server? yes/no"), get a yes, run the command, and
Hermes then shows its own prompt for it, which the owner no longer watches (treelume-m5: three timeouts in a
row, 2026-10-26). Owner decision 2026-10-26: a plain yes counts for one prompt of the turn it starts, when the
prompted command is, as a whole, a line, a code block or a `span` of the agent's message right before that
yes (whitespace and line continuations aside; a part of a shown command never counts). Anything
else stays asked. Decided here, in code, from the owner's own text, never by the model.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from .replies import parse_answer

logger = logging.getLogger("a2a_relay")

MIN_COMMAND = 8  # a shorter prompted command is too easy to find by chance in any message


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\\\s*\n", " ", text or "")).strip()


_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_SPAN = re.compile(r"`([^`\n]+)`")


def shown_commands(text: str) -> set[str]:
    """Every whole command the message shows: code blocks, `spans` and lines (a leading "$ " dropped)."""
    joined = re.sub(r"\\\s*\n", " ", text or "")
    pieces = [m.group(1) for m in _FENCE.finditer(joined)] + _SPAN.findall(joined) + joined.splitlines()
    pieces += [line for block in _FENCE.findall(joined) for line in block.splitlines()]
    found = set()
    for piece in pieces:
        flat = _flat(piece)
        flat = flat[2:] if flat.startswith("$ ") else flat
        if flat:
            found.add(flat)
    return found


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


def last_assistant_text(history) -> str:
    """Text of the agent's last message with text before the owner's current one."""
    for message in reversed(list(history or [])):
        if isinstance(message, dict) and message.get("role") == "assistant":
            text = _text_of(message.get("content"))
            if text.strip():
                return text
    return ""


class ChatConsent:
    """Per owner chat: whether the turn began with a plain yes, and what the agent had shown before it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._chats: dict[str, dict] = {}

    def on_turn(self, session_key: str, owner_text: str, history) -> None:
        answer = parse_answer(owner_text or "")
        with self._lock:
            if answer is not None and answer[0] == "once":
                self._chats[session_key] = {"shown": shown_commands(last_assistant_text(history)), "used": False}
            else:
                self._chats.pop(session_key, None)

    def take(self, session_key: str, command: str) -> bool:
        """True (once per yes) when ``command`` was shown verbatim right before the owner's yes."""
        wanted = _flat(command)
        if len(wanted) < MIN_COMMAND:
            return False
        with self._lock:
            state: Optional[dict] = self._chats.get(session_key)
            if state is None or state["used"] or wanted not in state["shown"]:
                return False
            state["used"] = True
        logger.info("a2a-relay: owner's yes in %s covers the prompted command: %s", session_key, wanted[:120])
        return True
