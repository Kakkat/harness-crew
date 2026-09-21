import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest

from triad.backends import ENTRY, alive
from triad.client import Client
from triad.core import initialize
from triad.util import TriadError, atomic_json, read_json


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace, self.state = self.root / "workspace", self.root / "state"
        self.workspace.mkdir()
        initialize(self.state, self.workspace)
        self.proc = None
        self.client = Client(self.state)
        self.launch_controller()
        self.client.call("design", {"text": "Create greeting.txt containing hello triad. Preserve other files."})

    def launch_controller(self):
        self.proc = subprocess.Popen([sys.executable, str(ENTRY), "--state", str(self.state), "serve"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait(lambda: self.client.call("status"), timeout=10)

    def wait(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except (TriadError, FileNotFoundError) as exc:
                last = str(exc)
            time.sleep(0.1)
        self.fail(f"Timed out: {last}; state={self.state}")

    def start(self, role, profile=None):
        result = self.client.call("start", {"role": role, "profile": profile or "demo-" + role})
        self.wait(lambda: any(s["role"] == role and s["turn"] == "ready" for s in self.client.call("status")["sessions"]))
        return result

    def task(self, checks=None):
        return self.client.call("create_task", {"objective": "Create greeting", "checks": checks or [{"name": "greeting",
            "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('greeting.txt').read_text() == 'hello triad\\n'"],
            "timeout": 10}]})

    def tearDown(self):
        try:
            if self.proc.poll() is not None:
                self.launch_controller()
            self.client.call("stop")
            self.client.call("shutdown")
            self.proc.wait(timeout=10)
        finally:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.temp.cleanup()

    def test_complete_two_tasks_in_same_persistent_sessions(self):
        self.start("worker")
        self.start("supervisor")
        first = self.task()
        self.wait(lambda: self.client.call("task", {"task": first["id"]})["state"] == "accepted")
        second = self.task()
        self.wait(lambda: self.client.call("task", {"task": second["id"]})["state"] == "accepted")
        self.assertEqual([s["generation"] for s in self.client.call("status")["sessions"]], [1, 1])
        self.assertLess(self.client.call("storage")["used_bytes"], 1024 * 1024)

    def test_controller_crash_keeps_worker_and_recovers(self):
        session = self.start("worker")
        host_path = Path(session["spec"]).parent / "host.json"
        pid = read_json(host_path)["pid"]
        self.proc.kill()
        self.proc.wait(timeout=5)
        self.assertTrue(alive(host_path.parent))
        self.launch_controller()
        self.start("supervisor")
        task = self.task()
        self.wait(lambda: self.client.call("task", {"task": task["id"]})["state"] == "accepted")
        self.assertEqual(read_json(host_path)["pid"], pid)

    def test_replacement_preserves_dirty_files_and_revokes_old_authority(self):
        first = self.start("worker")
        spec = read_json(Path(first["spec"]))
        (self.workspace / "keep.txt").write_text("user work")
        config = read_json(self.state / "config.json")
        config["profiles"]["replacement"] = dict(config["profiles"]["demo-worker"])
        atomic_json(self.state / "config.json", config)
        replacement = self.client.call("replace", {"role": "worker", "profile": "replacement"})
        self.wait(lambda: self.client.call("status")["sessions"][0]["turn"] == "ready")
        self.assertEqual(replacement["generation"], 2)
        self.assertFalse(alive(Path(first["spec"]).parent))
        self.assertEqual((self.workspace / "keep.txt").read_text(), "user work")
        with self.assertRaises(TriadError):
            Client(self.state, spec["token"]).call("ready")
        self.assertTrue((Path(replacement["spec"]).parent / "handoff.json").exists())

    def test_timed_out_check_is_not_accepted(self):
        self.start("worker")
        self.start("supervisor")
        task = self.task([{"name": "hang", "argv": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout": 0.2}])
        self.wait(lambda: self.client.call("task", {"task": task["id"]})["state"] == "blocked")
        run = self.client.call("status")["runs"][0]
        self.assertTrue(run["timed_out"])
        self.assertFalse(run["valid"])

    def test_output_flood_is_capped(self):
        self.start("worker")
        self.start("supervisor")
        task = self.task([{"name": "output", "argv": [sys.executable, "-c", "print('x' * 3000000)"], "timeout": 10}])
        self.wait(lambda: self.client.call("task", {"task": task["id"]})["state"] == "accepted")
        run = self.client.call("status")["runs"][0]
        self.assertTrue(run["truncated"])
        log = self.state / "evidence" / run["id"] / "stdout.log"
        self.assertLessEqual(log.stat().st_size, 1024 * 1024)

    @unittest.skipUnless(os.name == "nt" and shutil.which("psmux"), "Native psmux is not installed")
    def test_replace_jsonl_worker_with_cooperative_worker_in_psmux(self):
        first = self.start("worker")
        config = read_json(self.state / "config.json")
        script = ENTRY.parent / "examples" / "cooperative_worker.py"
        config["profiles"]["cooperative"] = {"mode": "cooperative", "argv": [sys.executable, str(script)]}
        atomic_json(self.state / "config.json", config)
        replacement = self.client.call("replace", {"role": "worker", "profile": "cooperative", "backend": "psmux"})
        self.wait(lambda: self.client.call("status")["sessions"][0]["turn"] == "ready", timeout=30)
        self.start("supervisor")
        task = self.task()
        self.wait(lambda: self.client.call("task", {"task": task["id"]})["state"] == "accepted", timeout=30)
        self.assertEqual(replacement["generation"], 2)
        self.assertFalse(alive(Path(first["spec"]).parent))


if __name__ == "__main__":
    unittest.main()
