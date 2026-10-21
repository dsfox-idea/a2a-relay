"""Run: cd ~/.hermes/hermes-agent && hermes --run-module unittest discover -s <repo>/tests"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_jobs import _load

restart = _load("restart")


class FakeRunner:
    """Just the GatewayRunner surface the tool touches, with a real event loop on a thread."""

    def __init__(self, accept=True, active=1):
        self._gateway_loop = asyncio.new_event_loop()
        threading.Thread(target=self._gateway_loop.run_forever, daemon=True).start()
        self._restart_requested = False
        self._draining = False
        self.accept, self.active, self.calls = accept, active, []

    def request_restart(self, *, detached=False, via_service=False):
        self.calls.append((detached, via_service, threading.current_thread() is not threading.main_thread()))
        self._restart_requested = self.accept
        return self.accept

    def _active_work_count(self):
        return self.active

    def close(self):
        self._gateway_loop.call_soon_threadsafe(self._gateway_loop.stop)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = Path(self.tmp.name) / "restart_notice.json"
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, runner, results=None):
        results = list(results or [])

        def inject(key, text):
            self.sent.append((key, text))
            return results.pop(0) if results else True
        return restart.GatewayRestart(self.marker, inject, runner=lambda: runner, retry_seconds=0)

    def test_request_goes_through_the_gateway_loop_and_leaves_a_marker(self):
        runner = FakeRunner()
        try:
            with mock.patch.object(restart, "_via_service", return_value=False):
                out = self.tool(runner).request(announce_to="agent:main:local:dm:local-1")
            self.assertIn("Gateway restart scheduled", out)
            self.assertEqual(runner.calls, [(True, False, True)], "detached watcher, called on the loop thread")
            data = json.loads(self.marker.read_text(encoding="utf-8"))
            self.assertEqual((data["session_key"], data["pid"]), ("agent:main:local:dm:local-1", os.getpid()))
        finally:
            runner.close()

    def test_service_managed_gateway_exits_to_its_supervisor(self):
        runner = FakeRunner()
        try:
            with mock.patch.object(restart, "_via_service", return_value=True):
                self.tool(runner).request()
            self.assertEqual(runner.calls[0][:2], (False, True))
            self.assertFalse(self.marker.exists(), "no chat to announce to")
        finally:
            runner.close()

    def test_second_request_and_refusal_are_reported(self):
        runner = FakeRunner(accept=False)
        try:
            with mock.patch.object(restart, "_via_service", return_value=False):
                self.assertIn("refused", self.tool(runner).request(announce_to="k"))
            self.assertFalse(self.marker.exists())
            runner._restart_requested = True
            self.assertIn("already in progress", self.tool(runner).request())
        finally:
            runner.close()

    def test_new_process_announces_once_and_retries_until_the_chat_is_back(self):
        self.marker.write_text(json.dumps({"session_key": "k", "pid": -1, "ts": 0}), encoding="utf-8")
        tool = self.tool(object(), results=[False, False, True])
        tool._clock = lambda: 25.0
        self.assertTrue(tool.announce())
        self.assertEqual(len(self.sent), 3)
        self.assertIn("было PID -1", self.sent[-1][1])
        self.assertIn("через 25 с", self.sent[-1][1])
        self.assertFalse(self.marker.exists())
        self.assertFalse(tool.announce(), "announced only once")

    def test_marker_of_this_process_or_too_old_is_ignored(self):
        self.marker.write_text(json.dumps({"session_key": "k", "pid": os.getpid(), "ts": 0}), encoding="utf-8")
        self.assertIsNone(self.tool(object()).pending_announcement())
        self.marker.write_text(json.dumps({"session_key": "k", "pid": -1, "ts": 0}), encoding="utf-8")
        tool = self.tool(object())
        tool._clock = lambda: 10_000.0
        self.assertIsNone(tool.pending_announcement())
        self.assertFalse(self.marker.exists())

    def test_runner_lookup_never_imports_the_gateway(self):
        with mock.patch.dict("sys.modules", {"gateway.run": None}):
            self.assertIsNone(restart._gateway_runner())


if __name__ == "__main__":
    unittest.main()
