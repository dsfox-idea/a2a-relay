"""Tells every session which machine and A2A node it runs on.

Shared memory replicated across machines cannot carry "this machine is X" facts: every machine reads
them as its own. The identity therefore comes from the host itself, rendered fresh per session.
"""

from __future__ import annotations

import os
import socket
from typing import Callable, Mapping

SECTION_ID = "a2a-relay.identity"


class MachineIdentity:
    def __init__(self, hostname: Callable[[], str] = socket.gethostname,
                 env: Mapping[str, str] = os.environ):
        self._hostname = hostname
        self._env = env

    def render(self, session: Mapping[str, str] | None = None) -> str:
        host = (self._hostname() or "").strip() or "unknown"
        node = (self._env.get("A2A_AGENT_NAME") or "").strip()
        profile = ((session or {}).get("profile_name") or "").strip()
        facts = [f"host '{host}'"]
        if node:
            facts.append(f"A2A node name '{node}'")
        if profile and profile != "default":
            facts.append(f"Hermes profile '{profile}'")
        return ("Machine identity (read from this host; it overrides any memory entry): you run on "
                + ", ".join(facts) + ".\n"
                "Memory entries about other hosts or nodes describe those machines, not this one.")
