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
_server_lock = threading.Lock()
_server_started = False


def sessions_url(a2a_url: str) -> str:
    parts = urlsplit(a2a_url)
    port = (parts.port or (443 if parts.scheme == "https" else 80)) + PORT_OFFSET
    return f"{parts.scheme}://{parts.hostname}:{port}"


class LocalSessions:
    """Cards of the live chat sessions in this process."""

    def __init__(self, machine: Callable[[], str], live: Optional[Callable[[], list[tuple[str, dict]]]] = None,
                 clock: Callable[[], float] = time.time):
        self._machine = machine
        self._live = live or _tui_sessions
        self._clock = clock

    def cards(self) -> list[dict]:
        cards = []
        for sid, session in self._live():
            if session.get("_finalized"):
                continue
            try:
                cards.append(self._card(sid, session))
            except Exception:
                logger.debug("a2a-relay: no card for session %s", sid, exc_info=True)
        return cards

    def _card(self, sid: str, session: dict) -> dict:
        agent = session.get("agent")
        key = str(getattr(agent, "session_id", None) or session.get("session_key") or sid)
        cwd = str(session.get("cwd") or "")
        repo, branch = _git(cwd)
        home = session.get("profile_home")
        return {
            "machine": self._machine(), "id": sid, "session_key": key, "title": _title(session, key),
            "profile": Path(home).name if home else "default", "source": str(session.get("source") or ""),
            "status": _status(sid, session), "model": str(getattr(agent, "model", "") or ""),
            "cwd": cwd, "repo": repo, "branch": branch,
            "started_at": float(session.get("created_at") or 0), "last_active": float(session.get("last_active") or 0),
            "preview": _preview(session),
        }


def _tui_server():
    # Never import tui_gateway.server here: only a process that already runs it has live chats.
    return sys.modules.get("tui_gateway.server")


def _tui_sessions() -> list[tuple[str, dict]]:
    server = _tui_server()
    if server is None:
        return []
    with server._sessions_lock:
        return list(server._sessions.items())


def _status(sid: str, session: dict) -> str:
    server = _tui_server()
    probe = getattr(server, "_session_live_status", None)
    if callable(probe):
        try:
            return str(probe(sid, session))
        except Exception:
            pass
    return "working" if session.get("running") else "idle"


def _title(session: dict, key: str) -> str:
    server = _tui_server()
    probe = getattr(server, "_session_live_title", None)
    if callable(probe):
        try:
            return str(probe(session, key) or "")
        except Exception:
            pass
    return str(session.get("pending_title") or "")


def _preview(session: dict) -> str:
    for msg in reversed(list(session.get("history") or [])):
        content = msg.get("content", msg.get("text", "")) if isinstance(msg, dict) else ""
        text = content if isinstance(content, str) else " ".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)) if isinstance(content, list) else ""
        text = " ".join(text.split())
        if text:
            return text[:_PREVIEW]
    return ""


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

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="a2a-relay-sessions").start()
        return self.httpd

    def start_when_gateway(self, runner: Callable[[], object], address: Callable[[], Optional[tuple[str, int]]],
                           tries: int = 60, every: float = 5.0) -> None:
        """Bind once per process, after this process turns out to be the gateway (the plugin loads first)."""

        def wait() -> None:
            global _server_started
            for _ in range(tries):
                if runner() is not None:
                    break
                time.sleep(every)
            else:
                return
            with _server_lock:
                if _server_started:
                    return
                _server_started = True
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
