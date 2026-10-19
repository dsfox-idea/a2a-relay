"""Run: cd ~/.hermes/hermes-agent && hermes --run-module unittest discover -s <repo>/tests"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _load(name: str):
    if "a2a_relay_under_test" not in sys.modules:
        spec = importlib.util.spec_from_file_location("a2a_relay_under_test", PLUGIN_DIR / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"a2a_relay_under_test.{name}"]


doctor = _load("doctor")
cli = _load("cli")

GOOD_CONFIG = {
    "plugins": {"enabled": ["a2a-relay"], "entries": {"a2a-relay": {"allow_gateway_injection": True}}},
    "platforms": {"a2a": {"enabled": True}},
    "approvals": {"timeout": 300},
    "a2a_agents": {"m4": {"url": "http://m4:9900", "auth": {"type": "bearer", "token": "secret-m4"},
                          "timeout": 900}},
}
GOOD_ENV = {"A2A_AGENT_NAME": "win", "A2A_PEER_TOKENS": "m4:tok", "A2A_TRUSTED_PEERS": "m4",
            "A2A_REPLY_TIMEOUT": "900"}


def facts(config=None, env=None, toolsets=("a2a",), profiles=None, fix=True, http=None):
    env = dict(GOOD_ENV if env is None else env)

    def http_get(url, headers):
        if http:
            return http(url, headers)
        if url.endswith("agent-card.json"):
            return 200, {"name": "m4"}
        return (200, {"status": "ok", "served_agents": []}) if headers else (200, {"status": "ok"})

    return doctor.HostFacts(GOOD_CONFIG if config is None else config, env.get, set(toolsets),
                            profiles or {}, fix, "dsfox-pc2", http_get)


def by_name(checks):
    return {c.name: c for c in checks}


class DoctorTests(unittest.TestCase):
    def test_healthy_machine_has_no_failures(self):
        checks = doctor.Doctor(facts()).run()
        self.assertEqual([c.name for c in checks if c.status != "ok"], [])

    def test_missing_toolset_suggests_the_exact_command(self):
        check = by_name(doctor.Doctor(facts(toolsets=())).run())["inbound sessions can call peers"]
        self.assertTrue(check.failed)
        self.assertEqual(check.fixes, [["tools", "enable", "a2a", "--platform", "a2a"]])
        self.assertTrue(check.restart)

    def test_timeouts_follow_the_approval_timeout(self):
        config = dict(GOOD_CONFIG, approvals={"timeout": 600},
                      a2a_agents={"m4": {"url": "http://m4:9900", "timeout": 300}})
        checks = by_name(doctor.Doctor(facts(config=config, env=dict(GOOD_ENV, A2A_REPLY_TIMEOUT="300"))).run())
        self.assertEqual(checks["reply outlasts an approval"].fixes, [["config", "set", "A2A_REPLY_TIMEOUT", "900"]])
        self.assertEqual(checks["outbound peers"].fixes,
                         [["config", "set", "a2a_agents.m4.timeout", "900", "--force"]])
        long = dict(GOOD_CONFIG, approvals={"timeout": 1200})
        target = by_name(doctor.Doctor(facts(config=long)).run())["reply outlasts an approval"].fixes[0][-1]
        self.assertEqual(target, "1260")

    def test_node_name_defaults_to_short_hostname(self):
        check = by_name(doctor.Doctor(facts(env=dict(GOOD_ENV, A2A_AGENT_NAME=""))).run())["A2A node name"]
        self.assertEqual(check.fixes, [["config", "set", "A2A_AGENT_NAME", "dsfox-pc2"]])

    def test_untrusted_peers_get_the_tokened_names(self):
        check = by_name(doctor.Doctor(facts(env=dict(GOOD_ENV, A2A_TRUSTED_PEERS=""))).run())["inbound peers"]
        self.assertEqual(check.fixes, [["config", "set", "A2A_TRUSTED_PEERS", "m4"]])

    def test_peer_tokens_never_appear_in_the_report(self):
        env = dict(GOOD_ENV, A2A_PEER_TOKENS='"m4:super-secret-token,m5:other-secret"', A2A_TRUSTED_PEERS="m4")
        report = doctor.format_report(doctor.Doctor(facts(env=env)).run())
        self.assertNotIn("super-secret-token", report)
        self.assertNotIn("other-secret", report)
        self.assertNotIn("secret-m4", report)
        self.assertIn("have a token but are not trusted: m5", report)

    def test_peer_probe_reports_unreachable_and_rejected_token(self):
        def unreachable(url, headers):
            return 0, None
        self.assertIn("unreachable", by_name(doctor.Doctor(facts(http=unreachable)).run())["peer m4"].detail)

        def rejects(url, headers):
            return (200, {"name": "m4"}) if url.endswith(".json") else (200, {"status": "ok"})
        check = by_name(doctor.Doctor(facts(http=rejects)).run())["peer m4"]
        self.assertTrue(check.failed)
        self.assertIn("token is not accepted", check.detail)

    def test_unexpanded_token_reference_is_not_sent(self):
        seen = []
        config = dict(GOOD_CONFIG, a2a_agents={"m4": {"url": "http://m4:9900", "timeout": 900,
                                                      "auth": {"type": "bearer", "token": "${A2A_M4}"}}})
        doctor.Doctor(facts(config=config, http=lambda u, h: (seen.append(h), (200, {}))[1])).run()
        self.assertTrue(all("Authorization" not in h for h in seen))

    def test_profile_without_the_plugin_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare, cloned, ready = Path(tmp) / "growser", Path(tmp) / "cloned", Path(tmp) / "other"
            for home in (cloned, ready):
                (home / "plugins" / "a2a-relay").mkdir(parents=True)
            (ready / "config.yaml").write_text("plugins:\n  enabled: [a2a-relay]\n", encoding="utf-8")
            (cloned / "config.yaml").write_text("plugins:\n  enabled: [memsync]\n", encoding="utf-8")
            checks = by_name(doctor.Doctor(facts(profiles={"growser": bare, "cloned": cloned,
                                                          "other": ready})).run())
        self.assertEqual(checks["profile growser"].status, "warn")
        self.assertEqual(checks["profile growser"].fixes[0][:4], ["-p", "growser", "plugins", "install"])
        # Copied by `profile create --clone` but never enabled there (seen on m4).
        self.assertEqual(checks["profile cloned"].status, "warn")
        self.assertIn("installed but not enabled", checks["profile cloned"].detail)
        self.assertEqual(checks["profile cloned"].fixes[0][:4], ["-p", "cloned", "plugins", "enable"])
        self.assertEqual(checks["profile other"].status, "ok")

    def test_old_hermes_build_is_a_warning_with_update_commands(self):
        check = by_name(doctor.Doctor(facts(fix=False)).run())["Hermes can deliver into gateway-served chats"]
        self.assertEqual(check.status, "warn")
        self.assertEqual(check.fixes, [["update", "--set-channel", "main"], ["update"]])


class ProfileTests(unittest.TestCase):
    """A non-default profile: inbound server settings live in the default profile, only the route counts."""

    def profile_facts(self, routes):
        f = facts(env={}, profiles={"growser": Path("/nonexistent")})
        f.profile, f.default_routes = "growser", routes
        return f

    def test_routed_profile_skips_server_checks(self):
        checks = by_name(doctor.Doctor(self.profile_facts({"growser": {"profile": "growser"}})).run())
        self.assertEqual(checks["reachable over A2A"].status, "ok")
        for server_only in ("A2A platform enabled", "A2A node name", "inbound peers", "reply outlasts an approval",
                            "profile growser"):
            self.assertNotIn(server_only, checks)
        self.assertEqual([c.name for c in checks.values() if c.status != "ok"], [])

    def test_unrouted_profile_gets_the_route_command(self):
        check = by_name(doctor.Doctor(self.profile_facts({})).run())["reachable over A2A"]
        self.assertTrue(check.failed)
        self.assertEqual(check.fixes, [["-p", "default", "config", "set",
                                        "platforms.a2a.extra.agents.growser.profile", "growser"]])


class SetupTests(unittest.TestCase):
    def setUp(self):
        self._live = doctor.HostFacts.live
        self._backup = cli.Setup._backup_config
        cli.Setup._backup_config = staticmethod(lambda: None)

    def tearDown(self):
        doctor.HostFacts.live = self._live
        cli.Setup._backup_config = self._backup

    def test_setup_runs_each_fix_through_hermes_and_reports_restart(self):
        states = iter([facts(toolsets=()), facts()])
        doctor.HostFacts.live = classmethod(lambda cls: next(states))
        calls = []

        class Result:
            returncode, stdout, stderr = 0, "", ""

        def runner(argv, **kw):
            calls.append(argv[1:])
            return Result()

        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.Setup(assume_yes=True, probe_peers=False, runner=runner).run()
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["tools", "enable", "a2a", "--platform", "a2a"]])
        self.assertIn("hermes gateway restart", out.getvalue())

    def test_setup_without_confirmation_changes_nothing(self):
        doctor.HostFacts.live = classmethod(lambda cls: facts(toolsets=()))
        import builtins
        import contextlib
        import io
        original_input = builtins.input
        builtins.input = lambda prompt="": "n"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.Setup(assume_yes=False, probe_peers=False,
                                 runner=lambda *a, **k: self.fail("must not run")).run()
        finally:
            builtins.input = original_input
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
