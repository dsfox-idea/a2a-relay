"""Trust levels: how much an inbound A2A task may do in this profile without the owner (#7).

The level belongs to the receiving profile and lives in ``$HERMES_HOME/a2a_relay/trust.json``. Only
code reads it; only the owner's chats change it (``/a2a-trust``). ``0`` is the most permissive level and
every higher number is stricter, so new levels are added on top and ``strict`` always names the top.

Hermes' own floors (hardline blocks, ``approvals.deny``) run before any of this and stay in force.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .store import append_jsonl, write_json_atomic

logger = logging.getLogger("a2a_relay")

A2A_PLATFORM = "a2a"
A2A_SESSION_MARKER = ":a2a:"
FORWARD_TOOLS = frozenset({"a2a_call", "a2a_send", "a2a_orchestrate"})
READ_TOOLS = frozenset({"read_file", "search_files", "web_search", "web_extract", "session_search",
                        "skills_list", "skill_view", "vision_analyze", "a2a_task", "code_task", "code_wait",
                        "mcp__growser__web_search", "mcp__growser__web_fetch"})
ESCALATION_RULE = "a2a-trust"


@dataclass(frozen=True)
class TrustLevel:
    number: int
    name: str
    summary: str
    hold_for_owner: bool = False     # the task waits for the owner's "да" before it starts
    neutral_framing: bool = False    # no "untrusted external input" frame on the peer's text
    may_forward: bool = False        # may hand the task on to another peer
    approvals: str = "allow_list"    # all | all_but_system | allow_list | read_only


LEVELS: tuple[TrustLevel, ...] = (
    TrustLevel(0, "autonomous", "everything runs on its own", neutral_framing=True, may_forward=True,
               approvals="all"),
    TrustLevel(1, "delegate", "runs on its own; system changes (global installs, Hermes update/plugins/config, "
               "config.yaml/.env/SOUL.md) are asked", neutral_framing=True, may_forward=True,
               approvals="all_but_system"),
    TrustLevel(2, "allow", "commands that need approval run only when they match the allow-list, others are "
               "asked; no forwarding", approvals="allow_list"),
    TrustLevel(3, "read", "read-only tools and a fixed list of read commands; everything else is refused",
               approvals="read_only"),
    TrustLevel(4, "observe", "every task waits for the owner's yes; then as level 2", hold_for_owner=True,
               approvals="allow_list"),
)
DEFAULT_LEVEL = 1
STRICT_ALIAS = "strict"


def level_by_token(token: str) -> Optional[TrustLevel]:
    token = (token or "").strip().lower()
    if token == STRICT_ALIAS:
        return LEVELS[-1]
    for level in LEVELS:
        if token in (str(level.number), level.name):
            return level
    return None


class TrustPolicy:
    """Level and allow-list of the current profile; re-read on every call (the TUI process writes it)."""

    def __init__(self, root: Callable[[], Path]):
        self._root = root

    @property
    def path(self) -> Path:
        return self._root() / "trust.json"

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            logger.warning("a2a-relay: unreadable %s; using the strictest level", self.path, exc_info=True)
            return {"level": LEVELS[-1].number}
        return data if isinstance(data, dict) else {}

    def level(self) -> TrustLevel:
        raw = self._read().get("level", DEFAULT_LEVEL)
        found = level_by_token(str(raw))
        return found or LEVELS[-1]

    def is_default(self) -> bool:
        return "level" not in self._read()

    def allow_list(self) -> list[str]:
        raw = self._read().get("allow") or []
        return [p for p in raw if isinstance(p, str) and p.strip()]

    def set_level(self, level: TrustLevel) -> None:
        data = self._read()
        data["level"] = level.number
        write_json_atomic(self.path, data)

    def set_allow_list(self, patterns: Iterable[str]) -> None:
        data = self._read()
        data["allow"] = sorted(set(patterns))
        write_json_atomic(self.path, data)


def _command_variants(command: str) -> list[str]:
    try:
        from tools.approval_detection import _deny_command_variants
        variants = list(_deny_command_variants(command))
    except Exception:
        variants = []
    return [v.lower().strip() for v in [command, *variants] if v]


def _is_compound(command: str) -> bool:
    try:
        from tools.approval_floors import _has_allowlist_shell_operator
        return bool(_has_allowlist_shell_operator(command))
    except Exception:
        return bool(re.search(r"[;&|<>`$\n]", command))


_PIP_INSTALL = re.compile(r"(?:^|[\s;&|(])(?:sudo\s+)?(?:\S*python[\d.]*\s+-m\s+)?\S*pip[\d.]*\s+install\b")
_SYSTEM_COMMANDS = tuple(re.compile(p) for p in (
    r"\b(?:npm|pnpm)\s+(?:i|install|add)\b[^\n]*\s(?:-g|--global)\b",
    r"\byarn\s+global\s+add\b",
    r"\bbrew\s+(?:install|upgrade|reinstall|tap)\b",
    r"\bwinget\s+(?:install|upgrade|import)\b",
    r"\b(?:apt|apt-get|dnf|yum|pacman|zypper|apk|snap|port)\s+(?:-\S+\s+)*(?:install|upgrade|-S)\b",
    r"\b(?:choco|scoop)\s+(?:install|upgrade|update)\b",
    r"\b(?:cargo|go|gem|pipx)\s+install\b",
    r"\buv\s+tool\s+install\b",
    r"\bhermes\s+(?:-p\s+\S+\s+)?(?:update\b|plugins?\s+(?:install|enable|disable|remove|uninstall|update)\b|"
    r"config\s+(?:set|edit|migrate)\b|tools\b|setup\b|a2a-relay\s+(?:setup|fleet\s+sync)\b)",
    r"(?:^|[/\\\s'\"])(?:config\.yaml|\.env|soul\.md)\b",
    r"\ba2a_relay\b",
))
_PROTECTED_FILES = frozenset({"config.yaml", ".env", "soul.md"})
_RELAY_DIR = "a2a_relay"


class CommandRules:
    """Pure classification of a tool call or command; no I/O except Hermes' own parsers."""

    def __init__(self, hermes_root: Callable[[], Path]):
        self._hermes_root = hermes_root

    @staticmethod
    def is_system_change(command: str) -> bool:
        text = (command or "").lower()
        if _PIP_INSTALL.search(text) and not re.search(r"venv[\\/]", text):
            return True
        return any(p.search(text) for p in _SYSTEM_COMMANDS)

    def is_protected_path(self, path: str) -> bool:
        if not path:
            return False
        try:
            target = Path(path).expanduser().resolve()
            root = self._hermes_root().resolve()
        except (OSError, RuntimeError):
            return False
        if (root / _RELAY_DIR) in target.parents:
            return True  # trust.json, decision files: a peer must never raise its own level or approve itself
        return target.name.lower() in _PROTECTED_FILES and (target == root or root in target.parents)

    @staticmethod
    def matches_allow_list(command: str, patterns: Iterable[str]) -> Optional[str]:
        globs = [p.strip().lower() for p in patterns if p.strip()]
        for variant in _command_variants(command):
            for pattern in globs:
                if fnmatch.fnmatchcase(variant, pattern):
                    return pattern
        return None

    @staticmethod
    def is_read_command(command: str) -> bool:
        command = (command or "").strip()
        if not command or _is_compound(command):
            return False
        try:
            words = shlex.split(command, posix=True)
        except ValueError:
            return False
        if not words:
            return False
        head, rest = Path(words[0]).name.lower().removesuffix(".exe"), words[1:]
        if head in ("ls", "cat", "head", "tail", "pwd", "wc"):
            return True
        if head == "rg":
            return not any(a == "--pre" or a.startswith("--pre=") for a in rest)
        if head == "git":
            # -C <dir> only changes the directory; any other global option (-c, --exec-path, ...) can run code.
            while len(rest) >= 2 and rest[0] == "-C":
                rest = rest[2:]
            if rest and rest[0].startswith("-"):
                return False
            sub = next((a for a in rest if not a.startswith("-")), "")
            if sub not in ("status", "log", "diff", "show", "branch"):
                return False
            if any(a == "--output" or a.startswith("--output=") for a in rest):
                return False
            if sub == "branch":
                return not any(a in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy",
                                     "--set-upstream-to", "-u", "--unset-upstream", "--edit-description")
                               or a.startswith("--set-upstream-to=") for a in rest)
            return True
        if head == "hermes":
            return bool(rest) and rest[-1] in ("doctor", "status")
        return False


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "") or ""
    except Exception:
        return ""


