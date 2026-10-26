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
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .i18n import t
from .store import append_jsonl, write_json_atomic

logger = logging.getLogger("a2a_relay")

A2A_PLATFORM = "a2a"
A2A_SESSION_MARKER = ":a2a:"
FORWARD_TOOLS = frozenset({"a2a_call", "a2a_send", "a2a_orchestrate", "session_send"})
ASK_TOOLS = frozenset({"clarify"})
ANSWER_TOOLS = frozenset({"session_reply"})
READ_TOOLS = frozenset({"read_file", "search_files", "web_search", "web_extract", "session_search",
                        "skills_list", "skill_view", "vision_analyze", "a2a_task", "code_task", "code_wait", "sessions_list",
                        "mcp__growser__web_search", "mcp__growser__web_fetch"})
ESCALATION_RULE = "a2a-trust"


@dataclass(frozen=True)
class TrustLevel:
    number: int
    name: str
    summary: str
    hold_for_owner: bool = False     # the task waits for the owner's yes before it starts
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
))
_PROTECTED_FILES = frozenset({"config.yaml", ".env", "soul.md"})
_RELAY_DIR = "a2a_relay"
_RELAY_WORD = re.compile(r"\ba2a_relay\b", re.IGNORECASE)
# Commands that cannot write files on their own (output redirects are checked separately).
_RELAY_READERS = frozenset({"ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "find", "stat",
                            "jq", "echo", "cd", "pwd", "du", "test", "true"})
_FIND_WRITES = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", "(", ")", ";;"})


_ENV_FILE = re.compile(r"(?:^|[/\\\s'\"])\.env\b", re.IGNORECASE)
# Hermes subcommands that only read (owner decision 2026-10-26: no approval for them). Anything else under
# ``hermes`` is judged by the system-change patterns.
_HERMES_READS: dict[str, frozenset] = {
    "update": frozenset({"--check", "--plan", "--list-venv-holders", "--install-id"}),
    "config": frozenset({"get", "show", "path", "env-path", "check"}),
    "tools": frozenset({"list", "--summary"}),
    "gateway": frozenset({"status", "list"}),
    "plugins": frozenset({"list"}),
    "a2a-relay": frozenset({"doctor"}),
}


def _hermes_reads(rest: list[str]) -> bool:
    while len(rest) >= 2 and rest[0] in ("-p", "--profile"):
        rest = rest[2:]
    if rest in (["--version"], ["version"], ["status"], ["doctor"]):
        return True
    if not rest or rest[0] not in _HERMES_READS:
        return False
    allowed, args = _HERMES_READS[rest[0]], rest[1:]
    if rest[0] == "update":
        return bool(args) and all(a in allowed for a in args)
    if rest[0] == "a2a-relay" and args[:2] == ["fleet", "show"]:
        return True
    return bool(args) and args[0] in allowed


def _only_reads(command: str) -> bool:
    """True when EVERY part of a command is a read: plain readers (cat, grep, ...), read-only hermes
    subcommands, output to /dev/null or another descriptor only, nothing substituted (#15)."""
    if not (command or "").strip() or "`" in command or "$(" in command or "${" in command:
        return False
    try:
        lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    segment: list[str] = []
    expect: Optional[str] = None  # "file": the redirect target must be /dev/null; "fd": a descriptor
    for token in tokens + [";"]:
        if expect is not None:
            if (expect == "file" and token != "/dev/null") or (expect == "fd" and not re.fullmatch(r"\d+|-", token)):
                return False
            expect = None
            continue
        if token in (">", ">>", "&>", "&>>", ">|"):
            expect = "file"
        elif token == ">&":
            expect = "fd"
        elif token.startswith(">") or token.startswith("<<"):
            return False  # other redirect forms, heredocs: not worth parsing
        elif token in _SEPARATORS:
            if segment and not _segment_reads(segment):
                return False
            segment = []
        elif token != "<":
            segment.append(token)
    return True


def _segment_reads(words: list[str]) -> bool:
    head, rest = Path(words[0]).name.lower().removesuffix(".exe"), words[1:]
    if head == "env":  # env -u NAME / NAME=value prefixes only change the environment of the command
        while rest and (rest[0] in ("-i", "-u") or "=" in rest[0]):
            rest = rest[2:] if rest[0] == "-u" else rest[1:]
        return bool(rest) and _segment_reads(rest)
    if head == "hermes":
        return _hermes_reads(rest)
    if head not in _RELAY_READERS:
        return False
    if head == "find":
        return not any(a in _FIND_WRITES for a in rest)
    if head == "rg":
        return not any(a == "--pre" or a.startswith("--pre=") for a in rest)
    return True


