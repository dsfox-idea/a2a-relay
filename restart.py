"""``gateway_restart`` tool: an agent restarts its own machine's gateway without the owner.

Inside the gateway it uses the same drain-aware path as the ``/restart`` chat command
(``GatewayRunner.request_restart``): new turns are refused, running turns (the calling one included)
finish first, then a detached watcher (Windows) or the service manager (launchd/systemd, exit 75)
starts the new process. A ``hermes gateway restart`` run from the agent's own terminal instead kills
the process tree it runs in.

When the request came from an owner chat, the new gateway process announces itself in that chat, so
the agent re-checks whatever it was waiting on. On Windows a client may auto-start a gateway before
the restart watcher replaces it with yet another one (#12), killing the turn the notice started; so
the marker outlives the delivery until the announcing gateway has stayed up for a while, and a later
gateway that finds the announcer dead announces again.
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
_SETTLE_SECONDS = 180.0  # the announcing gateway must live this long before its notice counts as delivered
_LOST_TEXT = ("\nПредыдущее такое уведомление ушло в gateway PID {lost}, но его тоже перезапустили, так что "
              "ход по нему мог прерваться.")
_BACK_TEXT = ("[a2a-relay: gateway перезапущен по запросу из этого чата (было PID {old}, теперь {new}, "
              "через {took} с)]\nПроверь всё, чего ты ждал: свои фоновые процессы, пиров, незаконченные шаги. "
              "Это служебное сообщение плагина, а не сообщение владельца.")


def _pid_alive(pid: int) -> bool:
    # Never os.kill(pid, 0) on Windows: it sends CTRL_C to the console group (bpo-14484).
    from hermes_cli._subprocess_compat import pid_exists_stdlib
    return bool(pid_exists_stdlib(int(pid)))


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
                 retry_seconds: float = _ANNOUNCE_EVERY, alive: Callable[[int], bool] = _pid_alive,
                 settle_seconds: float = _SETTLE_SECONDS):
        self.marker = Path(marker)
        self._inject = inject
        self._runner = runner
        self._clock = clock
        self._retry_seconds = retry_seconds
        self._alive = alive
        self._settle_seconds = settle_seconds

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
        self._save({"session_key": session_key, "pid": os.getpid(), "ts": self._clock()})

    def _save(self, data: dict) -> None:
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.marker.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.marker)

    def _load(self) -> Optional[dict]:
        try:
            data = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _drop_marker(self) -> None:
        try:
            self.marker.unlink()
        except OSError:
            pass

    def pending_announcement(self) -> Optional[dict]:
        """The marker left by a previous gateway process, if it is recent and no live gateway has
        announced it yet; None otherwise."""
        data = self._load()
        if data is None or data.get("pid") == os.getpid():
            return None
        if self._clock() - float(data.get("ts") or 0) > _MARKER_MAX_AGE:
            self._drop_marker()
            return None
        announcer = data.get("announced_by")
        if announcer is not None and (announcer == os.getpid() or self._alive(int(announcer))):
            return None
        return data

    def announce_in_background(self) -> None:
        """Called at plugin load: post the "back" notice once this process turns out to be the gateway.

        The plugin loads a few seconds before the gateway runner exists, so the thread waits for it."""
        data = self._load()
        if data is None or data.get("pid") == os.getpid() or data.get("announced_by") == os.getpid():
            return
        threading.Thread(target=self._announce_when_gateway, daemon=True, name="a2a-relay-restart-notice").start()

    def _announce_when_gateway(self) -> bool:
        """Wait until this process is the gateway and no live gateway has announced already: an
        announcer that is being stopped may still be alive when this plugin loads."""
        for attempt in range(_ANNOUNCE_TRIES):
            if not self.marker.exists():
                return False
            if self._runner() is not None and self.pending_announcement() is not None:
                return self.announce()
            if attempt + 1 < _ANNOUNCE_TRIES:
                time.sleep(self._retry_seconds)
        # Not the gateway (TUI, a CLI command), or the announcer lives on: leave the marker.
        logger.debug("a2a-relay: restart notice not announced by this process; marker left")
        return False

    def announce(self) -> bool:
        data = self.pending_announcement()
        if data is None:
            return False
        text = _BACK_TEXT.format(old=data.get("pid"), new=os.getpid(),
                                 took=int(self._clock() - float(data.get("ts") or 0)))
        lost = data.get("announced_by")
        if lost is not None:
            text += _LOST_TEXT.format(lost=lost)
        for attempt in range(_ANNOUNCE_TRIES):
            try:
                if self._inject(str(data["session_key"]), text):
                    self._save({**data, "announced_by": os.getpid(), "announced_at": self._clock()})
                    logger.info("a2a-relay: restart notice delivered to %s%s", data["session_key"],
                                f" (again: gateway {lost} died after announcing)" if lost is not None else "")
                    threading.Thread(target=self._settle_later, daemon=True, name="a2a-relay-restart-settle").start()
                    return True
            except Exception:
                logger.debug("a2a-relay: restart notice not delivered yet", exc_info=True)
            if attempt + 1 < _ANNOUNCE_TRIES:
                time.sleep(self._retry_seconds)
        logger.warning("a2a-relay: restart notice for %s was never accepted", data.get("session_key"))
        self._drop_marker()
        return False

    def _settle_later(self) -> None:
        time.sleep(self._settle_seconds)
        self._settle()

    def _settle(self) -> bool:
        """Drop the marker once this gateway has outlived the restart; if it dies first, the marker
        stays and the next gateway announces again."""
        data = self._load()
        if data is None or data.get("announced_by") != os.getpid():
            return False
        self._drop_marker()
        logger.info("a2a-relay: restart notice settled (gateway %s stayed up)", os.getpid())
        return True


def _via_service() -> bool:
    """Same choice as the /restart command: exit 75 to a restarting supervisor, else a detached watcher."""
    from gateway.restart import is_container_restart_context, is_gateway_supervisor_process
    return bool(is_gateway_supervisor_process() or is_container_restart_context())