def current_session_is_a2a() -> bool:
    """True inside a turn that serves an inbound A2A task (gateway session or profile forward)."""
    if A2A_SESSION_MARKER in _session_env("HERMES_SESSION_KEY"):
        return True
    try:
        from hermes_cli.plugin_session import session_surface
        return session_surface() == A2A_PLATFORM
    except Exception:
        return _session_env("HERMES_SESSION_PLATFORM") == A2A_PLATFORM


class TrustGate:
    """Enforcement in A2A sessions: tool gate, approval auto-decisions and the decision journal."""

    def __init__(self, policy: TrustPolicy, rules: CommandRules, journal: Path,
                 is_a2a: Callable[[], bool] = current_session_is_a2a, clock: Callable[[], float] = time.time,
                 owner_said_yes: Callable[[], bool] = lambda: False):
        self._policy = policy
        self._rules = rules
        self._journal = journal
        self._is_a2a = is_a2a
        self._clock = clock
        self._owner_said_yes = owner_said_yes

    def note(self, decision: str, what: str, level: TrustLevel, peer: str = "") -> None:
        append_jsonl(self._journal, {"type": "auto", "decision": decision, "what": what[:300],
                                     "level": level.number, "peer": peer, "ts": self._clock()})
        logger.info("a2a-relay: trust level %d %s: %s", level.number, decision, what[:120])

    # -- pre_tool_call ----------------------------------------------------------------------------
    def on_pre_tool_call(self, tool_name: str = "", args: Optional[dict] = None, **_) -> Optional[dict]:
        if not self._is_a2a():
            return None
        level, args = self._policy.level(), args if isinstance(args, dict) else {}
        command = str(args.get("command") or "") if tool_name == "terminal" else ""
        if level.hold_for_owner and not self._owner_said_yes():
            self.note("refused", f"{tool_name} (task not approved)", level, _session_env("HERMES_SESSION_USER_ID"))
            return {"action": "block", "message": (
                f"Refused: at A2A trust level {level.number} '{level.name}' a task runs only after the owner "
                "approved it, and this one was not. Tell the peer the task needs the owner's approval.")}
        if level.approvals == "read_only":
            if tool_name in READ_TOOLS or (tool_name == "terminal" and self._rules.is_read_command(command)):
                return None
            what = f"{tool_name}: {command}" if command else tool_name
            self.note("refused", what, level, _session_env("HERMES_SESSION_USER_ID"))
            return {"action": "block", "message": (
                f"Refused by the A2A trust level of this profile (level {level.number} '{level.name}': "
                f"{level.summary}). Tell the peer which part of the task was not done and why.")}
        if tool_name in FORWARD_TOOLS and not level.may_forward:
            self.note("refused", f"{tool_name} (forwarding)", level, _session_env("HERMES_SESSION_USER_ID"))
            return {"action": "block", "message": (
                f"Forwarding to another peer is not allowed at A2A trust level {level.number} '{level.name}'.")}
        if level.approvals == "all":
            return None
        system = (self._rules.is_system_change(command) if command else
                  tool_name in ("write_file", "patch") and self._rules.is_protected_path(str(args.get("path") or "")))
        if not system:
            return None
        if level.approvals == "allow_list" and command and \
                self._rules.matches_allow_list(command, self._policy.allow_list()):
            return None
        return {"action": "approve", "rule_key": f"{ESCALATION_RULE}:{tool_name}",
                "message": f"system change requested by an A2A peer (trust level {level.number} '{level.name}')"}

    # -- approval prompts -------------------------------------------------------------------------
    def auto_decision(self, command: str, pattern_keys: Iterable[str]) -> Optional[str]:
        """``"once"`` / ``"deny"`` to settle an approval prompt of an A2A session without the owner, or
        None to ask the owner."""
        level = self._policy.level()
        escalated = any(str(k).startswith(f"plugin_rule:{ESCALATION_RULE}") for k in pattern_keys or ())
        if level.approvals == "read_only":
            return "deny"
        if escalated:
            return None
        if level.approvals == "all":
            return "once"
        if level.approvals == "all_but_system":
            return None if self._rules.is_system_change(command) else "once"
        return "once" if self._rules.matches_allow_list(command, self._policy.allow_list()) else None

    def level(self) -> TrustLevel:
        return self._policy.level()


