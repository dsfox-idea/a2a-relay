"""``hermes a2a-relay doctor`` (read-only) and ``hermes a2a-relay setup`` (applies doctor's fixes)."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time

from .doctor import Doctor, HostFacts, format_report


def build_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="a2a_relay_action")
    doctor = sub.add_parser("doctor", help="Check this machine's A2A and a2a-relay setup (read-only)")
    doctor.add_argument("--no-network", action="store_true", help="Skip probing the configured peers")
    setup = sub.add_parser("setup", help="Apply the fixes doctor reports (backs up config.yaml first)")
    setup.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation")
    setup.add_argument("--no-network", action="store_true", help="Skip probing the configured peers")


def command(args: argparse.Namespace) -> int:
    action = getattr(args, "a2a_relay_action", None)
    if action == "doctor":
        checks = Doctor(HostFacts.live()).run(probe_peers=not args.no_network)
        print(format_report(checks))
        return 1 if any(c.failed for c in checks) else 0
    if action == "setup":
        return Setup(args.yes, probe_peers=not args.no_network).run()
    print("Usage: hermes a2a-relay {doctor|setup}")
    return 2


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
        if not self._assume_yes and input("\nApply? [y/N] ").strip().lower() not in ("y", "yes", "да", "д"):
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
