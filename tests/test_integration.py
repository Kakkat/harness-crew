import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest

from triad.backends import ENTRY, alive, process_identity
from triad.client import Client
from triad.core import initialize
from triad.util import TriadError, atomic_json, read_json

# Tests use their own controllers. A session credential inherited from an enclosing Triad
# session (for example when a Worker runs this suite) must not leak into test clients.
for _name in [n for n in os.environ if n.startswith("TRIAD_")]:
    del os.environ[_name]

# Orphans a sleeper in its own session, as a daemonizing tool would, then acts as a Worker.
ESCAPING_HARNESS = r'''
import json, os, subprocess, sys, time
subprocess.run([sys.executable, "-c", "import subprocess, sys; "
                "p = subprocess.Popen(['sleep', '300'], start_new_session=True); "
                "open(sys.argv[1], 'w').write(str(p.pid))", sys.argv[1]], check=True)
if sys.argv[2] == "exit":
    while not os.path.exists(sys.argv[1] + ".go"):
        time.sleep(0.05)
    sys.exit(0)
print(json.dumps({"type": "action", "id": "ready", "action": "ready", "data": {}}), flush=True)
for line in sys.stdin:
    pass
'''

# Reuses one action ID for everything and records each delivered message type.
REUSED_ID_HARNESS = r'''
import json, sys
def act(action, data=None):
    print(json.dumps({"type": "action", "id": "same", "action": action, "data": data or {}}), flush=True)
act("ready")
with open(sys.argv[1], "a") as record:
    for line in sys.stdin:
        value = json.loads(line)
        if value["type"] == "message":
            record.write(value["message"]["type"] + "\n")
            record.flush()
            act("ack", {"message": value["message"]["id"]})
            act("ready")
'''

MULTIPLEXER = "psmux" if os.name == "nt" else "tmux"

MARKING_HARNESS = r'''
import json, pathlib, sys
pathlib.Path(sys.argv[1]).touch()
print(json.dumps({"type": "action", "id": "ready", "action": "ready", "data": {}}), flush=True)
for line in sys.stdin:
    pass
'''


class ControllerFixture(unittest.TestCase):
    """Isolated state, workspace and controller; no tests of its own."""

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
        # Runs even if setUp fails, so a controller is never left behind.
        self.addCleanup(lambda proc=self.proc: proc.poll() is None and (proc.kill(), proc.wait(timeout=5)))
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

    def profile(self, name, source, *args):
        script = self.root / (name + ".py")
        script.write_text(source)
        config = read_json(self.state / "config.json")
        config["profiles"][name] = {"mode": "jsonl", "argv": [sys.executable, str(script), *map(str, args)]}
        atomic_json(self.state / "config.json", config)

    def escaped_sleeper(self, record):
        pid = int(self.wait(lambda: record.exists() and record.read_text()))
        identity = process_identity(pid)
        self.assertIsNotNone(identity)
        self.addCleanup(lambda: process_identity(pid) == identity and os.kill(pid, 9))
        return pid, identity

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


class IntegrationTests(ControllerFixture):
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

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux child subreaper")
    def test_stopping_worker_kills_descendants_outside_its_process_group(self):
        record = self.root / "escaped.pid"
        self.profile("escaping", ESCAPING_HARNESS, record, "stay")
        self.start("worker", "escaping")
        pid, identity = self.escaped_sleeper(record)
        self.client.call("interrupt", {"role": "worker"})
        self.wait(lambda: process_identity(pid) != identity, timeout=5)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux child subreaper")
    def test_exiting_harness_leaves_no_adopted_descendants(self):
        record = self.root / "escaped.pid"
        self.profile("escaping", ESCAPING_HARNESS, record, "exit")
        self.client.call("start", {"role": "worker", "profile": "escaping"})
        pid, identity = self.escaped_sleeper(record)
        Path(str(record) + ".go").touch()
        self.wait(lambda: process_identity(pid) != identity, timeout=10)

    def test_jsonl_harness_may_reuse_action_ids(self):
        record = self.root / "received.log"
        self.profile("reused", REUSED_ID_HARNESS, record)
        self.start("worker", "reused")
        task = self.task()
        self.client.call("assign", {"task": task["id"]})
        self.wait(lambda: record.exists() and record.read_text().split() == ["assign"])
        self.client.call("correct", {"task": task["id"], "instruction": "Check the newline"})
        self.wait(lambda: record.read_text().split() == ["assign", "correct"])

    def test_host_of_revoked_session_does_not_spawn_harness(self):
        marker = self.root / "spawned"
        self.profile("marking", MARKING_HARNESS, marker)
        session = self.start("worker", "marking")
        self.client.call("interrupt", {"role": "worker"})
        marker.unlink()
        # A host that only starts running after its session was stopped.
        subprocess.run([sys.executable, str(ENTRY), "host", "--spec", session["spec"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        self.assertFalse(marker.exists())
        self.assertIn("revoked", (Path(session["spec"]).parent / "output.log").read_text())

    @unittest.skipUnless(shutil.which(MULTIPLEXER), f"{MULTIPLEXER} is not installed")
    def test_replace_jsonl_worker_with_cooperative_worker_in_multiplexer(self):
        first = self.start("worker")
        config = read_json(self.state / "config.json")
        script = ENTRY.parent / "examples" / "cooperative_worker.py"
        config["profiles"]["cooperative"] = {"mode": "cooperative", "argv": [sys.executable, str(script)]}
        atomic_json(self.state / "config.json", config)
        replacement = self.client.call("replace", {"role": "worker", "profile": "cooperative", "backend": MULTIPLEXER})
        self.wait(lambda: self.client.call("status")["sessions"][0]["turn"] == "ready", timeout=30)
        self.start("supervisor")
        task = self.task()
        self.wait(lambda: self.client.call("task", {"task": task["id"]})["state"] == "accepted", timeout=30)
        self.assertEqual(replacement["generation"], 2)
        self.assertFalse(alive(Path(first["spec"]).parent))


if __name__ == "__main__":
    unittest.main()
