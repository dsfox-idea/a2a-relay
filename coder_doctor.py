"""Doctor checks for coding executors (#14) and ``hermes a2a-relay coder init``.

Read-only: config, binaries and versions; no prompt is sent, so no tokens are spent. Coding is optional, but a
machine without ``coder.json`` gets a warning with the command that enables it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .coder import ACPX_VERSION, CoderConfig, CoderError, resolve_acpx
from .doctor import FAIL, OK, WARN, Check
from .jobs import JOB_WAIT_SECONDS

NODE_MIN = (22, 13)
TOOL_TIMEOUT = 420  # Hermes cuts a foreground tool call here; a job shorter than this needs no executor
# What each built-in acpx adapter starts (acpx docs/agents.md): claude resolves claude.exe/claude from PATH and
# runs its adapter through npx; opencode and codex run entirely through npx.
EXECUTOR_COMMANDS = {"claude": ("claude", "npx"), "opencode": ("npx",), "codex": ("npx",)}
INIT_FIX = ["a2a-relay", "coder", "init"]


def _version(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(part) for part in match.groups() if part is not None) if match else ()


def _output(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or done.stderr).strip() if done.returncode == 0 else ""


class CoderDoctor:
    def __init__(self, relay_root: Path, which: Callable[[str], Optional[str]] = shutil.which,
                 output: Callable[[list[str]], str] = _output):
        self._path = Path(relay_root) / "coder.json"
        self._which = which
        self._output = output

    def run(self) -> list[Check]:
        try:
            cfg = CoderConfig.load(self._path)
        except CoderError as exc:
            absent = not self._path.exists()
            return [Check("coding executors", WARN if absent else FAIL,
                          "off: no coder.json" if absent else str(exc), [INIT_FIX] if absent else [])]
        return [self.roots(cfg), self.acpx(cfg), self.node(), *self.executors(cfg), self.timeout(cfg)]

    def roots(self, cfg: CoderConfig) -> Check:
        missing = [str(r) for r in cfg.roots if not r.is_dir()]
        if len(missing) == len(cfg.roots):
            return Check("coding roots", FAIL, f"none of the roots exists: {', '.join(missing)}; edit {self._path}")
        if missing:
            return Check("coding roots", WARN, f"missing: {', '.join(missing)}")
        return Check("coding roots", OK, ", ".join(str(r) for r in cfg.roots))

    def acpx(self, cfg: CoderConfig) -> Check:
        if not self._which(cfg.acpx):
            return Check("acpx", FAIL, f"'{cfg.acpx}' is not on PATH; install it: npm install -g acpx@{ACPX_VERSION}")
        found = self._output([*resolve_acpx(cfg.acpx, self._which), "--version"])
        if found != ACPX_VERSION:
            return Check("acpx", WARN, f"version {found or 'unknown'}, the executor is tested with {ACPX_VERSION}: "
                                       f"npm install -g acpx@{ACPX_VERSION}")
        return Check("acpx", OK, ACPX_VERSION)

    def node(self) -> Check:
        found = _version(self._output(["node", "--version"]))
        if not found:
            return Check("node for acpx", FAIL, f"node is not on PATH; acpx needs Node >= {'.'.join(map(str, NODE_MIN))}")
        if found[:2] < NODE_MIN:
            return Check("node for acpx", FAIL, f"Node {'.'.join(map(str, found))} is too old; acpx needs "
                                                f">= {'.'.join(map(str, NODE_MIN))}")
        return Check("node for acpx", OK, ".".join(map(str, found)))

    def executors(self, cfg: CoderConfig) -> list[Check]:
        checks = []
        for name in cfg.executors:
            label = f"executor {name}"
            missing = [cmd for cmd in EXECUTOR_COMMANDS.get(name, ()) if not self._which(cmd)]
            mode = cfg.session_mode(name)
            if name == "claude" and mode in ("", "auto"):
                checks.append(Check(label, FAIL, "session mode is 'auto': claude-agent-acp never asks for permission "
                                                 "there, so the permission mode is void; set session_modes.claude "
                                                 f"to \"default\" in {self._path}"))
            elif missing:
                checks.append(Check(label, FAIL, f"not on PATH: {', '.join(missing)}"))
            elif name not in EXECUTOR_COMMANDS:
                checks.append(Check(label, WARN, "not a known built-in; its launcher is not checked"))
            else:
                checks.append(Check(label, OK, f"mode {mode}" if mode else ""))
        if cfg.default_executor not in cfg.executors:
            checks.append(Check("default executor", FAIL,
                                f"'{cfg.default_executor}' is not in executors ({', '.join(cfg.executors)})"))
        return checks

    def timeout(self, cfg: CoderConfig) -> Check:
        if cfg.timeout_s <= TOOL_TIMEOUT or cfg.timeout_s > JOB_WAIT_SECONDS:
            return Check("coding timeout", WARN, f"timeout_s {cfg.timeout_s}: keep it above {TOOL_TIMEOUT} "
                                                 f"(else a2a_call would do) and at most {JOB_WAIT_SECONDS}")
        return Check("coding timeout", OK, f"{cfg.timeout_s} s")


def default_config(roots: list[str]) -> dict:
    return {"executors": ["claude"], "default_executor": "claude", "roots": roots, "timeout_s": JOB_WAIT_SECONDS,
            "permissions": "approve-all", "session_modes": {"claude": "default"}}


def init_config(relay_root: Path, roots: list[str], home: Path = Path.home()) -> str:
    """Write a default coder.json; refuses to overwrite one. Returns the message for the owner."""
    path = Path(relay_root) / "coder.json"
    if path.exists():
        return f"{path} already exists; edit it by hand."
    if not roots:
        default = home / "dev"
        if not default.is_dir():
            raise CoderError(f"{default} does not exist; pass the projects directory: "
                             "hermes a2a-relay coder init --root <dir>")
        roots = [default.as_posix()]
    missing = [r for r in roots if not Path(os.path.expanduser(r)).is_dir()]
    if missing:
        raise CoderError(f"not a directory: {', '.join(missing)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(default_config(roots), indent=2) + "\n", encoding="utf-8")
    return f"Wrote {path}: executor claude, roots {', '.join(roots)}. Check it with: hermes a2a-relay doctor"
