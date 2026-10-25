"""``hermes a2a-relay doctor`` (read-only) and ``hermes a2a-relay setup`` (applies doctor's fixes)."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time

from .doctor import Doctor, HostFacts, format_report
from .i18n import ALL_YES


def build_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="a2a_relay_action")
    doctor = sub.add_parser("doctor", help="Check this machine's A2A and a2a-relay setup (read-only)")
    doctor.add_argument("--no-network", action="store_true", help="Skip probing the configured peers")
    setup = sub.add_parser("setup", help="Apply the fixes doctor reports (backs up config.yaml first)")
    setup.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation")
    setup.add_argument("--no-network", action="store_true", help="Skip probing the configured peers")
    fleet = sub.add_parser("fleet", help="Pair machines: one fleet secret, a token per pair")
    actions = fleet.add_subparsers(dest="fleet_action")
    actions.add_parser("show", help="Members, and which peers use fleet tokens (no secrets printed)")
    add = actions.add_parser("add", help="Add or move a member (name and A2A base URL); not secret")
    add.add_argument("name")
    add.add_argument("url")
    remove = actions.add_parser("remove", help="Remove a member and its tokens")
    remove.add_argument("name")
    actions.add_parser("invite", help="Print the join command for another machine (contains the secret)")
    join = actions.add_parser("join", help="Join the fleet from an invite code")
    join.add_argument("code")
    actions.add_parser("sync", help="Write the tokens and peer entries this profile needs")
    actions.add_parser("prune", help="Drop pre-fleet inbound tokens of members")
    coder = sub.add_parser("coder", help="Coding executors through acpx (code_send)")
    coder_actions = coder.add_subparsers(dest="coder_action")
    init = coder_actions.add_parser("init", help="Write a default coder.json for this profile (claude, ~/dev)")
    init.add_argument("--root", action="append", default=[], help="Projects directory (repeatable; default ~/dev)")
    init.add_argument("--executor", action="append", default=[],
                      help="claude | opencode | codex (repeatable; the first is the default; default claude)")


def command(args: argparse.Namespace) -> int:
    action = getattr(args, "a2a_relay_action", None)
    if action == "fleet":
        return FleetCommand().run(args)
    if action == "doctor":
        checks = Doctor(HostFacts.live()).run(probe_peers=not args.no_network)
        print(format_report(checks))
        return 1 if any(c.failed for c in checks) else 0
    if action == "setup":
        return Setup(args.yes, probe_peers=not args.no_network).run()
    if action == "coder" and getattr(args, "coder_action", None) == "init":
        from hermes_constants import get_hermes_home
        from .coder import CoderError
        from .coder_doctor import init_config
        try:
            print(init_config(get_hermes_home() / "a2a_relay", list(args.root), executors=tuple(args.executor)))
        except CoderError as exc:
            print(f"Error: {exc}")
            return 1
        return 0
    print("Usage: hermes a2a-relay {doctor|setup|fleet|coder init}")
    return 2


_RESTART = ("\nRestart the gateway from a separate terminal for inbound changes to take effect: "
            "hermes gateway restart\n(An agent cannot restart the gateway it runs in.)")


class FleetCommand:
    """``hermes a2a-relay fleet ...``; every change ends with a sync of this profile."""

    def __init__(self, fleet=None):
        self._fleet = fleet

    @property
    def fleet(self):
        if self._fleet is None:
            self._fleet = build_fleet()
        return self._fleet

    def run(self, args: argparse.Namespace) -> int:
        action = getattr(args, "fleet_action", None) or "show"
        try:
            if action == "show":
                print(self.show())
                return 0
            if action == "invite":
                code = self.fleet.invite_code()
                self._sync()
                print("On the machine to add, run (the code contains the fleet secret: paste it into a "
                      "terminal there, not into a chat):\n\n"
                      f"  hermes a2a-relay fleet join {code}\n")
                return 0
            if action == "add":
                self.fleet.add(args.name, args.url)
            elif action == "remove":
                self.fleet.remove(args.name)
            elif action == "join":
                self.fleet.join(args.code)
            elif action == "prune":
                dropped = self.fleet.prune()
                print("Dropped pre-fleet inbound tokens of: " + (", ".join(dropped) or "nobody"))
                if dropped:
                    print(_RESTART)
                return 0
            if self.fleet.secret():
                self._sync()
            print(self.show())
            return 0
        except ValueError as exc:
            print(f"Error: {exc}")
            return 1

    def _sync(self) -> None:
        for line in self.fleet.sync():
            print("  " + line)
        if self.fleet.serves_inbound:
            print(_RESTART)

    def show(self) -> str:
        fleet = self.fleet
        members = fleet.members()
        if not members:
            return ("No fleet here yet. On the first machine: hermes a2a-relay fleet add <name> "
                    "http://<this-machine>:9900, then hermes a2a-relay fleet invite")
        lines = [f"Fleet ({'secret set' if fleet.secret() else 'NO secret: not joined'}), this machine: {fleet.me}"]
        lines += [f"  {name:<12} {url}" + ("  (this machine)" if name == fleet.me else "")
                  for name, url in sorted(members.items())]
        off = fleet.outbound_off_fleet()
        missing = fleet.missing_inbound()
        legacy = fleet.legacy_inbound()
        if off:
            lines.append("Outbound not on fleet tokens yet: " + ", ".join(off) + " (run: fleet sync)")
        if missing:
            lines.append("Inbound fleet tokens missing for: " + ", ".join(missing) + " (run: fleet sync)")
        if legacy:
            lines.append("Pre-fleet inbound tokens still accepted from: " + ", ".join(legacy)
                         + " (once every machine is on fleet tokens: fleet prune)")
        return "\n".join(lines)


def build_fleet():
    from .fleet import Fleet, HermesSettings, ProfileFiles
    settings = HermesSettings()
    me = settings.env("A2A_AGENT_NAME")
    source = None
    try:
        from hermes_cli.profiles import get_active_profile_name, get_profile_dir
        # "custom" is a HERMES_HOME outside ~/.hermes: a standalone install, its own default profile.
        if get_active_profile_name() not in ("default", "custom"):
            source = ProfileFiles(get_profile_dir("default"))
            me = source.env("A2A_AGENT_NAME")
    except ImportError:
        pass
    if not me:
        raise ValueError("A2A_AGENT_NAME is not set; run: hermes a2a-relay setup")
    return Fleet(settings, me, source=source)


class Setup:
    def __init__(self, assume_yes: bool, probe_peers: bool, runner=subprocess.run):
        self._assume_yes = assume_yes
        self._probe_peers = probe_peers
        self._run = runner

    def run(self) -> int:
        checks = Doctor(HostFacts.live()).run(probe_peers=self._probe_peers)
        todo = [c for c in checks if c.status != "ok" and c.fixes]
        if not todo:
            print(format_report(checks))
            print("\nNothing to fix automatically." if any(c.failed for c in checks) else "\nAll set.")
            return 1 if any(c.failed for c in checks) else 0
        print("These fixes will be applied:")
        for check in todo:
            for fix in check.fixes:
                print(f"  hermes {' '.join(fix)}    # {check.name}")
        if not self._assume_yes and input("\nApply? [y/N] ").strip().lower() not in ("y", "yes") + ALL_YES:
            print("Nothing changed.")
            return 1
        backup = self._backup_config()
        if backup:
            print(f"config.yaml backed up to {backup}")
        hermes = shutil.which("hermes") or "hermes"
        for check in todo:
            for fix in check.fixes:
                result = self._run([hermes, *fix], capture_output=True, text=True)
                state = "ok" if result.returncode == 0 else f"failed ({result.returncode})"
                print(f"  {state}: hermes {' '.join(fix)}")
                if result.returncode != 0:
                    print("    " + (result.stderr or result.stdout).strip().splitlines()[-1:][0]
                          if (result.stderr or result.stdout).strip() else "")
        after = Doctor(HostFacts.live()).run(probe_peers=self._probe_peers)
        print("\n" + format_report(after))
        if any(c.restart for c in todo):
            print("\nRestart the gateway from a separate terminal for these to take effect: "
                  "hermes gateway restart\n(An agent cannot restart the gateway it runs in.)")
        return 1 if any(c.failed for c in after) else 0

    @staticmethod
    def _backup_config():
        try:
            from hermes_cli.config import get_config_path
            source = get_config_path()
            target = source.parent / "backups" / f"config.yaml.pre-a2a-relay-setup-{time.strftime('%Y%m%d-%H%M%S')}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            return target
        except Exception:
            return None
