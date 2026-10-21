"""Fleet pairing (#4): one shared secret per fleet, a distinct bearer token per ordered pair of machines.

The token a sender presents to a receiver is HMAC(secret, "sender>receiver"), so every machine derives
every token it needs from the secret and the member list; nothing else is exchanged. The member list
(name -> A2A base URL) is not secret and lives in config.yaml; only the join code carries the secret.

Membership belongs to the machine: the default profile holds the secret and the member list, other
profiles on the machine read them from there and only get their own outbound peer entries.

Hermes reads inbound tokens once, at gateway start. ``sync`` therefore adds inbound tokens next to the old
ones and switches an outbound peer to the fleet token only once that peer already accepts it, so the
fleet keeps talking throughout a migration.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

SECRET_ENV = "A2A_RELAY_FLEET_SECRET"
MEMBERS_KEY = ("plugins", "entries", "a2a-relay", "fleet")
CODE_PREFIX = "a2ar1."
PEER_TIMEOUT = 900
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def pair_token(secret: str, sender: str, receiver: str) -> str:
    digest = hmac.new(secret.encode(), f"a2a-relay/v1 {sender}>{receiver}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def token_env(peer: str) -> str:
    """The .env variable holding this machine's outbound token for ``peer``."""
    return "A2A_FLEET_TOKEN_" + re.sub(r"[^A-Z0-9]+", "_", peer.upper()).strip("_")


def parse_peer_tokens(raw: Optional[str]) -> list[tuple[str, str]]:
    """``A2A_PEER_TOKENS`` (``name:token,...``) -> [(name, token)], order kept."""
    pairs = []
    for part in (raw or "").strip().strip('"').split(","):
        name, sep, token = part.partition(":")
        if sep and name.strip() and token.strip():
            pairs.append((name.strip(), token.strip()))
    return pairs


def join_peer_tokens(pairs: list[tuple[str, str]]) -> str:
    return ",".join(f"{name}:{token}" for name, token in pairs)


