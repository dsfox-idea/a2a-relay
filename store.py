"""On-disk state shared by the gateway process and the TUI process."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import IO, Iterator, Optional

logger = logging.getLogger("a2a_relay")

if os.name == "nt":
    import msvcrt

    def _lock_file(fh: IO) -> None:
        # msvcrt.locking locks a byte range from the current position; LK_LOCK gives up after
        # ~10 s, so keep retrying until the holder releases it (as flock(LOCK_EX) would block).
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue

    def _unlock_file(fh: IO) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fh: IO) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _unlock_file(fh: IO) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)

_REPLACE_ATTEMPTS = 50
_REPLACE_RETRY_SECONDS = 0.02


class RelayPaths:
    """Every file the relay touches. ``root`` defaults to ``$HERMES_HOME/a2a_relay``."""

    def __init__(self, root: Optional[Path] = None, audit: Optional[Path] = None):
        if root is None:
            from hermes_constants import get_hermes_home
            root = get_hermes_home() / "a2a_relay"
        self.root = Path(root)
        # Written by the bundled A2A plugin for every inbound task, reply and outgoing call.
        self.audit = Path(audit) if audit else self.root.parent / "a2a_audit.jsonl"
        self.approvals = self.root / "approvals.jsonl"
        self.decisions = self.root / "decisions"
        self.state = self.root / "state.json"
        self.trust_journal = self.root / "trust_journal.jsonl"
        self.shown = self.root / "shown"  # request_id.json: when the owner's chat actually showed a prompt
        self.lock = self.root / "state.lock"

    def ensure(self) -> None:
        self.decisions.mkdir(parents=True, exist_ok=True)

    def decision_file(self, request_id: str) -> Path:
        return self.decisions / f"{request_id}.json"

    def shown_file(self, request_id: str) -> Path:
        return self.shown / f"{request_id}.json"


def append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON line (single write with O_APPEND, so concurrent writers do not interleave)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    # On Windows the replace fails while another process has the target open for reading.
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(_REPLACE_RETRY_SECONDS)


def read_jsonl(path: Path) -> list[dict]:
    records, _ = read_new_records(path, 0)
    return records


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def read_new_records(path: Path, offset: int) -> tuple[list[dict], int]:
    """Complete JSON lines after byte ``offset`` -> (records, new_offset). A partial last line is
    left for the next read; a file that shrank (rotated/truncated) is re-read from the start."""
    size = file_size(path)
    if size < offset:
        logger.info("a2a-relay: %s shrank (%d < %d), rereading from start", path.name, size, offset)
        offset = 0
    if size == offset:
        return [], offset
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(size - offset)
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset
    records = []
    for raw in chunk[: end + 1].splitlines():
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("a2a-relay: skipping malformed line in %s", path.name)
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records, offset + end + 1


class SharedState:
    """``state.json`` guarded by an exclusive lock on ``state.lock`` (cross-process read-modify-write)."""

    def __init__(self, paths: RelayPaths):
        self._paths = paths

    @contextlib.contextmanager
    def locked(self) -> Iterator[dict]:
        self._paths.ensure()
        # "a+" rather than "a": msvcrt.locking needs a seekable handle positioned at byte 0.
        with self._paths.lock.open("a+") as lock_fh:
            _lock_file(lock_fh)
            try:
                data = self._read()
                before = json.dumps(data, sort_keys=True)
                yield data
                if json.dumps(data, sort_keys=True) != before:
                    write_json_atomic(self._paths.state, data)
            finally:
                _unlock_file(lock_fh)

    def _read(self) -> dict:
        try:
            data = json.loads(self._paths.state.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            logger.warning("a2a-relay: unreadable state.json, starting fresh", exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}
