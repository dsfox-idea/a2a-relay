"""Plain yes/no answers to A2A approval prompts.

The decision is made here, in code, from the user's own message text — never by the model — so a
peer's text cannot talk the agent into approving. A bare yes/no counts while an approval prompt shown
in this chat is still pending (until it is answered or expires) and exactly one such request is pending;
other messages in between do not close the window.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from .approvals import ALLOW, DENY, DecisionDesk
from .i18n import ALL_NO, ALL_YES, is_relay_text

logger = logging.getLogger("a2a_relay")

_YES = re.compile(r"^\s*(" + "|".join(map(re.escape, ALL_YES)) + r")\s*[.!)]*\s*$", re.IGNORECASE)
_NO = re.compile(r"^\s*(" + "|".join(map(re.escape, ALL_NO)) + r")\b[\s,.:;!—-]*(.*)$", re.IGNORECASE | re.DOTALL)


def parse_answer(text: str) -> Optional[tuple[str, str]]:
    """(choice, reason) for a bare yes/no reply, else None."""
    text = (text or "").strip()
    if _YES.match(text):
        return ALLOW, ""
    match = _NO.match(text)
    if match:
        return DENY, match.group(2).strip()
    return None


class ReplyInterpreter:
    """Tracks which approval prompts the chat was shown and applies the user's yes/no to them."""

    def __init__(self, desk: DecisionDesk):
        self._desk = desk
        self._lock = threading.Lock()
        self._asked: set[str] = set()

    def note_asked(self, request_ids: list[str]) -> None:
        with self._lock:
            self._asked.update(request_ids)

    def on_user_message(self, text: str) -> Optional[str]:
        """Context for the model when the message was handled as a decision (or was ambiguous)."""
        if not isinstance(text, str) or is_relay_text(text):
            return None  # relay's own injected turn
        answer = parse_answer(text)
        if answer is None:
            # Other text keeps the window open until the request is answered or expires: a typo before the yes
            # cost the owner a request on m5 (2026-10-24, a typo, then yes). Requests no longer pending drop out.
            with self._lock:
                if self._asked:
                    live = {r["request_id"] for r in self._desk.pending()}
                    self._asked &= live
            return None
        with self._lock:
            asked, self._asked = set(self._asked), set()
        if not asked:
            return None
        pending = [r for r in self._desk.pending() if r["request_id"] in asked]
        if not pending:
            return "[a2a-relay] The owner's answer was not applied: the request is closed or expired. Tell the owner."
        if len(pending) > 1:
            with self._lock:
                self._asked.update(asked)
            ids = ", ".join(r["request_id"][:8] for r in pending)
            return (f"[a2a-relay] The answer was not applied: several requests wait ({ids}). Ask the owner to "
                    "answer with /a2a-allow <id> or /a2a-deny <id>.")
        choice, reason = answer
        request = pending[0]
        result = self._desk.decide(f"{request['request_id']} {reason}".strip(), choice)
        logger.info("a2a-relay: plain reply applied as %s to %s", choice, request["request_id"][:8])
        return (f"[a2a-relay] Plugin code applied the owner's message as the decision on A2A request "
                f"{request['request_id'][:8]} ({request.get('command', '')[:120]}). Result: {result} "
                "Confirm it to the owner briefly; call no tools.")
