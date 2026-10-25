"""Approval side of the relay.

Gateway process: :class:`ApprovalBridge` sees every approval prompt raised inside an inbound A2A
session (``pre_approval_request`` hook), publishes it to ``approvals.jsonl`` and resolves the
waiting agent thread once the user's decision file appears.

TUI process: :class:`DecisionDesk` backs the ``/a2a-allow`` / ``/a2a-deny`` / ``/a2a-pending``
slash commands, which only the human can type.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

from .i18n import t
from .store import RelayPaths, append_jsonl, read_jsonl, write_json_atomic

logger = logging.getLogger("a2a_relay")

A2A_SESSION_MARKER = ":a2a:"
ALLOW, DENY = "once", "deny"
_POLL_SECONDS = 0.5


def _approval_timeout() -> float:
    try:
        from tools.approval_context import _get_approval_timeout
        return float(_get_approval_timeout())
    except Exception:
        return 300.0


def _session_peer() -> str:
    """Authenticated A2A peer of the current turn (the gateway binds it as the session user)."""
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_USER_ID", "") or get_session_env("HERMES_SESSION_USER_NAME", "")
    except Exception:
        return ""


class GatewayQueue:
    """Thin adapter over the host's pending-approval queues (``tools.approval``)."""

    def find_request_id(self, session_key: str, command: str, taken: set[str]) -> Optional[str]:
        from tools import approval
        with approval._lock:
            entries = list(approval._gateway_queues.get(session_key, []))
        for entry in reversed(entries):
            rid = entry.data.get("request_id")
            if rid and rid not in taken and entry.data.get("command") == command:
                return rid
        return None

    def is_pending(self, session_key: str, request_id: str) -> bool:
        from tools import approval
        with approval._lock:
            return any(e.data.get("request_id") == request_id for e in approval._gateway_queues.get(session_key, []))

    def resolve(self, session_key: str, choice: str, request_id: str, reason: Optional[str]) -> int:
        from tools.approval import resolve_gateway_approval
        return resolve_gateway_approval(session_key, choice, reason=reason, request_id=request_id)


