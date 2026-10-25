"""Inbound A2A tasks at the gateway door (``pre_gateway_dispatch``, gateway process).

* Framing: at levels with ``neutral_framing`` the core "untrusted external input" frame is replaced by
  a neutral header that still keeps the peer's text from starting with ``/``.
* Hold: at levels with ``hold_for_owner`` the task waits for the owner's yes in the owner's chat, using
  the same ``approvals.jsonl`` / decision-file channel as command approvals. A refusal (no owner chat,
  "no", timeout) is returned to the peer as a rejected A2A task.

Only the direct A2A path passes this hook. A task forwarded to another profile (``/growser/``) goes
through the A2A adapter's ``_forward_to_profile`` with no plugin hook on the way; :class:`ForwardGuard`
wraps that method on the live adapter and applies the target profile's level the same way (#13).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

from .approvals import ALLOW, DENY, _approval_timeout
from .store import RelayPaths, append_jsonl, write_json_atomic
from .trust import A2A_PLATFORM, TrustGate, TrustLevel, TrustPolicy

logger = logging.getLogger("a2a_relay")

_POLL_SECONDS = 0.5
_MAX_APPROVED = 500
_REPLY_MARGIN = 30.0  # the refusal must reach the peer before the adapter gives up on the task
_TIMEOUTS = ("timeout", "timeout: the owner's chat was busy and never showed the request")


def _reply_limit() -> float:
    """Longest hold for a task: the A2A adapter's own wait for a reply (``A2A_REPLY_TIMEOUT``) minus a margin."""
    try:
        from plugins.platforms.a2a.adapter import _reply_timeout
        return max(60.0, float(_reply_timeout()) - _REPLY_MARGIN)
    except Exception:
        return 300.0


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


def _approved_file(root: Path) -> Path:
    return Path(root) / "approved_tasks.json"