def _writes_relay_dir(command: str) -> bool:
    """True when a command that names ``a2a_relay`` may write: anything but plain reads (#15)."""
    return bool(_RELAY_WORD.search(command)) and not _only_reads(command)


_GRANTS_MODULE = "_a2a_relay_task_grants"
GRANT_TTL = 4 * 3600.0


class TaskGrants:
    """Tasks the owner already said yes to (owner decision 2026-10-26: one yes per task). After his yes to
    one system change of a task, its further system changes run without asking again. A task is an A2A
    session key, or ``mail:<id>`` for a peer's task in an owner chat. Process-wide: every profile's plugin
    copy sees the same grants. Grants expire after ``GRANT_TTL``."""

    def __init__(self, clock: Callable[[], float] = time.time, store: Optional[dict] = None):
        if store is None:
            module = sys.modules.setdefault(_GRANTS_MODULE, types.ModuleType(_GRANTS_MODULE))
            store = module.__dict__.setdefault("grants", {})
        self._grants = store
        self._clock = clock

    def grant(self, task: str) -> None:
        if task:
            self._grants[task] = self._clock() + GRANT_TTL
            logger.info("a2a-relay: owner said yes to task %s; its system changes are not asked again", task)

    def has(self, task: str) -> bool:
        expires = self._grants.get(task) if task else None
        if expires is None:
            return False
        if expires > self._clock():
            return True
        self._grants.pop(task, None)
        return False