class TrustCommand:
    """``/a2a-trust`` (owner's chats only)."""

    def __init__(self, policy: TrustPolicy, journal: Path, is_a2a: Callable[[], bool] = current_session_is_a2a):
        self._policy = policy
        self._journal = journal
        self._is_a2a = is_a2a

    def handle(self, raw: str) -> str:
        if self._is_a2a():
            return "Уровень доверия A2A меняет только владелец из своего чата, не A2A-сессия."
        parts = (raw or "").strip().split(None, 1)
        if not parts:
            return self.describe()
        verb = parts[0].lower()
        if verb in ("allow", "disallow"):
            pattern = parts[1].strip() if len(parts) > 1 else ""
            if not pattern:
                return f"Укажи шаблон: /a2a-trust {verb} <шаблон>, например /a2a-trust allow git pull*"
            current = self._policy.allow_list()
            if verb == "allow":
                self._policy.set_allow_list([*current, pattern])
                return f"Шаблон добавлен: {pattern}\n\n" + self.describe()
            if pattern not in current:
                return f"Шаблона «{pattern}» нет в списке.\n\n" + self.describe()
            self._policy.set_allow_list([p for p in current if p != pattern])
            return f"Шаблон убран: {pattern}\n\n" + self.describe()
        level = level_by_token(verb)
        if level is None:
            return ("Не понял. Варианты: /a2a-trust, /a2a-trust <0-4|имя|strict>, "
                    "/a2a-trust allow <шаблон>, /a2a-trust disallow <шаблон>.")
        self._policy.set_level(level)
        logger.info("a2a-relay: owner set trust level %d (%s)", level.number, level.name)
        return f"Уровень доверия A2A для этого профиля: {level.number} «{level.name}».\n\n" + self.describe()

    def describe(self) -> str:
        current = self._policy.level()
        lines = [f"Уровень доверия A2A этого профиля: {current.number} «{current.name}»"
                 + (" (по умолчанию)" if self._policy.is_default() else "") + f" — {current.summary}.", "",
                 "Уровни (0 свободнее всего, strict = самый строгий):"]
        lines += [f"{'→' if lv == current else ' '} {lv.number} {lv.name}: {lv.summary}" for lv in LEVELS]
        allow = self._policy.allow_list()
        lines += ["", "Список разрешённых команд (уровни 2 и 4): " + (", ".join(allow) if allow else "пуст")]
        recent = self._recent_decisions(5)
        if recent:
            lines += ["", "Последние автоматические решения:"]
            lines += [f"- {time.strftime('%d.%m %H:%M', time.localtime(r.get('ts', 0)))} "
                      f"{r.get('peer') or '?'}: {r.get('decision')} — {str(r.get('what', ''))[:100]}" for r in recent]
        return "\n".join(lines)

    def _recent_decisions(self, count: int) -> list[dict]:
        from .store import read_jsonl
        return [r for r in read_jsonl(self._journal) if r.get("type") == "auto"][-count:]