def _read_approved(root: Path) -> dict:
    try:
        data = json.loads(_approved_file(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def is_approved_in(root: Path, context_id: str) -> bool:
    """The owner said yes to a task of this A2A context; ``root`` is a profile's ``a2a_relay`` dir."""
    return bool(context_id) and context_id in _read_approved(root)


def remember_approved(root: Path, context_id: str, now: float) -> None:
    if not context_id:
        return
    approved = _read_approved(root)
    approved[context_id] = now
    if len(approved) > _MAX_APPROVED:
        approved = dict(sorted(approved.items(), key=lambda item: item[1])[-_MAX_APPROVED:])
    write_json_atomic(_approved_file(root), approved)


def forwarded_context_id() -> str:
    """A2A context id of the forwarded task this turn serves (a ``/growser/`` route), ``""`` otherwise.
    Such a turn runs in a local session of the target profile, so the session's chat id is not the
    A2A context; the forward policy carries it (``gateway/session_a2a.py``: identity [agent, tenant,
    peer, context_id])."""
    try:
        from gateway.session_a2a import is_forward_policy
        from gateway.session_policy import active_policy
        policy = active_policy()
        if not is_forward_policy(policy):
            return ""
        return str(json.loads(policy.request_json)["a2a_identity"][3])
    except Exception:
        return ""


FORWARD_METHOD = "_forward_to_profile"
FORWARD_PARAMS = ("agent", "peer", "context_id", "framed_text", "input_id")


def forward_seam_ok() -> tuple[bool, str]:
    """Whether the A2A adapter still has the method :class:`ForwardGuard` wraps, with the same parameters."""
    import inspect
    try:
        from plugins.platforms.a2a.adapter import A2AAdapter
    except Exception as exc:
        return False, f"A2A adapter not importable: {exc}"
    method = getattr(A2AAdapter, FORWARD_METHOD, None)
    if method is None:
        return False, f"A2AAdapter.{FORWARD_METHOD} is gone"
    params = tuple(p for p in inspect.signature(method).parameters if p != "self")
    if params != FORWARD_PARAMS:
        return False, f"A2AAdapter.{FORWARD_METHOD} parameters changed: {params}"
    return True, ""


class ForwardGuard:
    """Trust levels for tasks forwarded to another profile (#13), by wrapping the live A2A adapter's
    ``_forward_to_profile`` (it runs in an HTTP worker thread, so blocking on the owner is fine).

    The level is the TARGET profile's; the owner is asked through ``hold`` (the default profile's
    channel, the one the owner's chat shows) and an approval is stored in the target profile's
    ``a2a_relay`` dir, where that profile's tool gate looks for it."""

    MARK = "_a2a_relay_forward_guard"

    def __init__(self, hold: "TaskHold", profile_home: Callable[[str], Optional[str]], clock=time.time):
        self._hold = hold
        self._profile_home = profile_home
        self._clock = clock

    def install(self, adapter) -> bool:
        original = getattr(adapter, FORWARD_METHOD, None)
        if original is None or getattr(original, self.MARK, False):
            return False
        guard = self

        def guarded(agent, peer, context_id, framed_text, *, input_id=None):
            outcome = guard.before(agent, peer, context_id, framed_text)
            if isinstance(outcome, tuple):
                return outcome
            return original(agent, peer, context_id, outcome, input_id=input_id)

        setattr(guarded, self.MARK, True)
        setattr(adapter, FORWARD_METHOD, guarded)
        logger.info("a2a-relay: trust levels apply to profile routes (A2A adapter forward wrapped)")
        return True

    def before(self, agent: dict, peer: str, context_id: str, framed_text: str):
        """The text to forward, or ``(reply, state)`` to finish the task without forwarding."""
        profile = str((agent or {}).get("profile") or (agent or {}).get("slug") or "").strip()
        home = self._profile_home(profile) if profile else None
        if not home:
            return framed_text
        root = Path(home) / "a2a_relay"
        level = TrustPolicy(lambda: root).level()
        prefix = _core_prefix(peer)
        body = framed_text[len(prefix):] if prefix and framed_text.startswith(prefix) else framed_text
        if level.hold_for_owner:
            choice, reason = self._hold.ask(peer, f"[profile {profile}] {body}")
            if choice != ALLOW:
                self._hold._gate.note("refused", f"task for profile {profile}: {body}", level, peer)
                from plugins.platforms.a2a import protocol
                return f"Declined by the owner of this machine: {reason}", protocol.STATE_REJECTED
            remember_approved(root, context_id, self._clock())
            self._hold._gate.note("approved by owner", f"task for profile {profile}: {body}", level, peer)
        if level.neutral_framing and prefix and framed_text.startswith(prefix):
            return neutral_header(peer, level) + body
        return framed_text

    def install_when_gateway(self, runner: Callable[[], object], tries: int = 120, every: float = 1.0) -> None:
        def wait() -> None:
            for _ in range(tries):
                adapter = _a2a_adapter(runner())
                if adapter is not None:
                    ok, why = forward_seam_ok()
                    if not ok:
                        logger.warning("a2a-relay: trust levels do NOT apply to profile routes: %s", why)
                        return
                    self.install(adapter)
                    return
                time.sleep(every)

        threading.Thread(target=wait, daemon=True, name="a2a-relay-forward-guard").start()


def _a2a_adapter(runner):
    for platform, adapter in (getattr(runner, "adapters", None) or {}).items():
        if getattr(platform, "value", platform) == A2A_PLATFORM:
            return adapter
    return None


class TaskHold:
    def __init__(self, paths: RelayPaths, gate: TrustGate, owner_present: Callable[[], bool],
                 timeout: Callable[[], float] = _approval_timeout, clock: Callable[[], float] = time.time,
                 limit: Optional[Callable[[], float]] = None, sleep: Callable[[float], None] = time.sleep):
        self._paths = paths
        self._gate = gate
        self._owner_present = owner_present
        self._timeout = timeout
        self._limit = limit or _reply_limit
        self._clock = clock
        self._sleep = sleep

    def is_approved(self, context_id: str) -> bool:
        """The owner said yes to a task of this A2A context (read from disk: tools may run elsewhere)."""
        return is_approved_in(self._paths.root, context_id)

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
        remember_approved(self._paths.root, context_id, self._clock())

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
        answer = float(self._timeout())
        limit = max(answer, float(self._limit()))
        now = self._clock()
        append_jsonl(self._paths.approvals, {
            "type": "request", "kind": "task", "request_id": request_id, "session_key": "", "peer": peer,
            "command": task_text.strip()[:2000], "description": "A2A task waits for the owner's yes (trust level)",
            "ts": now, "answer_seconds": answer, "expires_at": now + limit})
        logger.info("a2a-relay: task from %s held for the owner as %s", peer, request_id[:8])
        choice, reason = self._wait(request_id, answer, limit)
        append_jsonl(self._paths.approvals, {"type": "resolved", "request_id": request_id,
                                             "choice": "timeout" if reason in _TIMEOUTS else choice,
                                             "ts": self._clock()})
        self._paths.shown_file(request_id).unlink(missing_ok=True)
        return choice, reason

    def _shown_at(self, request_id: str) -> Optional[float]:
        try:
            return float(json.loads(self._paths.shown_file(request_id).read_text(encoding="utf-8"))["ts"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _wait(self, request_id: str, answer: float, limit: float) -> tuple[str, str]:
        """The owner gets ``answer`` seconds from the moment the chat showed the prompt (an injected
        prompt only queues while the agent works); ``limit`` bounds the whole wait."""
        decision_file = self._paths.decision_file(request_id)
        started = self._clock()
        while True:
            now = self._clock()
            shown = self._shown_at(request_id)
            if shown is not None and now >= shown + answer:
                return DENY, "timeout"
            if now >= started + limit:
                return DENY, ("timeout" if shown is not None else
                              "timeout: the owner's chat was busy and never showed the request")
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
            self._sleep(_POLL_SECONDS)