class CommandRules:
    """Pure classification of a tool call or command; no I/O except Hermes' own parsers."""

    def __init__(self, hermes_root: Callable[[], Path]):
        self._hermes_root = hermes_root

    @staticmethod
    def is_system_change(command: str) -> bool:
        text = (command or "").lower()
        if _only_reads(command or "") and not _ENV_FILE.search(text):
            return False  # reads of config.yaml / SOUL.md / a2a_relay and read-only hermes commands
        if _PIP_INSTALL.search(text) and not re.search(r"venv[\\/]", text):
            return True
        return any(p.search(text) for p in _SYSTEM_COMMANDS) or _writes_relay_dir(command or "")

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
                 owner_said_yes: Callable[[], bool] = lambda: False, grants: Optional[TaskGrants] = None,
                 task_key: Callable[[], str] = lambda: _session_env("HERMES_SESSION_KEY")):
        self._grants = grants or TaskGrants(clock)
        self._task_key = task_key
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
    def on_pre_tool_call(self, tool_name: str = "", args: Optional[dict] = None,
                         peer_turn: Optional[dict] = None, **_) -> Optional[dict]:
        """``peer_turn``: this owner-chat turn works on a peer's task (``mail.PeerTurns``); the same level
        applies, but the owner is in the chat, so questions are allowed and the hold is the turn's own yes."""
        if peer_turn is None and not self._is_a2a():
            return None
        level, args = self._policy.level(), args if isinstance(args, dict) else {}
        if tool_name in ANSWER_TOOLS:
            return None  # answering the peer (also "cannot do it") is always allowed
        if peer_turn is not None:
            if level.hold_for_owner and not peer_turn.get("approved"):
                self.note("refused", f"{tool_name} (peer task not approved)", level, peer_turn.get("mail", ""))
                return {"action": "block", "message": (
                    f"Refused: at A2A trust level {level.number} '{level.name}' a peer's task runs only after "
                    "the owner says yes in this chat. Retell the task to them in two lines and ask yes or no; "
                    "then end the turn.")}
            return self._decide(level, tool_name, args, approved=True, task=f"mail:{peer_turn.get('mail') or ''}")
        if tool_name in ASK_TOOLS:
            # In an A2A session the question goes back to the calling agent, not to the owner, and nobody answers
            # it: the turn hung for the whole clarify timeout (m4, 2026-10-24). Owner approvals come from the trust
            # level on the command itself.
            self.note("refused", f"{tool_name} (no human in an A2A session)", level,
                      _session_env("HERMES_SESSION_USER_ID"))
            return {"action": "block", "message": (
                "Refused: an A2A session has no human to answer questions; the owner of this machine does not see "
                "them. Do not ask. Run the task as given; a command that needs the owner's approval is sent to "
                "the owner automatically by the trust level. If something is unclear, say so in your reply.")}
        return self._decide(level, tool_name, args, approved=self._owner_said_yes(), task=self._task_key())

    def _decide(self, level: TrustLevel, tool_name: str, args: dict, approved: bool,
                task: str = "") -> Optional[dict]:
        command = str(args.get("command") or "") if tool_name == "terminal" else ""
        if level.hold_for_owner and not approved:
            self.note("refused", f"{tool_name} (task not approved)", level, _session_env("HERMES_SESSION_USER_ID"))
            return {"action": "block", "message": (
                f"Refused: at A2A trust level {level.number} '{level.name}' a task runs only after the owner "
                "approved it, and this one was not. Tell the peer the task needs the owner's approval.")}
        if level.approvals == "read_only":
            if tool_name in READ_TOOLS or tool_name in ASK_TOOLS or \
                    (tool_name == "terminal" and self._rules.is_read_command(command)):
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
        if self._grants.has(task):
            self.note("allowed (owner said yes to this task)", f"{tool_name}: {command}" if command else tool_name,
                      level, task)
            return None
        if level.approvals == "allow_list" and command and \
                self._rules.matches_allow_list(command, self._policy.allow_list()):
            return None
        return {"action": "approve", "rule_key": f"{ESCALATION_RULE}:{tool_name}",
                "message": f"system change requested by an A2A peer (trust level {level.number} '{level.name}')"}

    # -- approval prompts -------------------------------------------------------------------------
    def auto_decision(self, command: str, pattern_keys: Iterable[str], task: str = "") -> Optional[str]:
        """``"once"`` / ``"deny"`` to settle an approval prompt of a peer's task (A2A session, or a peer's task
        in an owner chat) without the owner, or None to ask the owner."""
        level = self._policy.level()
        escalated = any(str(k).startswith(f"plugin_rule:{ESCALATION_RULE}") for k in pattern_keys or ())
        if level.approvals == "read_only":
            return "deny"
        if self._grants.has(task):
            return "once"
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
            return t("trust.owner_only")
        parts = (raw or "").strip().split(None, 1)
        if not parts:
            return self.describe()
        verb = parts[0].lower()
        if verb in ("allow", "disallow"):
            pattern = parts[1].strip() if len(parts) > 1 else ""
            if not pattern:
                return t("trust.give_pattern", verb=verb)
            current = self._policy.allow_list()
            if verb == "allow":
                self._policy.set_allow_list([*current, pattern])
                return t("trust.pattern_added", pattern=pattern) + "\n\n" + self.describe()
            if pattern not in current:
                return t("trust.pattern_missing", pattern=pattern) + "\n\n" + self.describe()
            self._policy.set_allow_list([p for p in current if p != pattern])
            return t("trust.pattern_removed", pattern=pattern) + "\n\n" + self.describe()
        level = level_by_token(verb)
        if level is None:
            return t("trust.usage")
        self._policy.set_level(level)
        logger.info("a2a-relay: owner set trust level %d (%s)", level.number, level.name)
        return t("trust.set", number=level.number, name=level.name) + "\n\n" + self.describe()

    def describe(self) -> str:
        current = self._policy.level()
        lines = [t("trust.current", number=current.number, name=current.name, summary=current.summary,
                   default=t("trust.default") if self._policy.is_default() else ""), "", t("trust.levels")]
        lines += [f"{'→' if lv == current else ' '} {lv.number} {lv.name}: {lv.summary}" for lv in LEVELS]
        allow = self._policy.allow_list()
        lines += ["", t("trust.allow_list", list=", ".join(allow) if allow else t("trust.empty"))]
        recent = self._recent_decisions(5)
        if recent:
            lines += ["", t("trust.recent")]
            lines += [f"- {time.strftime('%d.%m %H:%M', time.localtime(r.get('ts', 0)))} "
                      f"{r.get('peer') or '?'}: {r.get('decision')} — {str(r.get('what', ''))[:100]}" for r in recent]
        return "\n".join(lines)

    def _recent_decisions(self, count: int) -> list[dict]:
        from .store import read_jsonl
        return [r for r in read_jsonl(self._journal) if r.get("type") == "auto"][-count:]
