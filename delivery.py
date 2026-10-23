"""Retrying delivery of a result into the owner's chat (``PluginContext.inject_message``).

A chat may refuse an injection for a while (its host restarting, the TUI reconnecting), so a result is
retried for a bounded time instead of being lost. Used by peer jobs (``a2a_send``) and coding jobs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("a2a_relay")


@dataclass
class ChatDelivery:
    inject: Callable[[str, str], bool]
    retry_seconds: float = 5.0
    max_attempts: int = 60
    sleep: Callable[[float], None] = time.sleep

    def deliver(self, session_key: str, text: str, label: str) -> bool:
        """True once a host accepted ``text`` for ``session_key``; ``label`` names the item in logs."""
        for attempt in range(self.max_attempts):
            try:
                if self.inject(session_key, text):
                    return True
            except Exception:
                logger.warning("a2a-relay: delivering %s raised", label, exc_info=True)
            if attempt + 1 < self.max_attempts:
                self.sleep(self.retry_seconds)
        logger.warning("a2a-relay: %s could not be delivered to %s", label, session_key)
        return False
