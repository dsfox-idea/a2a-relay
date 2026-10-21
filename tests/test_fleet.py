"""Run: cd ~/.hermes/hermes-agent && hermes --run-module unittest discover -s <repo>/tests"""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest

from test_jobs import _load

fleet = _load("fleet")
cli = _load("cli")


class FakeSettings:
    """A profile: dict .env plus a nested dict config, with dotted-key writes like `hermes config set`."""

    def __init__(self, env=None, config=None):
        self.env_values = dict(env or {})
        self.cfg = config or {}

    def env(self, key):
        return self.env_values.get(key, "")

    def set_env(self, key, value):
        self.env_values[key] = value

    def unset_env(self, key):
        self.env_values.pop(key, None)

    def config(self):
        return self.cfg

    def set_config(self, key, value):
        node = self.cfg
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = int(value) if value.isdigit() else value

    def unset_config(self, key):
        node = self.cfg
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.get(part, {})
        node.pop(leaf, None)


URLS = {"win": "http://100.0.0.1:9900", "m4": "http://100.0.0.4:9900", "m5": "http://100.0.0.5:9900"}


class Network:
    """Which tokens each machine's running gateway accepts (fixed at its start, like Hermes)."""

    def __init__(self):
        self.accepted = {name: set() for name in URLS}

    def restart(self, name, settings):
        self.accepted[name] = {t for _, t in fleet.parse_peer_tokens(settings.env("A2A_PEER_TOKENS"))}

    def http_get(self, url, headers):
        name = next(n for n, u in URLS.items() if url.startswith(u))
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        return (200, {"served_agents": []}) if token in self.accepted[name] else (401, None)


def machine(name, net, legacy=None):
    env = {"A2A_AGENT_NAME": name}
    if legacy:
        env["A2A_PEER_TOKENS"] = ",".join(f"{p}:old-{p}-{name}" for p in legacy)
        env["A2A_TRUSTED_PEERS"] = ",".join(legacy)
    config = {"a2a_agents": {p: {"url": URLS[p], "auth": {"type": "bearer", "token": f"old-{name}-{p}"},
                                 "timeout": 900} for p in (legacy or [])}}
    settings = FakeSettings(env, config)
    return settings, fleet.Fleet(settings, name, http_get=net.http_get)


