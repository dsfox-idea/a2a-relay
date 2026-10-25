"""Open sessions on every fleet machine (#5): each machine answers for itself, no hub.

The gateway process of each machine serves ``GET /sessions`` on its fleet address, port 9901 (the A2A
port + 1), to fleet members only: the bearer token is the fleet pair token ``sender>receiver``, the same
HMAC the A2A port uses. ``sessions_list`` asks every member in parallel and merges the cards; a machine
that does not answer is listed as such, never silently dropped.

Cards come from the live TUI/desktop sessions of the gateway process (``tui_gateway.server._sessions``),
so a closed chat disappears at once; nothing is persisted.
"""

from __future__ import annotations

import hmac
import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("a2a_relay")

PORT_OFFSET = 1  # sessions port = A2A port + 1 (9900 -> 9901)
PEER_TIMEOUT = 5.0
_PREVIEW = 120
_GUARD_MODULE = "_a2a_relay_sessions_guard"


def _process_guard():
    """One server per process. Every served profile loads its own copy of the plugin (module globals are
    per copy), and one server already lists all profiles' chats."""
    import types
    guard = sys.modules.get(_GUARD_MODULE)
    if guard is None:
        guard = sys.modules.setdefault(_GUARD_MODULE, types.ModuleType(_GUARD_MODULE))
    if not hasattr(guard, "lock"):
        guard.lock, guard.started = threading.Lock(), False
    return guard


class _ExclusiveServer(ThreadingHTTPServer):
    # HTTPServer sets SO_REUSEADDR; on Windows that lets a second socket bind the same port (seen live: two
    # listeners on 9901). The server lives as long as its process, so no TIME_WAIT rebind is needed.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


def sessions_url(a2a_url: str) -> str:
    parts = urlsplit(a2a_url)
    port = (parts.port or (443 if parts.scheme == "https" else 80)) + PORT_OFFSET
    return f"{parts.scheme}://{parts.hostname}:{port}"


class LocalSessions:
    """Cards of the owner's open chats served by this gateway process."""

    def __init__(self, machine: Callable[[], str], live: Optional[Callable[[], list[dict]]] = None):
        self._machine = machine
        self._live = live or _gateway_chats

    def cards(self) -> list[dict]:
        cards = []
        for raw in self._live():
            try:
                cwd = str(raw.get("cwd") or "")
                repo, branch = _git(cwd)
                cards.append({"machine": self._machine(), "cwd": cwd, "repo": repo, "branch": branch, **{
                    k: raw.get(k, "") for k in ("id", "session_key", "title", "profile", "source", "status", "model",
                                                "started_at", "last_active", "preview")}})
            except Exception:
                logger.debug("a2a-relay: no card for session %s", raw.get("id"), exc_info=True)
        return cards


def _profile_name(home: str) -> str:
    path = Path(str(home))
    return path.name if path.parent.name == "profiles" else "default"


def _gateway_chats() -> list[dict]:
    """Open owner chats of every profile this gateway serves: LOCAL sessions (TUI, desktop) with a viewer
    attached, read from the session authorities (``gateway/session_live_listing``'s population)."""
    from .restart import _gateway_runner
    runner = _gateway_runner()
    if runner is None:
        return []
    from gateway.config import Platform
    from gateway.session_authorities import all_authorities
    from hermes_state_sessions import INTERNAL_LISTING_SOURCES
    chats = []
    for authority in all_authorities(runner):
        if authority is None or getattr(authority, "retiring", False) or getattr(authority, "retired", False):
            continue
        profile = _profile_name(authority.profile_id)
        for sid, live in list(authority.sessions.items()):
            source = getattr(live, "source", None)
            if source is None or source.platform != Platform.LOCAL or live.superseded or not live.subscribers:
                continue
            row = authority.db.get_session_list_row(sid) or {}
            if row.get("source") in INTERNAL_LISTING_SOURCES or row.get("hidden") or row.get("archived"):
                continue
            busy = live.task is not None and not live.task.done()
            started = float(row.get("started_at") or 0)
            chats.append({"id": sid, "session_key": live.route, "title": row.get("title") or "", "profile": profile,
                          "source": row.get("source") or "", "status": "working" if busy else "idle",
                          "model": row.get("model") or "", "cwd": _session_cwd(runner, source),
                          "started_at": started, "last_active": _last_active(authority.db, sid) or started,
                          "preview": _preview(authority.db, sid)})
    return chats


def _session_cwd(runner, source) -> str:
    try:
        from gateway.session_policy import policy_for_source
        policy = policy_for_source(runner, source)
        return str(policy.cwd) if policy is not None else ""
    except Exception:
        return ""