class ApprovalBridge:
    """Publishes A2A approval prompts and applies the user's decisions (gateway process)."""

    def __init__(self, paths: RelayPaths, queue: Optional[GatewayQueue] = None,
                 clock: Callable[[], float] = time.time,
                 auto_decide: Optional[Callable[[str, list], Optional[str]]] = None):
        self._paths = paths
        self._queue = queue or GatewayQueue()
        self._clock = clock
        self._auto_decide = auto_decide or (lambda command, pattern_keys: None)
        self._lock = threading.Lock()
        self._tracked: dict[str, str] = {}  # request_id -> session_key

    def on_pre_approval_request(self, session_key: str = "", command: str = "", description: str = "",
                                surface: str = "", coalesced: bool = False, pattern_keys=None,
                                **_: object) -> None:
        if surface != "gateway" or coalesced or A2A_SESSION_MARKER not in (session_key or ""):
            return
        auto = self._auto_decide(command, list(pattern_keys or []))
        with self._lock:
            request_id = self._queue.find_request_id(session_key, command, set(self._tracked))
            if not request_id:
                logger.warning("a2a-relay: approval prompt in %s has no matching queue entry", session_key)
                return
            if auto in (ALLOW, DENY):
                resolved = self._queue.resolve(session_key, auto, request_id,
                                               None if auto == ALLOW else "refused by the A2A trust level")
                logger.info("a2a-relay: trust level settled approval %s as %s (%d)", request_id[:8], auto, resolved)
                return
            self._tracked[request_id] = session_key
        now = self._clock()
        timeout = _approval_timeout()
        append_jsonl(self._paths.approvals, {
            "type": "request", "request_id": request_id, "session_key": session_key,
            "peer": _session_peer(), "command": command, "description": description,
            "ts": now, "expires_at": now + timeout,
        })
        logger.info("a2a-relay: published approval %s for %s", request_id[:8], session_key)
        threading.Thread(target=self._await_decision, args=(request_id, session_key, timeout),
                         daemon=True, name=f"a2a-relay-{request_id[:8]}").start()

    def on_post_approval_response(self, session_key: str = "", surface: str = "", choice: str = "",
                                  **_: object) -> None:
        if surface != "gateway" or A2A_SESSION_MARKER not in (session_key or ""):
            return
        with self._lock:
            finished = [rid for rid, key in self._tracked.items()
                        if key == session_key and not self._queue.is_pending(key, rid)]
            for rid in finished:
                self._tracked.pop(rid, None)
        for rid in finished:
            append_jsonl(self._paths.approvals, {"type": "resolved", "request_id": rid, "choice": str(choice),
                                                 "ts": self._clock()})
            logger.info("a2a-relay: approval %s finished with %s", rid[:8], choice)

    def _await_decision(self, request_id: str, session_key: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout + 5
        decision_file = self._paths.decision_file(request_id)
        while time.monotonic() < deadline:
            if decision_file.exists():
                self._apply(decision_file, request_id, session_key)
                return
            if not self._queue.is_pending(session_key, request_id):
                return
            time.sleep(_POLL_SECONDS)

    def _apply(self, decision_file, request_id: str, session_key: str) -> None:
        try:
            decision = json.loads(decision_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("a2a-relay: unreadable decision for %s", request_id[:8], exc_info=True)
            return
        finally:
            decision_file.unlink(missing_ok=True)
        choice = decision.get("choice")
        if choice not in (ALLOW, DENY):
            logger.warning("a2a-relay: ignoring decision %r for %s", choice, request_id[:8])
            return
        resolved = self._queue.resolve(session_key, choice, request_id, decision.get("reason") or None)
        logger.info("a2a-relay: decision %s for %s applied to %d prompt(s)", choice, request_id[:8], resolved)


class DecisionDesk:
    """User-facing side: pending list and decisions (TUI process, slash commands)."""

    def __init__(self, paths: RelayPaths, clock: Callable[[], float] = time.time):
        self._paths = paths
        self._clock = clock

    def pending(self) -> list[dict]:
        records = read_jsonl(self._paths.approvals)
        done = {r.get("request_id") for r in records if r.get("type") == "resolved"}
        now = self._clock()
        return [r for r in records if r.get("type") == "request" and r.get("request_id") not in done
                and float(r.get("expires_at") or 0) > now]

    def decide(self, raw_args: str, choice: str, wait_seconds: float = 3.0) -> str:
        parts = (raw_args or "").strip().split(None, 1)
        pending = self.pending()
        if not parts:
            if len(pending) != 1:
                return self.describe_pending() + t("desk.give_id")
            target = pending[0]
            reason = ""
        else:
            matches = [r for r in pending if r["request_id"].startswith(parts[0].lower())]
            if len(matches) != 1:
                return t("desk.not_found" if not matches else "desk.ambiguous", id=parts[0])
            target = matches[0]
            reason = parts[1] if len(parts) > 1 else ""
        request_id = target["request_id"]
        write_json_atomic(self._paths.decision_file(request_id), {
            "request_id": request_id, "choice": choice, "reason": reason, "ts": self._clock(), "by": "slash"})
        logger.info("a2a-relay: user chose %s for %s", choice, request_id[:8])
        verb = t("desk.allowed" if choice == ALLOW else "desk.denied")
        if self._wait_resolved(request_id, wait_seconds):
            return t("desk.applied", verb=verb, id=request_id[:8], command=target.get("command", "")[:80])
        return t("desk.unconfirmed", verb=verb, id=request_id[:8])

    def describe_pending(self) -> str:
        pending = self.pending()
        if not pending:
            return t("desk.none")
        lines = [t("desk.list")]
        for r in pending:
            left = int(float(r["expires_at"]) - self._clock())
            lines.append(t("desk.item", id=r["request_id"][:8], peer=r.get("peer") or "?", left=left,
                           command=r.get("command", "")[:120]))
        return "\n".join(lines)

    def _wait_resolved(self, request_id: str, wait_seconds: float) -> bool:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if any(r.get("type") == "resolved" and r.get("request_id") == request_id
                   for r in read_jsonl(self._paths.approvals)):
                return True
            time.sleep(0.2)
        return False
