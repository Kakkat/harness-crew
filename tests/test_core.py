import json
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

from triad.core import Core, initialize
from triad.util import CappedLog, FileLock, TriadError, fingerprint, uid


class CoreTests(unittest.TestCase):
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
            with self.core.store.db:
                self.core.store.put("sessions", {"role": role, "generation": 1, "token": token,
                                                "state": "alive", "turn": "ready", "heartbeat": 1, "created": 1})
        self.call("design", {"text": "Implement exactly the requested behavior"})

    def tearDown(self):
        self.core.store.close()
        self.temp.cleanup()

    def call(self, action, data=None, role="designer", rid=None):
        return self.core.rpc(self.admin if role == "designer" else self.tokens[role],
                             {"id": rid or uid(), "action": action, "data": data or {}})

    def task(self):
        return self.call("create_task", {"objective": "Write a file", "checks": [
            {"name": "assert", "argv": [sys.executable, "-c", "pass"]}]})

    def assign(self):
        task = self.task()
        self.call("assign", {"task": task["id"]}, role="supervisor")
        message = self.call("inbox", role="worker")
        self.call("ack", {"message": message["id"]}, role="worker")
        return task

    def passing(self, task):
        run = self.call("run_start", {"task": task["id"], "check": "assert"}, role="worker")
        self.call("run_finish", {"run": run["id"], "exit_code": 0}, role="worker")
        self.call("result", {"task": task["id"], "summary": "Implemented", "evidence": [run["id"]]}, role="worker")
        return run

    def test_worker_cannot_accept_or_assign(self):
        for action in ["accept", "assign", "start", "create_task", "shutdown"]:
            with self.assertRaises(TriadError):
                self.call(action, role="worker")

    def test_supervisor_cannot_replace_itself(self):
        with self.assertRaises(TriadError):
            self.call("replace", {"role": "supervisor"}, role="supervisor")

    def test_duplicate_request_returns_same_task(self):
        body = {"objective": "x", "checks": [{"name": "x", "argv": [sys.executable, "-c", "pass"]}]}
        first = self.call("create_task", body, rid="repeat")
        self.assertEqual(first, self.call("create_task", body, rid="repeat"))
        self.assertEqual(len(self.core.store.all("tasks")), 1)

    def test_request_id_cannot_change_payload(self):
        self.call("pause", rid="same")
        with self.assertRaises(TriadError):
            self.call("resume", rid="same")

    def test_submitted_message_is_not_blindly_replayed(self):
        task = self.task()
        self.call("assign", {"task": task["id"]}, role="supervisor")
        delivered = self.call("inbox", role="worker", rid="delivery")
        self.assertIsNotNone(delivered)
        self.assertIsNone(self.call("inbox", role="worker"))
        self.assertEqual(delivered, self.call("inbox", role="worker", rid="delivery"))

    def test_ready_requires_ack(self):
        task = self.task()
        self.call("assign", {"task": task["id"]}, role="supervisor")
        self.call("inbox", role="worker")
        with self.assertRaises(TriadError):
            self.call("ready", role="worker")

    def test_one_active_task(self):
        self.assign()
        task = self.task()
        self.call("ready", role="worker")
        with self.assertRaises(TriadError):
            self.call("assign", {"task": task["id"]}, role="supervisor")

    def test_pause_stops_dispatch(self):
        task = self.task()
        self.call("pause")
        with self.assertRaises(TriadError):
            self.call("assign", {"task": task["id"]}, role="supervisor")
        self.assertIsNone(self.call("inbox", role="worker"))
        self.assertIsNotNone(self.call("inbox", role="supervisor"))

    def test_supervisor_takeover_pauses_its_inbox_until_resume(self):
        with self.core.store.db:
            job = self.core.store.meta("job")
            job.update(state="paused", takeover="supervisor")
            self.core.store.set_meta("job", job)
        self.assertIsNone(self.call("inbox", role="supervisor"))
        self.call("resume")
        self.assertIsNotNone(self.call("inbox", role="supervisor"))

    def test_result_is_not_acceptance(self):
        task = self.assign()
        self.passing(task)
        with self.assertRaises(TriadError):
            self.call("accept", {"task": task["id"]}, role="supervisor")
        self.call("ready", role="worker")
        accepted = self.call("accept", {"task": task["id"]}, role="supervisor")
        self.assertEqual(accepted["state"], "accepted")

    def test_changed_workspace_invalidates_evidence(self):
        task = self.assign()
        self.passing(task)
        self.call("ready", role="worker")
        (self.workspace / "new.txt").write_text("changed")
        with self.assertRaisesRegex(TriadError, "stale"):
            self.call("accept", {"task": task["id"]}, role="supervisor")

    def test_changes_during_check_invalidate_it(self):
        task = self.assign()
        run = self.call("run_start", {"task": task["id"], "check": "assert"}, role="worker")
        (self.workspace / "source.txt").write_text("changed while testing")
        result = self.call("run_finish", {"run": run["id"], "exit_code": 0}, role="worker")
        self.assertFalse(result["valid"])

    def test_failed_latest_run_cannot_be_hidden_by_old_success(self):
        task = self.assign()
        old = self.passing(task)
        run = self.call("run_start", {"task": task["id"], "check": "assert"}, role="worker")
        self.call("run_finish", {"run": run["id"], "exit_code": 1}, role="worker")
        self.call("result", {"task": task["id"], "summary": "done", "evidence": [old["id"]]}, role="worker")
        self.call("ready", role="worker")
        with self.assertRaises(TriadError):
            self.call("accept", {"task": task["id"]}, role="supervisor")

    def test_no_acceptance_with_running_command(self):
        task = self.assign()
        self.call("run_start", {"task": task["id"], "check": "assert"}, role="worker")
        with self.assertRaises(TriadError):
            self.call("ready", role="worker")

    def test_no_result_without_evidence(self):
        task = self.assign()
        with self.assertRaises(TriadError):
            self.call("result", {"task": task["id"], "summary": "done"}, role="worker")

    def test_old_generation_is_rejected(self):
        session = self.core.session("worker")
        session.update(token="new-token", generation=2)
        with self.core.store.db:
            self.core.store.put("sessions", session)
        with self.assertRaises(TriadError):
            self.call("ready", role="worker")

    def test_status_does_not_expose_tokens(self):
        status = json.dumps(self.call("status", role="worker"))
        for token in self.tokens.values():
            self.assertNotIn(token, status)

    def test_storage_admission_limit(self):
        self.core.config["storage_bytes"] = 1
        with self.assertRaisesRegex(TriadError, "Storage"):
            self.core.storage_check()

    def test_durable_state_after_reopen(self):
        task = self.assign()
        self.core.store.close()
        self.core = Core(self.root / "state")
        self.assertEqual(self.core.task(task["id"])["state"], "assigned")

    def test_design_cannot_change_under_active_task(self):
        self.assign()
        with self.assertRaises(TriadError):
            self.call("design", {"text": "Different architecture"})

    def test_capped_log_still_drains_without_growth(self):
        path = self.root / "bounded.log"
        log = CappedLog(path, 128)
        for _ in range(100):
            log.write(b"x" * 1024)
        log.close()
        self.assertLessEqual(path.stat().st_size, 128)
        self.assertIn(b"truncated", path.read_bytes())

    def test_exclusive_controller_lock(self):
        with FileLock(self.root / "lock"):
            with self.assertRaises(TriadError):
                FileLock(self.root / "lock").acquire()

    def test_fingerprint_includes_untracked(self):
        before = fingerprint(self.workspace)
        (self.workspace / "untracked").write_text("x")
        self.assertNotEqual(before, fingerprint(self.workspace))

    def test_polling_does_not_fill_request_database(self):
        count = self.core.store.db.execute("SELECT count(*) FROM requests").fetchone()[0]
        for _ in range(100):
            self.call("inbox", role="worker")
            self.call("heartbeat", role="worker")
        self.assertEqual(count, self.core.store.db.execute("SELECT count(*) FROM requests").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