def _last_message(db, sid: str) -> Optional[dict]:
    try:
        with db._read_ctx() as conn:
            row = conn.execute("SELECT role, content, timestamp FROM messages WHERE session_id = ? "
                               "AND role IN ('user', 'assistant') ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _last_active(db, sid: str) -> float:
    row = _last_message(db, sid)
    try:
        return float(row["timestamp"]) if row and row.get("timestamp") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _preview(db, sid: str) -> str:
    row = _last_message(db, sid)
    text = " ".join(str((row or {}).get("content") or "").split())
    return text[:_PREVIEW]


def _git(cwd: str) -> tuple[str, str]:
    if not cwd:
        return "", ""
    try:
        from tui_gateway import git_probe
        root = git_probe.repo_root(cwd)
        return (Path(root).name, git_probe.branch(cwd)) if root else ("", "")
    except Exception:
        return "", ""


class FleetAuth:
    """Which fleet member, if any, a bearer token belongs to (read per request: tokens follow ``fleet sync``)."""

    def __init__(self, fleet: Callable[[], object]):
        self._fleet = fleet

    def me_and_peers(self) -> tuple[str, str, dict[str, str]]:
        fleet = self._fleet()
        return fleet.me, fleet.secret(), fleet.peers()

    def caller(self, header: str) -> Optional[str]:
        from .fleet import pair_token
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            return None
        me, secret, peers = self.me_and_peers()
        if not secret:
            return None
        for name in peers:
            if hmac.compare_digest(token, pair_token(secret, name, me)):
                return name
        return None


class SessionServer:
    def __init__(self, local: LocalSessions, auth: FleetAuth):
        self._local = local
        self._auth = auth
        self.httpd: Optional[ThreadingHTTPServer] = None

    def bind(self, host: str, port: int) -> ThreadingHTTPServer:
        local, auth = self._local, self._auth

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                if self.path.split("?", 1)[0] != "/sessions":
                    return self._send(404, {"error": "not found"})
                caller = auth.caller(self.headers.get("Authorization", ""))
                if caller is None:
                    return self._send(401, {"error": "fleet token required"})
                self._send(200, {"sessions": local.cards()})

            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):  # noqa: A002 (http.server API)
                logger.debug("a2a-relay sessions: " + format, *args)

        self.httpd = _ExclusiveServer((host, port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="a2a-relay-sessions").start()
        return self.httpd

    def start_when_gateway(self, runner: Callable[[], object], address: Callable[[], Optional[tuple[str, int]]],
                           tries: int = 60, every: float = 5.0) -> None:
        """Bind once per process, after this process turns out to be the gateway (the plugin loads first)."""

        def wait() -> None:
            for _ in range(tries):
                if runner() is not None:
                    break
                time.sleep(every)
            else:
                return
            guard = _process_guard()
            with guard.lock:
                if guard.started:
                    return
                guard.started = True
            where = address()
            if where is None:
                logger.info("a2a-relay: no fleet address for this machine; sessions port not served")
                return
            try:
                self.bind(*where)
                logger.info("a2a-relay: sessions served on %s:%d", *where)
            except OSError:
                logger.warning("a2a-relay: could not serve sessions on %s:%d", *where, exc_info=True)

        threading.Thread(target=wait, daemon=True, name="a2a-relay-sessions-start").start()


def _http_get_json(url: str, token: str, timeout: float = PEER_TIMEOUT) -> tuple[int, object]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError) as exc:
        return 0, str(exc)


class SessionDirectory:
    """``sessions_list``: this machine's cards plus every fleet member's, fetched in parallel."""

    def __init__(self, local: LocalSessions, auth: FleetAuth, members: Callable[[], Mapping[str, str]],
                 http_get: Callable[[str, str], tuple[int, object]] = _http_get_json):
        self._local = local
        self._auth = auth
        self._members = members
        self._http_get = http_get

    def collect(self) -> tuple[list[dict], dict[str, str]]:
        """(cards, {machine: problem}) for every member."""
        from .fleet import pair_token
        me, secret, peers = self._auth.me_and_peers()
        cards, problems = list(self._local.cards()), {}
        members = {name: url for name, url in self._members().items() if name != me and name in peers}

        def fetch(item):
            name, url = item
            if not secret:
                return name, None, "no fleet secret on this machine"
            status, body = self._http_get(sessions_url(url) + "/sessions", pair_token(secret, me, name))
            if status == 200 and isinstance(body, dict) and isinstance(body.get("sessions"), list):
                return name, body["sessions"], ""
            if status == 0:
                return name, None, f"no answer ({body})" if body else "no answer"
            return name, None, f"HTTP {status}"

        if members:
            with ThreadPoolExecutor(max_workers=len(members)) as pool:
                for name, found, problem in pool.map(fetch, sorted(members.items())):
                    if problem:
                        problems[name] = problem
                    else:
                        cards.extend(c for c in found if isinstance(c, dict))
        return cards, problems

    @staticmethod
    def select(cards: Iterable[dict], machine: str = "", repo: str = "", profile: str = "",
               idle: Optional[bool] = None) -> list[dict]:
        def keep(card: dict) -> bool:
            if machine and card.get("machine") != machine:
                return False
            if repo and repo.lower() not in str(card.get("repo", "")).lower():
                return False
            if profile and card.get("profile") != profile:
                return False
            if idle is not None and (card.get("status") == "idle") != idle:
                return False
            return True
        return sorted((c for c in cards if keep(c)), key=lambda c: (str(c.get("machine")), -float(c.get("last_active") or 0)))

    def describe(self, machine: str = "", repo: str = "", profile: str = "", idle: Optional[bool] = None,
                 clock: Callable[[], float] = time.time) -> str:
        cards, problems = self.collect()
        chosen = self.select(cards, machine, repo, profile, idle)
        now = clock()
        lines = [f"Open sessions: {len(chosen)}" + (f" of {len(cards)}" if len(chosen) != len(cards) else "")]
        for c in chosen:
            ago = max(0, int(now - float(c.get("last_active") or now)))
            where = f"{c.get('repo')}@{c.get('branch')}" if c.get("repo") else (c.get("cwd") or "-")
            lines.append(f"- {c.get('machine')} | {c.get('profile')} | {c.get('status')} | {ago // 60} min ago | "
                         f"{where} | \"{c.get('title') or '(untitled)'}\" | session {c.get('session_key')}"
                         + (f"\n  last: {c.get('preview')}" if c.get("preview") else ""))
        for name, problem in sorted(problems.items()):
            lines.append(f"- {name}: not reachable ({problem})")
        return "\n".join(lines)