class TokenTests(unittest.TestCase):
    def test_each_direction_gets_its_own_token(self):
        tokens = {fleet.pair_token("s" * 40, a, b) for a in URLS for b in URLS if a != b}
        self.assertEqual(len(tokens), 6)
        self.assertEqual(fleet.pair_token("s" * 40, "m4", "m5"), fleet.pair_token("s" * 40, "m4", "m5"))
        self.assertNotEqual(fleet.pair_token("s" * 40, "m4", "m5"), fleet.pair_token("t" * 40, "m4", "m5"))

    def test_join_code_round_trip_and_damage(self):
        code = fleet.encode_code("x" * 43, URLS)
        self.assertEqual(fleet.decode_code(f"'{code}'"), ("x" * 43, URLS))
        for bad in ("hello", code[:-6] + "!!!!", fleet.encode_code("short", URLS),
                    fleet.encode_code("x" * 43, {"Bad Name": "http://a"}),
                    fleet.encode_code("x" * 43, {"m4": "ftp://a"})):
            with self.assertRaises(ValueError):
                fleet.decode_code(bad)


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.net = Network()

    def form_fleet(self, legacy=False):
        """win invites m4 and m5; returns {name: (settings, fleet)}."""
        machines = {n: machine(n, self.net, [p for p in URLS if p != n] if legacy else None) for n in URLS}
        for name, (settings, _) in machines.items():
            self.net.restart(name, settings)
        win = machines["win"][1]
        for name, url in URLS.items():
            win.add(name, url)
        code = win.invite_code()
        win.sync()
        for name in ("m4", "m5"):
            machines[name][1].join(code)
            machines[name][1].sync()
        return machines

    def test_new_fleet_connects_every_pair_after_restarts(self):
        machines = self.form_fleet()
        for name, (settings, _) in machines.items():
            self.net.restart(name, settings)
        for name, (settings, member) in machines.items():
            member.sync()
            self.assertEqual(member.outbound_off_fleet(), [])
            self.assertEqual(member.missing_inbound(), [])
            for peer, url in member.peers().items():
                token = settings.env(fleet.token_env(peer))
                self.assertEqual(self.net.http_get(url, {"Authorization": f"Bearer {token}"})[0], 200,
                                 f"{name} -> {peer}")
                self.assertEqual(settings.cfg["a2a_agents"][peer]["auth"]["token"],
                                 "${" + fleet.token_env(peer) + "}")
        self.assertNotIn(machines["win"][0].env(fleet.SECRET_ENV), str(machines["win"][0].cfg))

    def test_migration_keeps_old_tokens_until_the_peer_accepts_the_new_one(self):
        machines = self.form_fleet(legacy=True)
        win_settings, win = machines["win"]
        # No gateway restarted yet: outbound stays on the old token, which still works.
        self.assertEqual(win_settings.cfg["a2a_agents"]["m4"]["auth"]["token"], "old-win-m4")
        self.assertEqual(win.outbound_off_fleet(), ["m4", "m5"])
        self.assertIn("m4:old-m4-win", win_settings.env("A2A_PEER_TOKENS"))
        self.assertEqual(win.legacy_inbound(), ["m4", "m5"])
        # m4 restarts: win's next sync switches to the fleet token for m4 only.
        self.net.restart("m4", machines["m4"][0])
        report = win.sync()
        self.assertEqual(win.outbound_off_fleet(), ["m5"])
        self.assertTrue(any("m4: switched" in line for line in report))
        self.assertTrue(any("m5: still on its old token" in line for line in report))
        # Everyone restarts and syncs; then the old inbound tokens can go.
        for name, (settings, _) in machines.items():
            self.net.restart(name, settings)
        for _, member in machines.values():
            member.sync()
        self.assertEqual(win.prune(), ["m4", "m5"])
        self.assertNotIn("old-", win_settings.env("A2A_PEER_TOKENS"))
        self.assertEqual(win.prune(), [])

    def test_profile_routes_follow_their_machine_token(self):
        machines = self.form_fleet()
        for name, (settings, _) in machines.items():
            self.net.restart(name, settings)
        default_settings, win = machines["win"]
        profile = FakeSettings({}, {"a2a_agents": {"m4-growser": {"url": URLS["m4"] + "/growser/",
                                                                  "auth": {"type": "bearer", "token": "old"}}}})
        growser = fleet.Fleet(profile, "win", source=default_settings, http_get=self.net.http_get)
        growser.sync()
        self.assertEqual(profile.cfg["a2a_agents"]["m4-growser"]["auth"]["token"], "${A2A_FLEET_TOKEN_M4}")
        self.assertEqual(profile.env("A2A_FLEET_TOKEN_M4"), default_settings.env("A2A_FLEET_TOKEN_M4"))
        self.assertNotIn("A2A_PEER_TOKENS", profile.env_values, "a secondary profile serves no inbound")
        with self.assertRaises(ValueError):
            growser.add("m6", "http://x:9900")

    def test_remove_drops_the_member_everywhere(self):
        machines = self.form_fleet()
        settings, win = machines["win"]
        win.remove("m5")
        self.assertNotIn("m5", win.members())
        self.assertNotIn("m5", settings.cfg["a2a_agents"])
        self.assertNotIn("m5", settings.env("A2A_PEER_TOKENS") + settings.env("A2A_TRUSTED_PEERS"))
        self.assertEqual(settings.env("A2A_FLEET_TOKEN_M5"), "")
        # m4 re-joining from the new invite forgets m5 too.
        m4_settings, m4 = machines["m4"]
        m4.join(win.invite_code())
        self.assertNotIn("m5", m4.members())

    def test_join_refuses_codes_without_this_machine(self):
        _, win = machine("win", self.net)
        win.add("win", URLS["win"])
        _, m6 = machine("m6", self.net)
        with self.assertRaises(ValueError):
            m6.join(win.invite_code())

    def test_sync_without_a_fleet_explains_what_to_do(self):
        _, m4 = machine("m4", self.net)
        with self.assertRaisesRegex(ValueError, "fleet join"):
            m4.sync()


class CommandTests(unittest.TestCase):
    def run_cli(self, member, *argv):
        parser = argparse.ArgumentParser()
        cli.build_parser(parser)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.FleetCommand(member).run(parser.parse_args(["fleet", *argv]))
        return code, out.getvalue()

    def test_invite_prints_a_join_command_and_show_never_prints_secrets(self):
        net = Network()
        settings, win = machine("win", net)
        self.assertEqual(self.run_cli(win, "add", "win", URLS["win"])[0], 0)
        self.run_cli(win, "add", "m4", URLS["m4"])
        code, out = self.run_cli(win, "invite")
        self.assertEqual(code, 0)
        self.assertIn("hermes a2a-relay fleet join a2ar1.", out)
        _, shown = self.run_cli(win, "show")
        self.assertIn("m4", shown)
        secret = settings.env(fleet.SECRET_ENV)
        for value in (secret, settings.env("A2A_FLEET_TOKEN_M4")):
            self.assertTrue(value)
            self.assertNotIn(value, shown)

    def test_errors_are_reported_with_exit_code_1(self):
        _, win = machine("win", Network())
        code, out = self.run_cli(win, "add", "Bad Name", "http://x")
        self.assertEqual(code, 1)
        self.assertIn("bad machine name", out)


if __name__ == "__main__":
    unittest.main()
