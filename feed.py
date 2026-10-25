"""Feed side of the relay (TUI process): turns new A2A audit records and approval prompts into
one injected chat message per batch, delivered into the most recently active TUI chat."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from .i18n import is_relay_text, t
from .store import RelayPaths, SharedState, file_size, read_new_records, write_json_atomic


logger = logging.getLogger("a2a_relay")
_CHOICES = {f"choice.{c}" for c in ("once", "session", "always", "deny", "timeout", "cancelled")}

_QUIET_SECONDS = 3.0         # flush once no new record arrived for this long...
_REPLY_WAIT_SECONDS = 20.0   # ...but hold an unanswered inbound task this long to merge its reply
_MAX_INBOUND_IDS = 300
def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Never ``os.kill(pid, 0)`` here: on Windows signal 0 is CTRL_C_EVENT and interrupts the process."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information, error_access_denied, still_active = 0x1000, 5, 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def owner_chat_alive(paths: RelayPaths) -> bool:
    """True when the process that owns the feed (the owner's last active chat) is still running."""
    with SharedState(paths).locked() as state:
        owner = state.get("owner") or {}
    pid = owner.get("pid")
    if not pid or not owner.get("session_key"):
        return False
    return _pid_alive(int(pid))


def _short(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class FeedRelay:
    """One instance per process; only the process owning the most recently active owner chat claims records."""

    def __init__(self, paths: RelayPaths, inject: Callable[[str, str], bool], can_inject: Callable[[], bool],
                 clock: Callable[[], float] = time.time, pid: Optional[int] = None,
                 on_asked: Optional[Callable[[list], None]] = None):
        self._paths = paths
        self._state = SharedState(paths)
        self._inject = inject
        self._on_asked = on_asked or (lambda ids: None)
        self._can_inject = can_inject
        self._clock = clock
        self._pid = pid if pid is not None else os.getpid()
        self._lock = threading.Lock()
        self._session_key = ""
        self._buffer: list[dict] = []
        self._last_arrival = 0.0
        self._own_calls = 0
        self._queued_prompts: list[str] = []  # approval ids injected but not yet shown in a turn
        self._node = os.getenv("A2A_AGENT_NAME") or ""

    @property
    def node(self) -> str:
        return self._node or t("feed.this_machine")

    # -- hooks -------------------------------------------------------------------------------
    def claim_chat(self, session_key: str) -> None:
        """Make this owner chat (its injection key) the feed target; "" is ignored."""
        if not session_key or not self._can_inject():
            return
        with self._lock:
            self._session_key = session_key
        with self._state.locked() as state:
            state["owner"] = {"pid": self._pid, "session_key": session_key, "ts": self._clock()}

    def note_own_call(self, started: bool) -> None:
        """An a2a_* tool call from this chat is in flight: its audit record is already visible here."""
        with self._lock:
            self._own_calls = max(0, self._own_calls + (1 if started else -1))

    # -- polling -----------------------------------------------------------------------------
    def run(self, stop: threading.Event, interval: float = 1.0) -> None:
        while not stop.wait(interval):
            try:
                self.poll_once()
            except Exception:
                logger.warning("a2a-relay: feed poll failed", exc_info=True)

    def poll_once(self) -> bool:
        """Claim new records if this process owns the feed, flush when due. True when a message was injected."""
        if not self._can_inject():
            return False
        with self._lock:
            session_key = self._session_key
        if session_key:
            events = self._claim(session_key)
            if events:
                with self._lock:
                    self._buffer.extend(events)
                    self._last_arrival = self._clock()
        return self._flush_if_due()

    def _claim(self, session_key: str) -> list[dict]:
        with self._state.locked() as state:
            owner = state.get("owner") or {}
            if owner.get("pid") != self._pid:
                if owner.get("pid") and _pid_alive(int(owner["pid"])):
                    return []
                state["owner"] = {"pid": self._pid, "session_key": session_key, "ts": self._clock()}
            if "audit_offset" not in state:  # first run: start from now, never replay history
                state["audit_offset"] = file_size(self._paths.audit)
                state["approvals_offset"] = file_size(self._paths.approvals)
                return []
            audit, state["audit_offset"] = read_new_records(self._paths.audit, int(state["audit_offset"]))
            approvals, state["approvals_offset"] = read_new_records(self._paths.approvals,
                                                                    int(state.get("approvals_offset", 0)))
            inbound_ids = list(state.get("inbound_ids") or [])
            events = []
            for rec in audit:
                event = self._audit_event(rec, inbound_ids)
                if event:
                    events.append(event)
            state["inbound_ids"] = inbound_ids[-_MAX_INBOUND_IDS:]
        events.extend(self._approval_event(rec) for rec in approvals)
        return [e for e in events if e]

    def _audit_event(self, rec: dict, inbound_ids: list) -> Optional[dict]:
        direction, task_id = rec.get("direction"), str(rec.get("task_id") or "")
        peer, text = rec.get("peer") or "?", _short(rec.get("summary", ""))
        if direction == "inbound":
            inbound_ids.append(task_id)
            return {"kind": "inbound", "task_id": task_id, "ts": rec.get("ts"),
                    "text": t("feed.inbound", peer=peer, node=self.node, id=task_id[-8:], text=text)}
        if direction != "outbound":
            return None
        if task_id in inbound_ids:
            return {"kind": "reply", "task_id": task_id, "ts": rec.get("ts"),
                    "text": t("feed.reply", peer=peer, node=self.node, id=task_id[-8:], text=text)}
        with self._lock:
            own = self._own_calls > 0
        if own:
            return None  # this chat's own a2a_call: the tool call is already on screen
        return {"kind": "call", "task_id": task_id, "ts": rec.get("ts"),
                "text": t("feed.call", peer=peer, node=self.node, id=task_id[-8:], text=text)}

    def _approval_event(self, rec: dict) -> Optional[dict]:
        rid = str(rec.get("request_id") or "")
        if rec.get("type") == "request" and rec.get("kind") == "task":
            minutes = max(1, round(float(rec.get("answer_seconds") or 300) / 60))
            return {"kind": "approval", "request_id": rid, "ts": rec.get("ts"), "text": t(
                "feed.task_prompt", peer=rec.get("peer") or "?", node=self.node, id=rid[:8], minutes=minutes,
                command=_short(rec.get("command", ""), 400))}
        if rec.get("type") == "request":
            until = time.strftime("%H:%M:%S", time.localtime(float(rec.get("expires_at") or 0)))
            return {"kind": "approval", "request_id": rid, "ts": rec.get("ts"), "text": t(
                "feed.command_prompt", peer=rec.get("peer") or "?", id=rid[:8], until=until,
                command=_short(rec.get("command", ""), 400), why=_short(rec.get("description", ""), 200))}
        if rec.get("type") == "resolved":
            choice = str(rec.get("choice"))
            label = t(f"choice.{choice}") if f"choice.{choice}" in _CHOICES else choice
            return {"kind": "resolved", "ts": rec.get("ts"), "text": t("feed.resolved", id=rid[:8], label=label)}
        return None

    def _flush_if_due(self) -> bool:
        with self._lock:
            if not self._buffer or not self._session_key:
                return False
            now = self._clock()
            urgent = any(e["kind"] == "approval" for e in self._buffer)
            quiet = now - self._last_arrival >= _QUIET_SECONDS
            answered = {e["task_id"] for e in self._buffer if e["kind"] == "reply"}
            waiting = [e for e in self._buffer if e["kind"] == "inbound" and e["task_id"] not in answered]
            oldest = min(float(e.get("ts") or now) for e in self._buffer)
            if not urgent and not (quiet and (not waiting or now - oldest >= _REPLY_WAIT_SECONDS)):
                return False
            batch, session_key = list(self._buffer), self._session_key
        text = t("feed.header") + "\n\n".join(e["text"] for e in batch) + t("feed.footer")
        try:
            delivered = self._inject(session_key, text)
        except Exception:
            logger.warning("a2a-relay: injection raised", exc_info=True)
            delivered = False
        if not delivered:
            logger.info("a2a-relay: %d event(s) not delivered to %s yet, will retry", len(batch), session_key)
            return False
        asked = [e["request_id"] for e in batch if e["kind"] == "approval"]
        with self._lock:
            self._buffer = self._buffer[len(batch):]
            self._queued_prompts.extend(asked)
        logger.info("a2a-relay: delivered %d event(s) to %s", len(batch), session_key)
        return True

    def note_turn(self, text: str) -> None:
        """A turn starts in the owner's chat. Accepted injections only queue while the agent works, so a
        prompt counts as shown when the relay's own injected turn runs, not when it was accepted."""
        if not is_relay_text(text):
            return
        with self._lock:
            shown = [rid for rid in self._queued_prompts if f"id {rid[:8]}" in text]
            self._queued_prompts = [rid for rid in self._queued_prompts if rid not in shown]
        if not shown:
            return
        now = self._clock()
        for rid in shown:
            write_json_atomic(self._paths.shown_file(rid), {"request_id": rid, "ts": now})
        self._on_asked(shown)
        logger.info("a2a-relay: %d prompt(s) shown in the owner's chat", len(shown))
