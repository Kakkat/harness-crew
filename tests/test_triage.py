import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from triad import backends
from triad.connections import LocalConnection
from triad.core import DOORBELL_MAX, Core, bootstrap_text, initialize
from triad.util import TriadError, atomic_json, now, uid


class TriageTests(unittest.TestCase):
    """Deterministic triage: the Supervisor is woken only when it has something to decide."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        initialize(self.root / "state", self.workspace)
        self.core = Core(self.root / "state")
        self.admin = self.core.config["admin_token"]
        self.tokens = {role: secrets.token_hex(32) for role in ("worker", "supervisor")}
        for role, token in self.tokens.items():
            spec = self.root / f"{role}.json"
            atomic_json(spec, {"session_name": f"triad-test-{role}-g1", "doorbell_text": "[triad] ring"})
            with self.core.store.db:
                self.core.store.put("sessions", {"role": role, "generation": 1, "token": token, "state": "alive",
                                                 "turn": "ready", "heartbeat": now(), "created": now(),
                                                 "spec": str(spec), "backend": "local", "doorbell": False})
        self.call("design", {"text": "Implement exactly the requested behavior"})
        self.drain("supervisor")

    def tearDown(self):
        self.core.store.close()
        self.temp.cleanup()

    def call(self, action, data=None, role="designer"):
        return self.core.rpc(self.admin if role == "designer" else self.tokens[role],
                             {"id": uid(), "action": action, "data": data or {}})

    def drain(self, role):
        """Deliver and acknowledge everything queued for a role; return the message types."""
        seen = []
        while (message := self.call("inbox", role=role)) is not None:
            seen.append(message["type"])
            self.call("ack", {"message": message["id"]}, role=role)
            self.call("ready", role=role)
        return seen

    def queued(self, role):
        return [json.loads(d)["type"] for (d,) in self.core.store.db.execute(
            "SELECT data FROM messages WHERE recipient=? AND state='queued' ORDER BY created", (role,))]

    def task(self):
        return self.call("create_task", {"objective": "Write a file", "checks": [
            {"name": "unit", "argv": [sys.executable, "-c", "pass"]}]})

    def age(self, table, key, **fields):
        with self.core.store.db:
            record = self.core.store.get(table, key)
            record.update(fields)
            self.core.store.put(table, record)

    def tick(self, method):
        with self.core.store.db:
            getattr(self.core, method)()

    def test_ready_worker_wakes_supervisor_only_to_assign(self):
        self.call("ready", role="worker")
        self.assertEqual(self.queued("supervisor"), [])  # Nothing to decide.
        task = self.task()
        self.drain("supervisor")
        self.call("ready", role="worker")
        self.assertEqual(self.queued("supervisor"), ["worker_ready"])  # A queued task needs assigning.
        self.drain("supervisor")
        self.call("assign", {"task": task["id"]}, role="supervisor")
        self.drain("worker")  # Worker handles the assignment and declares ready again.
        self.assertEqual(self.queued("supervisor"), [])  # Active task, no result: not the Supervisor's turn.

    def test_result_is_delivered_with_the_worker_ready(self):
        task = self.task()
        self.drain("supervisor")
        self.call("assign", {"task": task["id"]}, role="supervisor")
        message = self.call("inbox", role="worker")
        self.call("ack", {"message": message["id"]}, role="worker")
        run = self.call("run_start", {"task": task["id"], "check": "unit"}, role="worker")
        self.call("run_finish", {"run": run["id"], "exit_code": 0}, role="worker")
        self.call("result", {"task": task["id"], "summary": "done", "evidence": [run["id"]]}, role="worker")
        self.assertEqual(self.core.session("worker")["turn"], "ready")  # A result also ends the Worker's turn.
        self.assertEqual(self.queued("supervisor"), ["result"])  # One message, acceptable immediately.
        self.call("ready", role="worker")  # A Worker that still calls ready adds nothing.
        self.assertEqual(self.queued("supervisor"), ["result"])
        self.drain("supervisor")
        self.assertEqual(self.call("accept", {"task": task["id"]}, role="supervisor")["state"], "accepted")

    def test_result_is_delivered_anyway_if_worker_never_becomes_ready(self):
        task = self.task()
        self.drain("supervisor")
        self.call("assign", {"task": task["id"]}, role="supervisor")
        message = self.call("inbox", role="worker")
        self.call("ack", {"message": message["id"]}, role="worker")
        self.call("ready", role="worker")
        self.call("correct", {"task": task["id"], "instruction": "One more thing"})
        self.call("inbox", role="worker")  # Left unacknowledged, so the result cannot end the turn.
        run = self.call("run_start", {"task": task["id"], "check": "unit"}, role="worker")
        self.call("run_finish", {"run": run["id"], "exit_code": 0}, role="worker")
        self.call("result", {"task": task["id"], "summary": "done", "evidence": [run["id"]]}, role="worker")
        result = self.core.task(task["id"])["result"]
        self.age("tasks", task["id"], result=result | {"submitted_at": now() - 61})
        self.tick("triage")
        message = self.call("inbox", role="supervisor")
        self.assertEqual((message["type"], message["body"]["worker_ready"]), ("result", False))

    def test_idle_worker_is_nudged_before_supervisor_is_woken(self):
        task = self.task()
        self.drain("supervisor")
        self.call("assign", {"task": task["id"]}, role="supervisor")
        self.drain("worker")  # Worker "finishes" without checks or a result, then idles.
        self.tick("triage")
        self.assertEqual(self.queued("worker"), [])  # Not yet: it may still continue on its own.
        for nudge in (1, 2):
            self.age("sessions", "worker", ready_at=now() - 61)
            if nudge == 2:
                self.age("tasks", task["id"], nudged_at=now() - 61)
            self.tick("triage")
            message = self.call("inbox", role="worker")
            self.assertEqual(message["type"], "nudge")
            self.assertIn(f"check --task {task['id']} --name NAME", message["body"]["instruction"])
            self.assertIn(f"result --task {task['id']}", message["body"]["instruction"])
            self.call("ack", {"message": message["id"]}, role="worker")
            self.call("ready", role="worker")
            self.assertEqual(self.queued("supervisor"), [])
        self.age("sessions", "worker", ready_at=now() - 61)
        self.age("tasks", task["id"], nudged_at=now() - 61)
        self.tick("triage")
        self.tick("triage")
        self.assertEqual(self.queued("supervisor"), ["worker_idle"])  # Once, after the nudges failed.

    def test_quiet_warning_only_for_a_busy_worker(self):
        task = self.task()
        self.drain("supervisor")
        self.call("assign", {"task": task["id"]}, role="supervisor")
        self.drain("worker")
        self.age("tasks", task["id"], assigned_at=now() - 301)
        self.core.tick()
        self.assertNotIn("progress_review", self.queued("supervisor"))  # Idle: triage nudges instead.
        self.age("sessions", "worker", turn="running")
        self.core.tick()
        self.assertIn("progress_review", self.queued("supervisor"))

    def test_doorbell_rings_idle_agent_with_waiting_message(self):
        rings = []
        real = LocalConnection.session

        def session(connection, operation, record):
            if operation == "ring":
                return rings.append(record["role"])
            return real(connection, operation, record)

        self.age("sessions", "supervisor", doorbell=True)
        self.task()  # Queues task_created for the Supervisor.
        with patch.object(LocalConnection, "session", session):
            self.tick("ring_doorbells")
            self.assertEqual(rings, [])  # Too fresh: a polling agent would collect it itself.
            with self.core.store.db:
                self.core.store.db.execute("UPDATE messages SET created=created-4 WHERE recipient='supervisor'")
            self.tick("ring_doorbells")
            self.tick("ring_doorbells")
            self.assertEqual(rings, ["supervisor"])  # One ring, not one per tick.
            for _ in range(5):
                self.age("sessions", "supervisor", rung_at=now() - 61)
                self.tick("ring_doorbells")
            self.assertEqual(len(rings), DOORBELL_MAX)  # Bounded retries.
            self.drain("supervisor")
            self.assertEqual(self.core.session("supervisor")["rings"], 0)  # Reset by delivery.
            self.age("sessions", "worker", doorbell=True, turn="running")
            with self.core.store.db:
                self.core.store.enqueue("worker", 1, "note", {})
                self.core.store.db.execute("UPDATE messages SET created=created-4 WHERE recipient='worker'")
            self.tick("ring_doorbells")
            self.assertEqual(len(rings), DOORBELL_MAX)  # A busy agent is never typed at.
            self.age("sessions", "worker", turn="ready")
            self.tick("ring_doorbells")
            self.assertEqual(rings[-1], "worker")  # Once idle, it is.

    def test_instructions_are_role_specific_with_exact_commands(self):
        worker = bootstrap_text("worker", "/s/handoff.json", "CLI", doorbell=True)
        self.assertIn("CLI check --task T --name NAME", worker)
        self.assertIn('CLI result --task T --summary "one line" --evidence RUN_ID', worker)
        self.assertIn("END YOUR TURN", worker)
        self.assertNotIn("CLI inbox --wait", worker)
        supervisor = bootstrap_text("supervisor", "/s/handoff.json", "CLI", doorbell=False)
        self.assertIn("CLI accept --task T", supervisor)
        self.assertIn("CLI inbox --wait", supervisor)
        self.assertNotIn("check --task", supervisor)


class MultiplexerTargetTests(unittest.TestCase):
    def test_tmux_uses_exact_targets_and_rings_with_literal_text(self):
        with tempfile.TemporaryDirectory() as temp:
            spec = Path(temp) / "spec.json"
            atomic_json(spec, {"session_name": "triad-x-worker-g1", "doorbell_text": "[triad] Enter inbox"})
            for name, pane in [("tmux", "=triad-x-worker-g1:"), ("psmux", "triad-x-worker-g1")]:
                with patch("triad.backends.shutil.which", return_value="/bin/" + name):
                    mux = backends.MuxBackend(name)
                with patch.object(mux, "command") as command:
                    mux.ring(str(spec))
                self.assertEqual([c.args for c in command.call_args_list],
                                 [("send-keys", "-t", pane, "-l", "[triad] Enter inbox"), ("send-keys", "-t", pane, "Enter")])
                self.assertEqual(mux.attach(str(spec))[-1], pane.rstrip(":"))

    @unittest.skipUnless(os.name != "nt" and __import__("shutil").which("tmux"), "tmux")
    def test_exact_target_never_matches_a_longer_session_name(self):
        subprocess.run(["tmux", "new-session", "-d", "-s", "triad-prefixtest-g10", "sleep 30"], check=True)
        try:
            mux = backends.MuxBackend("tmux")
            self.assertNotEqual(mux.command("has-session", "-t", mux.target("triad-prefixtest-g1"), check=False).returncode, 0)
            mux.command("kill-session", "-t", mux.target("triad-prefixtest-g1"), check=False)
            self.assertEqual(mux.command("has-session", "-t", mux.target("triad-prefixtest-g10"), check=False).returncode, 0)
        finally:
            subprocess.run(["tmux", "kill-session", "-t", "=triad-prefixtest-g10"], capture_output=True)


@unittest.skipIf(os.name == "nt", "POSIX process freezing")
class FrozenProcessTests(unittest.TestCase):
    def test_failed_listing_never_leaves_processes_frozen(self):
        victim = subprocess.Popen(["sleep", "60"])
        try:
            with patch.object(backends, "posix_processes", side_effect=TriadError("ps timed out")):
                with self.assertRaises(TriadError):
                    backends.kill_posix_tree(victim.pid)
            victim.wait(timeout=5)  # Killed, not left stopped.
            self.assertEqual(victim.returncode, -9)
        finally:
            if victim.poll() is None:
                victim.kill()
                victim.wait()


if __name__ == "__main__":
    unittest.main()