def encode_code(secret: str, members: Mapping[str, str]) -> str:
    payload = json.dumps({"s": secret, "m": dict(sorted(members.items()))}, separators=(",", ":"))
    return CODE_PREFIX + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_code(code: str) -> tuple[str, dict[str, str]]:
    code = code.strip().strip("'\"")
    if not code.startswith(CODE_PREFIX):
        raise ValueError("not an a2a-relay join code (it starts with 'a2ar1.')")
    body = code[len(CODE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        secret, members = str(data["s"]), {str(k): str(v) for k, v in dict(data["m"]).items()}
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"damaged join code: {exc}") from None
    if len(secret) < 32 or not members:
        raise ValueError("damaged join code: no secret or no members")
    for name, url in members.items():
        check_member(name, url)
    return secret, members


def check_member(name: str, url: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValueError(f"bad machine name '{name}': lowercase letters, digits and '-', up to 32 chars")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"bad URL for '{name}': {url!r} (expected http://host:port)")


def members_in(config: Mapping) -> dict[str, str]:
    node = config
    for part in MEMBERS_KEY:
        node = node.get(part) if isinstance(node, Mapping) else None
    return {str(k): str(v).rstrip("/") for k, v in node.items()} if isinstance(node, Mapping) else {}


class Settings(Protocol):
    """One profile's .env and config.yaml."""

    def env(self, key: str) -> str: ...
    def set_env(self, key: str, value: str) -> None: ...
    def unset_env(self, key: str) -> None: ...
    def config(self) -> Mapping: ...
    def set_config(self, key: str, value: str) -> None: ...
    def unset_config(self, key: str) -> None: ...


class ProfileFiles:
    """Read-only view of one profile's .env and config.yaml files.

    Files, not ``os.environ`` / ``load_config()``: a ``hermes`` process started by an agent inherits the
    gateway's environment (another profile's values), and ``load_config`` expands the ``${VAR}`` refs
    that ``sync`` compares.
    """

    def __init__(self, home: Path):
        self.home = Path(home)

    def env(self, key: str) -> str:
        try:
            lines = (self.home / ".env").read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        for line in lines:
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == key:
                return value.strip().strip("'\"")
        return ""

    def config(self) -> Mapping:
        from .doctor import _read_profile_config
        return _read_profile_config(self.home)


class HermesSettings(ProfileFiles):
    """The active profile, written through Hermes' own writers (locks, quoting, managed installs)."""

    def __init__(self):
        from hermes_cli.config import get_hermes_home
        super().__init__(get_hermes_home())

    def set_env(self, key: str, value: str) -> None:
        from hermes_cli.config import require_env_writable, save_env_value
        require_env_writable(key, "set")
        save_env_value(key, value)

    def unset_env(self, key: str) -> None:
        from hermes_cli.config import remove_env_value
        remove_env_value(key)

    def set_config(self, key: str, value: str) -> None:
        from hermes_cli.config import set_config_value
        set_config_value(key, value, force=True)

    def unset_config(self, key: str) -> None:
        from hermes_cli.config import unset_config_value
        unset_config_value(key)


def accepts(url: str, token: str, http_get: Optional[Callable] = None) -> bool:
    """Does the A2A server at ``url`` accept ``token``? (authenticated /health lists served agents)."""
    if http_get is None:
        from .doctor import _http_get as http_get
    status, body = http_get(url.rstrip("/") + "/health", {"Authorization": f"Bearer {token}"})
    return status == 200 and isinstance(body, dict) and "served_agents" in body


class Fleet:
    """The fleet as seen from one profile on one machine (``me``)."""

    def __init__(self, settings: Settings, me: str, source=None, http_get: Optional[Callable] = None):
        self.settings = settings
        self.me = me
        # Where the secret and the member list live: this profile when it is the default one.
        self.source = source or settings
        self.serves_inbound = source is None
        self._http_get = http_get

    # -- state -----------------------------------------------------------------------------------
    def members(self) -> dict[str, str]:
        return members_in(self.source.config())

    def secret(self) -> str:
        return self.source.env(SECRET_ENV)

    def peers(self) -> dict[str, str]:
        return {name: url for name, url in self.members().items() if name != self.me}

    # -- membership (default profile only) ---------------------------------------------------------
    def _require_default(self) -> None:
        if not self.serves_inbound:
            raise ValueError("fleet membership is kept in the default profile; run this without -p")

    def invite_code(self) -> str:
        """Join code for another machine; creates the fleet secret on first use."""
        self._require_default()
        if self.me not in self.members():
            raise ValueError(f"this machine ('{self.me}') is not a member yet: "
                             f"hermes a2a-relay fleet add {self.me} http://<this-machine>:9900")
        if not self.secret():
            self.settings.set_env(SECRET_ENV, secrets.token_urlsafe(32))
        return encode_code(self.secret(), self.members())

    def add(self, name: str, url: str) -> None:
        self._require_default()
        check_member(name, url)
        self.settings.set_config(".".join(MEMBERS_KEY + (name,)), url.rstrip("/"))

    def remove(self, name: str) -> None:
        self._require_default()
        if name not in self.members():
            raise ValueError(f"'{name}' is not a fleet member")
        self.settings.unset_config(".".join(MEMBERS_KEY + (name,)))
        self._forget_peer(name)

    def join(self, code: str) -> None:
        """Make this machine's fleet exactly the one in ``code``."""
        self._require_default()
        secret, members = decode_code(code)
        if self.me not in members:
            raise ValueError(f"'{self.me}' is not in this join code ({', '.join(sorted(members))}); "
                             f"run 'fleet add {self.me} <url>' on the inviting machine and invite again")
        for gone in set(self.members()) - set(members):
            self.remove(gone)
        if self.secret() != secret:
            self.settings.set_env(SECRET_ENV, secret)
        for name, url in members.items():
            if self.members().get(name) != url:
                self.add(name, url)

    # -- tokens --------------------------------------------------------------------------------------
    def sync(self) -> list[str]:
        """Write every token and peer entry this profile needs; idempotent and safe to repeat."""
        secret, peers = self.secret(), self.peers()
        if not secret:
            raise ValueError("this machine has not joined a fleet: 'fleet invite' on a member, "
                             "then 'fleet join <code>' here")
        if self.me not in self.members():
            raise ValueError(f"this machine ('{self.me}') is not a fleet member")
        report = []
        if self.serves_inbound:
            report += self._sync_inbound(secret, peers)
        agents = self.settings.config().get("a2a_agents") or {}
        live: dict[str, bool] = {}
        for peer, url in sorted(peers.items()):
            env, token = token_env(peer), pair_token(secret, self.me, peer)
            if self.settings.env(env) != token:
                self.settings.set_env(env, token)
                # The running gateway expands ${VAR} refs from its startup environment.
                report.append(f"{peer}: outbound token written (restart this gateway to use it)")
            live[peer] = accepts(url, token, self._http_get)
            entry = agents.get(peer) or {}
            if str(entry.get("url") or "").rstrip("/") != url:
                self.settings.set_config(f"a2a_agents.{peer}.url", url)
            if int(entry.get("timeout") or 0) < PEER_TIMEOUT:
                self.settings.set_config(f"a2a_agents.{peer}.timeout", str(PEER_TIMEOUT))
            report.append(self._switch(peer, peer, env, entry, live[peer], url))
        # Routes to other profiles of a member (e.g. m4-growser at <m4>/growser/) use that member's token.
        for name, entry in sorted(agents.items()):
            owner = "" if name in peers or not isinstance(entry, Mapping) else \
                self._member_for_url(str(entry.get("url") or ""), peers)
            if owner:
                report.append(self._switch(name, owner, token_env(owner), entry, live[owner],
                                           str(entry.get("url"))))
        return report

    def _switch(self, agent: str, owner: str, env: str, entry: Mapping, live: bool, url: str) -> str:
        ref = "${" + env + "}"
        auth = entry.get("auth") or {}
        if auth.get("token") == ref:
            return f"{agent}: fleet token" + ("" if live else f" (NOT accepted by {owner} yet: restart its gateway)")
        if not live and auth.get("token"):
            return f"{agent}: still on its old token; {owner} does not accept the fleet token yet " \
                   f"(restart {owner}'s gateway, then run 'fleet sync' again)"
        if auth.get("type") != "bearer":
            self.settings.set_config(f"a2a_agents.{agent}.auth.type", "bearer")
        self.settings.set_config(f"a2a_agents.{agent}.auth.token", ref)
        return f"{agent}: switched to the fleet token ({url})" + \
            ("" if live else f"; {owner} does not accept it yet (restart its gateway)")

    def legacy_inbound(self) -> list[str]:
        """Members that still have a non-fleet inbound token here (from manual pairing)."""
        if not self.serves_inbound or not self.secret():
            return []
        secret, peers = self.secret(), set(self.peers())
        return sorted({name for name, token in parse_peer_tokens(self.settings.env("A2A_PEER_TOKENS"))
                       if name in peers and not hmac.compare_digest(token, pair_token(secret, name, self.me))})

    def prune(self) -> list[str]:
        """Drop the legacy inbound tokens of fleet members (takes effect at the next gateway start)."""
        dropped = self.legacy_inbound()
        if dropped:
            secret = self.secret()
            current = parse_peer_tokens(self.settings.env("A2A_PEER_TOKENS"))
            kept = [(n, t) for n, t in current
                    if n not in dropped or hmac.compare_digest(t, pair_token(secret, n, self.me))]
            self.settings.set_env("A2A_PEER_TOKENS", join_peer_tokens(kept))
        return dropped

    def missing_inbound(self) -> list[str]:
        if not self.serves_inbound or not self.secret():
            return []
        have = set(parse_peer_tokens(self.settings.env("A2A_PEER_TOKENS")))
        trusted = {n.strip() for n in self.settings.env("A2A_TRUSTED_PEERS").split(",")}
        return sorted(p for p in self.peers()
                      if (p, pair_token(self.secret(), p, self.me)) not in have or p not in trusted)

    def outbound_off_fleet(self) -> list[str]:
        agents = self.settings.config().get("a2a_agents") or {}
        return sorted(p for p in self.peers()
                      if ((agents.get(p) or {}).get("auth") or {}).get("token") != "${" + token_env(p) + "}")

    # -- helpers -----------------------------------------------------------------------------------
    @staticmethod
    def _member_for_url(url: str, peers: Mapping[str, str]) -> str:
        url = url.rstrip("/")
        for name, base in peers.items():
            if url == base or url.startswith(base + "/"):
                return name
        return ""

    def _sync_inbound(self, secret: str, peers: Mapping[str, str]) -> list[str]:
        report = []
        tokens = parse_peer_tokens(self.settings.env("A2A_PEER_TOKENS"))
        wanted = [(peer, pair_token(secret, peer, self.me)) for peer in sorted(peers)]
        added = [pair for pair in wanted if pair not in tokens]
        if added:
            self.settings.set_env("A2A_PEER_TOKENS", join_peer_tokens(tokens + added))
            report.append("inbound: fleet tokens added for " + ", ".join(n for n, _ in added)
                          + " (restart this gateway to accept them)")
        trusted = [n.strip() for n in self.settings.env("A2A_TRUSTED_PEERS").split(",") if n.strip()]
        missing = [peer for peer in sorted(peers) if peer not in trusted]
        if missing:
            self.settings.set_env("A2A_TRUSTED_PEERS", ",".join(trusted + missing))
            report.append("inbound: now trusted " + ", ".join(missing) + " (restart this gateway)")
        return report

    def _forget_peer(self, name: str) -> None:
        if (self.settings.config().get("a2a_agents") or {}).get(name) is not None:
            self.settings.unset_config(f"a2a_agents.{name}")
        if self.settings.env(token_env(name)):
            self.settings.unset_env(token_env(name))
        tokens = parse_peer_tokens(self.settings.env("A2A_PEER_TOKENS"))
        kept = [(n, t) for n, t in tokens if n != name]
        if kept != tokens:
            self.settings.set_env("A2A_PEER_TOKENS", join_peer_tokens(kept))
        trusted = [n.strip() for n in self.settings.env("A2A_TRUSTED_PEERS").split(",") if n.strip()]
        if name in trusted:
            self.settings.set_env("A2A_TRUSTED_PEERS", ",".join(n for n in trusted if n != name))
