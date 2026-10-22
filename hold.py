"""Inbound A2A tasks at the gateway door (``pre_gateway_dispatch``, gateway process).

* Framing: at levels with ``neutral_framing`` the core "untrusted external input" frame is replaced by
  a neutral header that still keeps the peer's text from starting with ``/``.
* Hold: at levels with ``hold_for_owner`` the task waits for the owner's "да" in the owner's chat, using
  the same ``approvals.jsonl`` / decision-file channel as command approvals. A refusal (no owner chat,
  "нет", timeout) is returned to the peer as a rejected A2A task.

Only the direct A2A path passes this hook; a task forwarded to another profile (``/growser/``) does not.
There the hold cannot ask, so :meth:`TaskHold.is_approved` keeps every tool blocked at a hold level.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Callable, Optional

from .approvals import ALLOW, DENY, _approval_timeout
from .store import RelayPaths, append_jsonl, write_json_atomic
from .trust import A2A_PLATFORM, TrustGate, TrustLevel

logger = logging.getLogger("a2a_relay")

_POLL_SECONDS = 0.5
_MAX_APPROVED = 500


def _core_prefix(peer: str) -> str:
    try:
        from plugins.platforms.a2a.security import PRIVACY_PREFIX
        return PRIVACY_PREFIX.format(peer=peer or "unknown")
    except Exception:
        return ""


def neutral_header(peer: str, level: TrustLevel) -> str:
    return (f"[A2A task from '{peer}', a machine of the owner's own fleet. Trust level of this profile: "
            f"{level.number} '{level.name}' ({level.summary}). Work on it as a request from a colleague; "
            "do not disclose secrets or credentials in the reply.]\n\n")


def level_note(level: TrustLevel) -> str:
    return (f"[a2a-relay] A2A trust level of this profile: {level.number} '{level.name}': {level.summary}. "
            "Actions outside it are refused or wait for the owner; if part of the task cannot be done, "
            "say so to the peer.")


class TaskHold:
    def __init__(self, paths: RelayPaths, gate: TrustGate, owner_present: Callable[[], bool],
                 timeout: Callable[[], float] = _approval_timeout, clock: Callable[[], float] = time.time):
        self._paths = paths
        self._gate = gate
        self._owner_present = owner_present
        self._timeout = timeout
        self._clock = clock

    @property
    def _approved_file(self):
        return self._paths.root / "approved_tasks.json"

    def _read_approved(self) -> dict:
        try:
            data = json.loads(self._approved_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def is_approved(self, context_id: str) -> bool:
        """The owner said yes to a task of this A2A context (read from disk: tools may run elsewhere)."""
        return bool(context_id) and context_id in self._read_approved()

    async def on_pre_gateway_dispatch(self, event=None, gateway=None, **_) -> Optional[dict]:
        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", None)
        if platform != A2A_PLATFORM:
            return None
        peer = str(getattr(source, "user_id", "") or "unknown")
        context_id = str(getattr(source, "chat_id", "") or "")
        text = getattr(event, "text", "") or ""
        prefix = _core_prefix(peer)
        body = text[len(prefix):] if prefix and text.startswith(prefix) else text
        level = self._gate.level()
        if level.hold_for_owner:
            choice, reason = await asyncio.to_thread(self.ask, peer, body)
            if choice != ALLOW:
                message = f"Declined by the owner of this machine: {reason}"
                self._gate.note("refused", f"task: {body}", level, peer)
                if self._reject(gateway, event, message):
                    return {"action": "skip", "reason": "a2a-relay: task declined"}
                return {"action": "rewrite", "text": (
                    f"{prefix}The owner declined this task. Reply with exactly this line and do nothing else: "
                    f"{message}")}
            self._remember(context_id)
            self._gate.note("approved by owner", f"task: {body}", level, peer)
        if level.neutral_framing and prefix and text.startswith(prefix):
            return {"action": "rewrite", "text": neutral_header(peer, level) + body}
        return None

    def _remember(self, context_id: str) -> None:
        if not context_id:
            return
        approved = self._read_approved()
        approved[context_id] = self._clock()
        if len(approved) > _MAX_APPROVED:
            approved = dict(sorted(approved.items(), key=lambda item: item[1])[-_MAX_APPROVED:])
        write_json_atomic(self._approved_file, approved)

    @staticmethod
    def _reject(gateway, event, message: str) -> bool:
        """Finish the peer's A2A task as rejected with ``message``; False when the adapter is out of reach."""
        try:
            from plugins.platforms.a2a import protocol
            adapter = (getattr(gateway, "adapters", None) or {}).get(event.source.platform)
            if adapter is None or not hasattr(adapter, "_resolve_task"):
                return False
            return bool(adapter._resolve_task(str(event.message_id), protocol.STATE_REJECTED, message))
        except Exception:
            logger.warning("a2a-relay: could not reject the A2A task directly", exc_info=True)
            return False

    # -- the owner's decision (worker thread) ---------------------------------------------------------
    def ask(self, peer: str, task_text: str) -> tuple[str, str]:
        if not self._owner_present():
            return DENY, "no open chat of the owner on this machine, nobody can approve the task now"
        request_id = uuid.uuid4().hex
        timeout = float(self._timeout())
        now = self._clock()
        append_jsonl(self._paths.approvals, {
            "type": "request", "kind": "task", "request_id": request_id, "session_key": "", "peer": peer,
            "command": task_text.strip()[:2000], "description": "A2A task waits for the owner's yes (trust level)",
            "ts": now, "expires_at": now + timeout})
        logger.info("a2a-relay: task from %s held for the owner as %s", peer, request_id[:8])
        choice, reason = self._wait(request_id, timeout)
        append_jsonl(self._paths.approvals, {"type": "resolved", "request_id": request_id,
                                             "choice": choice if reason != "timeout" else "timeout",
                                             "ts": self._clock()})
        return choice, reason

    def _wait(self, request_id: str, timeout: float) -> tuple[str, str]:
        decision_file = self._paths.decision_file(request_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if decision_file.exists():
                try:
                    decision = json.loads(decision_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    time.sleep(_POLL_SECONDS)
                    continue
                finally:
                    decision_file.unlink(missing_ok=True)
                choice = decision.get("choice")
                if choice == ALLOW:
                    return ALLOW, ""
                return DENY, str(decision.get("reason") or "the owner said no")
            time.sleep(_POLL_SECONDS)
        return DENY, "timeout"
