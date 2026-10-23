"""Run: cd <hermes-agent> && hermes --run-module unittest discover -s <repo>/tests"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_jobs import _load

cdoc = _load("coder_doctor")
coder = _load("coder")
doctor = _load("doctor")


class CoderDoctorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.relay = self.tmp / "a2a_relay"
        self.relay.mkdir()
        self.root = self.tmp / "dev"
        self.root.mkdir()
        self.on_path = {"acpx": "/bin/acpx", "node": "/bin/node", "claude": "/bin/claude", "npx": "/bin/npx"}
        self.outputs = {"--version": coder.ACPX_VERSION, "node": "v24.19.0"}

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, **overrides):
        cfg = {**cdoc.default_config([str(self.root)]), **overrides}
        (self.relay / "coder.json").write_text(json.dumps(cfg), encoding="utf-8")

    def output(self, argv):
        if argv[0] == "node":
            return self.outputs["node"]
        return self.outputs["--version"]

    def run_checks(self):
        checks = cdoc.CoderDoctor(self.relay, which=self.on_path.get, output=self.output).run()
        return {c.name: c for c in checks}

    def test_no_config_is_a_warning_with_the_init_command(self):
        check = self.run_checks()["coding executors"]
        self.assertEqual(check.status, doctor.WARN)
        self.assertEqual(check.fixes, [cdoc.INIT_FIX])

    def test_healthy(self):
        self.write()
        checks = self.run_checks()
        self.assertEqual({n: c.status for n, c in checks.items() if c.status != doctor.OK}, {})
        self.assertIn("mode default", checks["executor claude"].detail)

    def test_broken_config_fails(self):
        (self.relay / "coder.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(self.run_checks()["coding executors"].status, doctor.FAIL)

    def test_problems(self):
        cases = [
            (lambda: self.on_path.pop("acpx"), "acpx", doctor.FAIL),
            (lambda: self.outputs.__setitem__("--version", "0.19.0"), "acpx", doctor.WARN),
            (lambda: self.outputs.__setitem__("node", "v22.12.0"), "node for acpx", doctor.FAIL),
            (lambda: self.outputs.__setitem__("node", ""), "node for acpx", doctor.FAIL),
            (lambda: self.on_path.pop("claude"), "executor claude", doctor.FAIL),
            (lambda: self.write(session_modes={"claude": "auto"}), "executor claude", doctor.FAIL),
            (lambda: self.write(timeout_s=300), "coding timeout", doctor.WARN),
            (lambda: self.write(roots=[str(self.tmp / "missing")]), "coding roots", doctor.FAIL),
            (lambda: self.write(executors=["claude", "pi"]), "executor pi", doctor.WARN),
        ]
        for breaks, name, status in cases:
            with self.subTest(name=name, status=status):
                self.setUp()
                self.write()
                breaks()
                self.assertEqual(self.run_checks()[name].status, status)

    def test_doctor_includes_the_coder_checks(self):
        from test_doctor import facts
        host = facts()
        host.coder = lambda: [doctor.Check("coding executors", doctor.WARN, "off")]
        names = [c.name for c in doctor.Doctor(host).run(probe_peers=False)]
        self.assertIn("coding executors", names)


class InitTests(unittest.TestCase):
    def test_init_writes_once_and_validates_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay, home = Path(tmp) / "a2a_relay", Path(tmp) / "home"
            with self.assertRaises(coder.CoderError):
                cdoc.init_config(relay, [], home=home)
            with self.assertRaises(coder.CoderError):
                cdoc.init_config(relay, [str(Path(tmp) / "missing")], home=home)
            (home / "dev").mkdir(parents=True)
            self.assertIn("Wrote", cdoc.init_config(relay, [], home=home))
            cfg = coder.CoderConfig.load(relay / "coder.json")
            self.assertEqual((cfg.executors, cfg.session_mode("claude")), (("claude",), "default"))
            self.assertIn("already exists", cdoc.init_config(relay, [], home=home))


if __name__ == "__main__":
    unittest.main()
