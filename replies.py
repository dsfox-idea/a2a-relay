"""Plain "да"/"нет" answers to A2A approval prompts.

The decision is made here, in code, from the user's own message text — never by the model — so a
peer's text cannot talk the agent into approving. A reply counts only when it is the user's first
message after an approval prompt was delivered to the chat and exactly one asked request is pending.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from .approvals import ALLOW, DENY, DecisionDesk

logger = logging.getLogger("a2a_relay")

_RELAY_HEADER = "[a2a-relay:"
_YES = re.compile(r"^\s*(да|ага|разрешаю|разреши|можно|давай|ок|ok|yes|y)\s*[.!)]*\s*$", re.IGNORECASE)
_NO = re.compile(r"^\s*(нет|не надо|не разрешаю|отклоняю|отклони|запрещаю|no|n)\b[\s,.:;!—-]*(.*)$",
                 re.IGNORECASE | re.DOTALL)


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
        if not isinstance(text, str) or text.lstrip().startswith(_RELAY_HEADER):
            return None  # relay's own injected turn
        with self._lock:
            asked, self._asked = set(self._asked), set()  # any user message ends the "just asked" window
        if not asked:
            return None
        answer = parse_answer(text)
        if answer is None:
            return None
        pending = [r for r in self._desk.pending() if r["request_id"] in asked]
        if not pending:
            return "[a2a-relay] Ответ Дмитрия не применён: запрос уже закрыт или истёк. Скажи ему об этом."
        if len(pending) > 1:
            with self._lock:
                self._asked.update(asked)
            ids = ", ".join(r["request_id"][:8] for r in pending)
            return (f"[a2a-relay] Ответ не применён: ждут решения несколько запросов ({ids}). "
                    "Попроси Дмитрия ответить командой /a2a-allow <id> или /a2a-deny <id>.")
        choice, reason = answer
        request = pending[0]
        result = self._desk.decide(f"{request['request_id']} {reason}".strip(), choice)
        logger.info("a2a-relay: plain reply applied as %s to %s", choice, request["request_id"][:8])
        return (f"[a2a-relay] Сообщение Дмитрия применено кодом плагина как решение по A2A-запросу "
                f"{request['request_id'][:8]} ({request.get('command', '')[:120]}). Итог: {result} "
                "Коротко подтверди Дмитрию, инструменты не вызывай.")
