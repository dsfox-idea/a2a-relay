"""Run: cd <hermes-agent> && hermes --run-module unittest discover -s <repo>/tests"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request

from test_jobs import _load

sessions = _load("sessions")
fleet = _load("fleet")

SECRET = "s3cret"


class FakeFleet:
    def __init__(self, me="win", peers=None, secret=SECRET):
        self.me = me
        self._peers = peers if peers is not None else {"m4": "http://127.0.0.1:9900", "m5": "http://127.0.0.1:9900"}
        self._secret = secret

    def secret(self):
        return self._secret

    def peers(self):
        return dict(self._peers)

    def members(self):
        return {self.me: "http://127.0.0.1:9900", **self._peers}


def chat(sid, **extra):
    return {"id": sid, "session_key": f"agent:main:local:dm:{sid}", "title": "", "profile": "default",
            "source": "tui", "status": "idle", "model": "opus", "cwd": "", "started_at": 1, "last_active": 2,
            "preview": "", **extra}


class LocalSessionsTests(unittest.TestCase):
    def test_cards_carry_the_fields_and_the_machine(self):
        local = sessions.LocalSessions(lambda: "win", lambda: [chat("s1", title="Build", status="working"),
                                                                chat("s3", profile="growser")])
        cards = {c["id"]: c for c in local.cards()}
        self.assertEqual(set(cards), {"s1", "s3"})
        self.assertEqual((cards["s1"]["title"], cards["s1"]["status"], cards["s1"]["machine"]),
                         ("Build", "working", "win"))
        self.assertEqual((cards["s3"]["profile"], cards["s3"]["repo"]), ("growser", ""))

    def test_no_gateway_means_no_cards(self):
        self.assertEqual(sessions.LocalSessions(lambda: "win").cards(), [])

    def test_profile_name(self):
        self.assertEqual(sessions._profile_name("C:/Users/x/AppData/Local/hermes/profiles/growser"), "growser")
        self.assertEqual(sessions._profile_name("C:/Users/x/AppData/Local/hermes"), "default")


class GuardTests(unittest.TestCase):
    def test_one_server_per_process_across_plugin_copies(self):
        self.assertIs(sessions._process_guard(), sessions._process_guard())
        self.assertIs(sessions._process_guard().lock, sessions._process_guard().lock)


class AuthTests(unittest.TestCase):
    def test_only_a_member_token_for_this_receiver_passes(self):
        auth = sessions.FleetAuth(lambda: FakeFleet())
        self.assertEqual(auth.caller("Bearer " + fleet.pair_token(SECRET, "m4", "win")), "m4")
        self.assertIsNone(auth.caller("Bearer " + fleet.pair_token(SECRET, "win", "m4")), "wrong direction")
        self.assertIsNone(auth.caller("Bearer " + fleet.pair_token(SECRET, "evil", "win")), "not a member")
        self.assertIsNone(auth.caller("Bearer " + fleet.pair_token("other", "m4", "win")))
        self.assertIsNone(auth.caller(""))
        self.assertIsNone(sessions.FleetAuth(lambda: FakeFleet(secret="")).caller("Bearer x"))


class ServerTests(unittest.TestCase):
    def setUp(self):
        local = sessions.LocalSessions(lambda: "win", lambda: [chat("local-1")])
        self.server = sessions.SessionServer(local, sessions.FleetAuth(lambda: FakeFleet()))
        self.httpd = self.server.bind("127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers), timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def test_auth_and_routes(self):
        self.assertEqual(self.get("/sessions")[0], 401)
        self.assertEqual(self.get("/sessions", "nope")[0], 401)
        self.assertEqual(self.get("/other", fleet.pair_token(SECRET, "m4", "win"))[0], 404)
        status, body = self.get("/sessions", fleet.pair_token(SECRET, "m4", "win"))
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in body["sessions"]], ["local-1"])


class DirectoryTests(unittest.TestCase):
    def directory(self, http_get, local_cards=()):
        local = sessions.LocalSessions(lambda: "win", lambda: list(local_cards))
        fake = FakeFleet()
        return sessions.SessionDirectory(local, sessions.FleetAuth(lambda: fake), fake.members, http_get=http_get)

    def test_merges_members_and_reports_the_unreachable(self):
        seen = []

        def http_get(url, token):
            seen.append((url, token))
            if token == fleet.pair_token(SECRET, "win", "m4"):
                return 200, {"sessions": [{"machine": "m4", "session_key": "local-a", "status": "idle",
                                           "repo": "growser", "profile": "growser", "last_active": 1}]}
            return 0, "timed out"

        directory = self.directory(http_get, [chat("local-w")])
        cards, problems = directory.collect()
        self.assertEqual(sorted(c["machine"] for c in cards), ["m4", "win"])
        self.assertEqual(problems, {"m5": "no answer (timed out)"})
        self.assertTrue(all(url == "http://127.0.0.1:9901/sessions" for url, _ in seen), seen)
        text = directory.describe()
        self.assertIn("m5: not reachable", text)
        self.assertIn("growser@", text)

    def test_filters(self):
        cards = [{"machine": "m4", "repo": "growser", "profile": "growser", "status": "idle", "last_active": 1},
                 {"machine": "m5", "repo": "ice9", "profile": "default", "status": "working", "last_active": 2}]
        select = sessions.SessionDirectory.select
        self.assertEqual([c["machine"] for c in select(cards, machine="m5")], ["m5"])
        self.assertEqual([c["machine"] for c in select(cards, repo="GROW")], ["m4"])
        self.assertEqual([c["machine"] for c in select(cards, profile="default")], ["m5"])
        self.assertEqual([c["machine"] for c in select(cards, idle=True)], ["m4"])
        self.assertEqual([c["machine"] for c in select(cards, idle=False)], ["m5"])

    def test_sessions_url(self):
        self.assertEqual(sessions.sessions_url("http://100.69.38.35:9900"), "http://100.69.38.35:9901")
        self.assertEqual(sessions.sessions_url("http://100.96.62.85:9900/growser/"), "http://100.96.62.85:9901")


if __name__ == "__main__":
    unittest.main()
