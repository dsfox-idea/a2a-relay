"""``gateway_restart`` tool: an agent restarts its own machine's gateway without the owner.

Inside the gateway it uses the same drain-aware path as the ``/restart`` chat command
(``GatewayRunner.request_restart``): new turns are refused, running turns (the calling one included)
finish first, then a detached watcher (Windows) or the service manager (launchd/systemd, exit 75)
starts the new process. A ``hermes gateway restart`` run from the agent's own terminal instead kills
the process tree it runs in.

When the request came from an owner chat, the new gateway process announces itself in that chat, so
the agent re-checks whatever it was waiting on.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("a2a_relay")

_ANNOUNCE_TRIES = 60
_ANNOUNCE_EVERY = 5.0
_MARKER_MAX_AGE = 3600.0
_BACK_TEXT = ("[a2a-relay: gateway перезапущен по запросу из этого чата (было PID {old}, теперь {new}, "
              "через {took} с)]\nПроверь всё, чего ты ждал: свои фоновые процессы, пиров, незаконченные шаги. "
              "Это служебное сообщение плагина, а не сообщение владельца.")


def _gateway_runner():
    # Never import gateway.run here: importing it marks any process as the gateway (_HERMES_GATEWAY=1).
    module = sys.modules.get("gateway.run")
    try:
        return module._gateway_runner_ref() if module else None
    except Exception:
        return None


class GatewayRestart:
    def __init__(self, marker: Path, inject: Callable[[str, str], bool],
                 runner: Callable[[], object] = _gateway_runner, clock: Callable[[], float] = time.time,
                 retry_seconds: float = _ANNOUNCE_EVERY):
        self.marker = Path(marker)
        self._inject = inject
        self._runner = runner
        self._clock = clock
        self._retry_seconds = retry_seconds

    # -- request ---------------------------------------------------------------------------------
    def request(self, announce_to: str = "") -> str:
        runner = self._runner()
        if runner is None or getattr(runner, "_gateway_loop", None) is None:
            return self._restart_from_outside()
        if getattr(runner, "_restart_requested", False) or getattr(runner, "_draining", False):
            return "A gateway restart is already in progress."
        if announce_to:
            self._write_marker(announce_to)
        accepted = self._call_on_loop(runner)
        if not accepted:
            self._drop_marker()
            return "Error: the gateway refused the restart (one is already running)."
        active = self._safe_count(runner)
        logger.info("a2a-relay: gateway restart requested (pid %s, %d active work unit(s))", os.getpid(), active)
        return (f"Gateway restart scheduled (current PID {os.getpid()}). It waits for {active} running "
                "turn(s), this one included, then restarts; expect the new process 10-30 s after this turn "
                "ends. Finish your reply now; do not call this tool again."
                + (" The new gateway will post a notice in this chat." if announce_to else
                   " Callers can check it by the A2A agent card answering again."))

    @staticmethod
    def _call_on_loop(runner) -> bool:
        loop = runner._gateway_loop
        result: list[bool] = []
        done = threading.Event()

        def call() -> None:
            try:
                via_service = _via_service()
                result.append(bool(runner.request_restart(detached=not via_service, via_service=via_service)))
            except Exception:
                logger.warning("a2a-relay: request_restart failed", exc_info=True)
            finally:
                done.set()

        loop.call_soon_threadsafe(call)
        done.wait(10.0)
        return bool(result and result[0])

    @staticmethod
    def _safe_count(runner) -> int:
        try:
            return int(runner._active_work_count())
        except Exception:
            return 0

    @staticmethod
    def _restart_from_outside() -> str:
        """Not the gateway process (e.g. the Ink TUI on macOS): its terminal may run the CLI itself."""
        hermes = shutil.which("hermes")
        if not hermes:
            return "Error: this process is not the gateway and the hermes CLI is not on PATH."
        subprocess.Popen([hermes, "gateway", "restart"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return ("This process is not the gateway; started `hermes gateway restart` in the background. "
                "Check it in ~30 s with `hermes gateway status`.")

    # -- announcement ------------------------------------------------------------------------------
    def _write_marker(self, session_key: str) -> None:
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.marker.with_suffix(".tmp")
        tmp.write_text(json.dumps({"session_key": session_key, "pid": os.getpid(), "ts": self._clock()}),
                       encoding="utf-8")
        os.replace(tmp, self.marker)

    def _drop_marker(self) -> None:
        try:
            self.marker.unlink()
        except OSError:
            pass

    def pending_announcement(self) -> Optional[dict]:
        """The marker left by a previous gateway process, if it is recent; None otherwise."""
        try:
            data = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("pid") == os.getpid():
            return None
        if self._clock() - float(data.get("ts") or 0) > _MARKER_MAX_AGE:
            self._drop_marker()
            return None
        return data

    def announce_in_background(self) -> None:
        """Called at plugin load in the gateway process: post the "back" notice once the chat accepts it."""
        if self._runner() is None or self.pending_announcement() is None:
            return
        threading.Thread(target=self.announce, daemon=True, name="a2a-relay-restart-notice").start()

    def announce(self) -> bool:
        data = self.pending_announcement()
        if data is None:
            return False
        text = _BACK_TEXT.format(old=data.get("pid"), new=os.getpid(),
                                 took=int(self._clock() - float(data.get("ts") or 0)))
        for attempt in range(_ANNOUNCE_TRIES):
            try:
                if self._inject(str(data["session_key"]), text):
                    self._drop_marker()
                    logger.info("a2a-relay: restart notice delivered to %s", data["session_key"])
                    return True
            except Exception:
                logger.debug("a2a-relay: restart notice not delivered yet", exc_info=True)
            if attempt + 1 < _ANNOUNCE_TRIES:
                time.sleep(self._retry_seconds)
        logger.warning("a2a-relay: restart notice for %s was never accepted", data.get("session_key"))
        self._drop_marker()
        return False


def _via_service() -> bool:
    """Same choice as the /restart command: exit 75 to a restarting supervisor, else a detached watcher."""
    from gateway.restart import is_container_restart_context, is_gateway_supervisor_process
    return bool(is_gateway_supervisor_process() or is_container_restart_context())
