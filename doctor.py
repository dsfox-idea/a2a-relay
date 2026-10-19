"""Read-only fleet checks (``hermes a2a-relay doctor``) and their fixes (``hermes a2a-relay setup``).

Every check reads the live Hermes config and env, never writes; a failed check carries the exact
``hermes ...`` commands that fix it. Secrets are never printed: only names and booleans.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

PLUGIN = "a2a-relay"
INSTALL_SOURCE = "dsfox-idea/a2a-relay"
DEFAULT_APPROVAL_TIMEOUT = 300
REPLY_TIMEOUT_TARGET = 900
PROBE_TIMEOUT = 5

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fixes: list[list[str]] = field(default_factory=list)
    restart: bool = False  # the fix takes effect only after a gateway restart

    @property
    def failed(self) -> bool:
        return self.status == FAIL


class HostFacts:
    """What the checks read, behind one seam so tests pass plain dicts."""

    def __init__(self, config: Mapping[str, Any], env: Callable[[str], Optional[str]],
                 a2a_platform_toolsets: set[str], profile_dirs: Mapping[str, Path],
                 has_local_injection_fix: bool, hostname: str,
                 http_get: Callable[[str, Mapping[str, str]], tuple[int, Any]]):
        self.config = config
        self.env = env
        self.a2a_platform_toolsets = a2a_platform_toolsets
        self.profile_dirs = profile_dirs
        self.has_local_injection_fix = has_local_injection_fix
        self.hostname = hostname
        self.http_get = http_get

    @classmethod
    def live(cls) -> "HostFacts":
        from hermes_cli.config import get_env_value, load_config_readonly
        config = load_config_readonly() or {}
        try:
            from hermes_cli.tools_config import _get_platform_tools
            toolsets = set(_get_platform_tools(config, "a2a"))
        except Exception:
            toolsets = set((config.get("platform_toolsets") or {}).get("a2a") or [])
        try:
            from hermes_cli.profiles import get_profile_dir, list_profiles
            profiles = {p.name: get_profile_dir(p.name) for p in list_profiles() if p.name != "default"}
        except Exception:
            profiles = {}
        try:
            from gateway.run_plugin_injection import GatewayPluginInjectionMixin
            fix = hasattr(GatewayPluginInjectionMixin, "_dispatch_plugin_local_injection")
        except Exception:
            fix = False
        return cls(config, get_env_value, toolsets, profiles, fix, socket.gethostname(), _http_get)


def _http_get(url: str, headers: Mapping[str, str]) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError):
        return 0, None


def _names(raw: Optional[str]) -> list[str]:
    """``a,b`` or ``a:token,b:token`` -> names (token values are dropped here, never kept)."""
    return [part.split(":", 1)[0].strip() for part in (raw or "").strip().strip('"').split(",")
            if part.split(":", 1)[0].strip()]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Doctor:
    def __init__(self, facts: HostFacts):
        self.facts = facts
        cfg = facts.config
        self.approval_timeout = _int((cfg.get("approvals") or {}).get("timeout"), DEFAULT_APPROVAL_TIMEOUT)
        self.timeout_target = max(REPLY_TIMEOUT_TARGET, self.approval_timeout + 60)

    def run(self, probe_peers: bool = True) -> list[Check]:
        checks = [self.plugin_enabled(), self.gateway_injection(), self.a2a_platform(), self.node_name(),
                  self.inbound_auth(), self.a2a_toolset(), self.reply_timeout(), self.peer_timeouts(),
                  self.local_injection_fix()]
        if probe_peers:
            checks.extend(self.peers())
        checks.extend(self.profiles())
        return checks

    # -- plugin --------------------------------------------------------------------------------
    def plugin_enabled(self) -> Check:
        enabled = PLUGIN in ((self.facts.config.get("plugins") or {}).get("enabled") or [])
        return Check("plugin enabled", OK if enabled else FAIL,
                     "" if enabled else f"{PLUGIN} is installed but not in plugins.enabled",
                     [] if enabled else [["plugins", "enable", PLUGIN, "--no-allow-tool-override"]], restart=True)

    def gateway_injection(self) -> Check:
        entry = ((self.facts.config.get("plugins") or {}).get("entries") or {}).get(PLUGIN) or {}
        ok = entry.get("allow_gateway_injection") is True
        return Check("feed may reach your chat", OK if ok else FAIL,
                     "" if ok else "plugins.entries.a2a-relay.allow_gateway_injection is not true",
                     [] if ok else [["config", "set", f"plugins.entries.{PLUGIN}.allow_gateway_injection", "true"]])

    # -- inbound A2A ---------------------------------------------------------------------------
    def a2a_platform(self) -> Check:
        enabled = ((self.facts.config.get("platforms") or {}).get("a2a") or {}).get("enabled") is True
        return Check("A2A platform enabled", OK if enabled else FAIL,
                     "" if enabled else "platforms.a2a.enabled is not true: this machine cannot be called",
                     [] if enabled else [["config", "set", "platforms.a2a.enabled", "true"]], restart=True)

    def node_name(self) -> Check:
        name = (self.facts.env("A2A_AGENT_NAME") or "").strip()
        if name:
            return Check("A2A node name", OK, name)
        suggestion = self.facts.hostname.split(".")[0].lower() or "node"
        return Check("A2A node name", FAIL, "A2A_AGENT_NAME is not set; peers and the feed see only the hostname",
                     [["config", "set", "A2A_AGENT_NAME", suggestion]], restart=True)

    def inbound_auth(self) -> Check:
        tokens = _names(self.facts.env("A2A_PEER_TOKENS"))
        trusted = _names(self.facts.env("A2A_TRUSTED_PEERS"))
        if not tokens:
            return Check("inbound peers", FAIL, "A2A_PEER_TOKENS is empty: no peer can authenticate "
                         "(pair machines first, see the README)")
        if not trusted:
            return Check("inbound peers", FAIL, "A2A_TRUSTED_PEERS is empty: every call is refused (403)",
                         [["config", "set", "A2A_TRUSTED_PEERS", ",".join(tokens)]], restart=True)
        untrusted = [n for n in tokens if n not in trusted]
        unknown = [n for n in trusted if n not in tokens]
        if unknown:
            return Check("inbound peers", WARN, f"trusted but without a token: {', '.join(unknown)}")
        if untrusted:
            return Check("inbound peers", WARN, f"have a token but are not trusted: {', '.join(untrusted)}")
        return Check("inbound peers", OK, ", ".join(trusted))

    def a2a_toolset(self) -> Check:
        ok = "a2a" in self.facts.a2a_platform_toolsets
        return Check("inbound sessions can call peers", OK if ok else FAIL,
                     "" if ok else "the a2a toolset is off for the a2a platform: a task that arrives over A2A "
                                   "cannot be forwarded to another machine",
                     [] if ok else [["tools", "enable", "a2a", "--platform", "a2a"]], restart=True)

    def reply_timeout(self) -> Check:
        value = _int(self.facts.env("A2A_REPLY_TIMEOUT"), DEFAULT_APPROVAL_TIMEOUT)
        ok = value > self.approval_timeout
        return Check("reply outlasts an approval", OK if ok else FAIL,
                     f"A2A_REPLY_TIMEOUT {value}s, approvals.timeout {self.approval_timeout}s",
                     [] if ok else [["config", "set", "A2A_REPLY_TIMEOUT", str(self.timeout_target)]], restart=True)

    def peer_timeouts(self) -> Check:
        peers = self.facts.config.get("a2a_agents") or {}
        short = [name for name, entry in peers.items()
                 if _int((entry or {}).get("timeout"), 120) <= self.approval_timeout]
        if not peers:
            return Check("outbound peers", FAIL, "a2a_agents is empty: this machine cannot call anyone "
                         "(pair machines first, see the README)")
        if not short:
            return Check("outbound peers", OK, ", ".join(sorted(peers)))
        return Check("outbound peers", FAIL,
                     f"wait less than a peer's approval may take: {', '.join(sorted(short))}",
                     [["config", "set", f"a2a_agents.{name}.timeout", str(self.timeout_target), "--force"]
                      for name in sorted(short)])

    def local_injection_fix(self) -> Check:
        if self.facts.has_local_injection_fix:
            return Check("Hermes can deliver into gateway-served chats", OK)
        return Check("Hermes can deliver into gateway-served chats", WARN,
                     "this Hermes build drops plugin messages into chats served by the gateway itself "
                     "(the Windows TUI); needs NousResearch/hermes-agent@11ba615c3",
                     [["update", "--set-channel", "main"], ["update"]])

    def peers(self) -> list[Check]:
        checks = []
        for name, entry in sorted((self.facts.config.get("a2a_agents") or {}).items()):
            entry = entry or {}
            url = str(entry.get("url") or "").rstrip("/")
            token = ((entry.get("auth") or {}).get("token") or "")
            headers = {"Authorization": f"Bearer {token}"} if token and not token.startswith("${") else {}
            status, card = self.facts.http_get(url + "/.well-known/agent-card.json", {})
            if status != 200:
                checks.append(Check(f"peer {name}", FAIL, f"{url} is unreachable (HTTP {status or 'no answer'})"))
                continue
            status, health = self.facts.http_get(url + "/health", headers)
            authenticated = status == 200 and isinstance(health, dict) and "served_agents" in health
            card_name = card.get("name") if isinstance(card, dict) else "?"
            checks.append(Check(f"peer {name}", OK if authenticated else FAIL,
                                f"reachable as '{card_name}'" + ("" if authenticated else
                                                                 ", but the token is not accepted")))
        return checks

    # -- profiles ------------------------------------------------------------------------------
    def profiles(self) -> list[Check]:
        checks = []
        for name, directory in sorted(self.facts.profile_dirs.items()):
            installed = (Path(directory) / "plugins" / PLUGIN).exists()
            checks.append(Check(f"profile {name}", OK if installed else WARN,
                                f"then run: hermes -p {name} a2a-relay doctor" if installed else
                                "plugin not installed: A2A sessions of this profile get no identity line "
                                "and their approvals never reach a chat",
                                [] if installed else [["-p", name, "plugins", "install", INSTALL_SOURCE, "--enable"],
                                                      ["-p", name, "config", "set",
                                                       f"plugins.entries.{PLUGIN}.allow_gateway_injection", "true"]],
                                restart=True))
        return checks


def format_report(checks: list[Check]) -> str:
    marks = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    lines = []
    for check in checks:
        lines.append(f"[{marks[check.status]}] {check.name}" + (f": {check.detail}" if check.detail else ""))
        for fix in check.fixes if check.status != OK else []:
            lines.append("       fix: hermes " + " ".join(fix))
    failed = sum(c.failed for c in checks)
    warned = sum(c.status == WARN for c in checks)
    lines.append(f"\n{failed} failed, {warned} warning(s), {len(checks) - failed - warned} ok")
    return "\n".join(lines)
