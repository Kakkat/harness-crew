"""Regressions for the 2026-09-21 review of 6a03d86. Each test failed against that commit."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

import secrets
from unittest.mock import patch

from test_integration import MARKING_HARNESS, ControllerFixture  # Also scrubs inherited TRIAD_* variables.
from triad.backends import ENTRY, alive, process_identity
from triad.client import Client
from triad.connections import LocalConnection
from triad.core import Core, initialize
from triad.store import LIMIT_BYTES, RESERVE_BYTES
from triad.util import TriadError, read_json, uid

# A check that exits successfully while leaving behind a descendant with all standard streams
# redirected to DEVNULL (on POSIX also detached into its own session, as a daemon would be).
# The descendant writes its PID at once, then records completion only after LINGER seconds.
LINGERING_CHECK = r'''
import subprocess, sys
child = ("import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); "
         "time.sleep(float(sys.argv[3])); open(sys.argv[2], 'w').write('done')")
subprocess.Popen([sys.executable, "-c", child, *sys.argv[1:4]], stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
'''


def host_processes(session):
    data = read_json(Path(session["spec"]).parent / "host.json")
    return data["pid"], data["identity"]


class ReviewRegressionTests(ControllerFixture):
    def tearDown(self):
        self.free_database()  # Lets the fixture's stop record normally even if a test failed.
        super().tearDown()

    def lingering_task(self, linger, timeout):
        script = self.root / "lingering_check.py"
        script.write_text(LINGERING_CHECK)
        pid_file, done_file = self.root / "lingering.pid", self.root / "lingering.done"
        task = self.task([{"name": "lingering", "timeout": timeout,
                           "argv": [sys.executable, str(script), str(pid_file), str(done_file), str(linger)]}])
        return task, pid_file, done_file

    def owned_pid(self, pid_file):
        pid = int(self.wait(lambda: pid_file.exists() and pid_file.read_text()))
        identity = process_identity(pid)
        if identity:
            self.addCleanup(lambda: process_identity(pid) == identity and os.kill(pid, 9))
        return pid, identity

    def test_revoked_host_does_not_spawn_during_controller_outage(self):
        marker = self.root / "spawned"
        self.profile("marking", MARKING_HARNESS, marker)
        session = self.start("worker", "marking")
        self.client.call("interrupt", {"role": "worker"})
        marker.unlink()
        self.proc.kill()
        self.proc.wait(timeout=5)
        # The delayed host of the revoked generation starts while nobody can refuse it.
        late = subprocess.Popen([sys.executable, str(ENTRY), "host", "--spec", session["spec"]],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: late.poll() is None and (late.kill(), late.wait()))
        host_json = Path(session["spec"]).parent / "host.json"
        self.wait(lambda: host_json.exists() and read_json(host_json)["pid"] == late.pid)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.assertFalse(marker.exists(), "Revoked Worker spawned during controller outage")
            self.assertIsNone(late.poll(), "Host must wait for authorization, not exit or proceed")
            time.sleep(0.1)
        # Authorization is decided once the controller answers: the revoked host exits unspawned.
        self.launch_controller()
        late.wait(timeout=30)
        self.assertFalse(marker.exists())
        self.assertIn("revoked", (Path(session["spec"]).parent / "output.log").read_text())

    def test_check_is_not_complete_while_redirected_descendant_runs(self):
        self.start("worker")
        self.start("supervisor")
        task, pid_file, done_file = self.lingering_task(linger=2, timeout=60)
        self.wait(lambda: self.client.call("task", {"task": task["id"]})["state"] == "accepted", timeout=60)
        pid, _ = self.owned_pid(pid_file)
        run = self.client.call("status")["runs"][0]
        self.assertTrue(run["valid"])
        # The run finished only after the descendant finished its last write.
        self.assertTrue(done_file.exists(), "Accepted while a check descendant was still running")
        self.assertGreaterEqual(run["finished"], done_file.stat().st_mtime)

    def test_check_timeout_kills_redirected_descendant(self):
        self.start("worker")
        self.start("supervisor")
        task, pid_file, done_file = self.lingering_task(linger=120, timeout=3)
        pid, identity = self.owned_pid(pid_file)
        self.assertIsNotNone(identity)
        self.wait(lambda: self.client.call("task", {"task": task["id"]})["state"] == "blocked", timeout=30)
        run = self.client.call("status")["runs"][0]
        self.assertTrue(run["timed_out"])
        self.assertFalse(run["valid"])
        self.assertNotEqual(process_identity(pid), identity, "Check descendant outlived its timed-out run")
        self.assertFalse(done_file.exists())

    def fill_database(self, limit_bytes):
        """Fill the controller's real database to limit_bytes using a separate connection."""
        db = sqlite3.connect(self.state / "state.sqlite", timeout=30)
        try:
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={limit_bytes // page_size}")
            db.execute("CREATE TABLE IF NOT EXISTS test_filler(data BLOB)")
            db.commit()
            for size in [1024 * 1024, 65536, 4096, 256, 16]:
                while True:
                    try:
                        db.execute("INSERT INTO test_filler VALUES(?)", (b"x" * size,))
                        db.commit()
                    except sqlite3.OperationalError as exc:
                        self.assertIn("full", str(exc))
                        db.rollback()
                        break
            # Existing pages keep free space. Fill the event log's last page too, so any control
            # record (each writes an event of at least this size) needs a page that cannot exist.
            while True:
                try:
                    db.execute("INSERT INTO events(id,created,type,data) VALUES(?,?,?,?)",
                               (os.urandom(8).hex(), time.time(), "test_filler", "{}"))
                    db.commit()
                except sqlite3.OperationalError as exc:
                    self.assertIn("full", str(exc))
                    db.rollback()
                    break
            self.assertEqual(db.execute("PRAGMA page_count").fetchone()[0], limit_bytes // page_size)
        finally:
            db.close()

    def event_types(self):
        types, after = [], 0
        while page := self.client.call("events", {"after": after}):
            types += [e["type"] for e in page]
            after = page[-1]["seq"]
        return types

    def free_database(self):
        db = sqlite3.connect(self.state / "state.sqlite", timeout=30)
        try:
            db.execute("DROP TABLE IF EXISTS test_filler")
            db.commit()
        finally:
            db.close()

    def test_stop_terminates_sessions_when_database_is_at_hard_limit(self):
        hosts = {role: host_processes(self.start(role)) for role in ["worker", "supervisor"]}
        self.fill_database(LIMIT_BYTES)
        with self.assertRaises(TriadError) as caught:
            self.client.call("stop")
        self.assertIn("database or disk is full", str(caught.exception))
        # Every owned session was stopped despite the failed writes.
        for role, (pid, identity) in hosts.items():
            self.assertNotEqual(process_identity(pid), identity, f"{role} host survived stop")
        status = self.client.call("status")
        self.assertIn("worker-g1", status["reconcile"]["terminated"])
        self.assertIn("supervisor-g1", status["reconcile"]["terminated"])
        # Explicit reconciliation once storage is available again.
        self.free_database()
        self.assertEqual(self.client.call("stop")["state"], "stopped")
        status = self.client.call("status")
        self.assertIsNone(status["reconcile"])
        self.assertEqual({s["state"] for s in status["sessions"]}, {"stopped"})
        self.assertIn("reconciled", self.event_types())

    def test_stop_and_shutdown_use_reserved_capacity(self):
        hosts = {role: host_processes(self.start(role)) for role in ["worker", "supervisor"]}
        self.fill_database(LIMIT_BYTES - RESERVE_BYTES)
        with self.assertRaises(TriadError):
            self.client.call("create_task", {"objective": "x", "checks": [{"name": "x", "argv": ["x"]}]})
        self.assertEqual(self.client.call("stop")["state"], "stopped")
        for role, (pid, identity) in hosts.items():
            self.assertNotEqual(process_identity(pid), identity, f"{role} host survived stop")
        self.assertEqual(self.client.call("shutdown"), {"shutdown": True})
        self.proc.wait(timeout=10)
        self.free_database()
        self.launch_controller()  # For tearDown.


    def test_stale_credentials_stay_fenced_across_restart_until_stop_reconciles(self):
        sessions = {role: self.start(role) for role in ["worker", "supervisor"]}
        tokens = {role: read_json(Path(s["spec"]))["token"] for role, s in sessions.items()}
        self.fill_database(LIMIT_BYTES)
        with self.assertRaises(TriadError):
            self.client.call("stop")
        for restarted in [False, True]:
            if restarted:
                self.proc.kill()
                self.proc.wait(timeout=5)
                self.launch_controller()
            for role, token in tokens.items():
                # A delayed host of a stopped generation fails its startup heartbeat.
                with self.assertRaisesRegex(TriadError, "revoked"):
                    Client(self.state, token).call("heartbeat")
            status = self.client.call("status")
            self.assertEqual(set(status["reconcile"]["fenced"]), {"job", "worker-g1", "supervisor-g1"})
            for action, data in [("resume", {}), ("start", {"role": "worker", "profile": "demo-worker"})]:
                with self.assertRaisesRegex(TriadError, "reconcile"):
                    self.client.call(action, data)
        self.free_database()
        self.assertEqual(self.client.call("stop")["state"], "stopped")
        self.assertIsNone(self.client.call("status")["reconcile"])
        self.assertIn("reconciled", self.event_types())
        with self.assertRaisesRegex(TriadError, "revoked"):
            Client(self.state, tokens["worker"]).call("heartbeat")
        self.client.call("resume")
        self.assertEqual(self.start("worker")["generation"], 2)


class ReconciliationFenceTests(unittest.TestCase):
    """Unit regressions for a stop whose state cannot be recorded (design revision 2 reproduction)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "workspace").mkdir()
        initialize(self.root / "state", self.root / "workspace")
        self.core = Core(self.root / "state")
        self.admin = self.core.config["admin_token"]
        self.tokens = {role: secrets.token_hex(32) for role in ("worker", "supervisor")}
        with self.core.store.db:
            for role, token in self.tokens.items():
                self.core.store.put("sessions", {"role": role, "generation": 1, "token": token, "profile": "demo-" + role,
                                                "backend": "local", "host": "local", "state": "alive",
                                                "turn": "ready", "heartbeat": 1, "created": 1,
                                                "spec": str(self.root / f"{role}-spec.json")})
        self.call("design", {"text": "Implement exactly the requested behavior"})
        self.task = self.call("create_task", {"objective": "x", "checks": [{"name": "x", "argv": ["x"]}]})
        self.stopped = []

    def tearDown(self):
        self.core.store.close()
        self.temp.cleanup()

    def call(self, action, data=None, token=None, core=None):
        return (core or self.core).rpc(token or self.admin, {"id": uid(), "action": action, "data": data or {}})

    def transport(self, unreachable=()):
        """Transport stop succeeds (or fails for unreachable roles); starts spawn nothing."""
        def session(connection, operation, record):
            if operation == "stop":
                if record["role"] in unreachable:
                    raise TriadError(f"{record['role']} host unreachable")
                self.stopped.append((record["role"], record["generation"]))
            return {"started": True} if operation == "start" else None
        return patch.object(LocalConnection, "session", session)

    def failed_stop(self):
        """Stop with every database write failing, as when SQLite is hard-full."""
        def persist(core, errors, label, action):
            errors.append(f"{label} not recorded: database or disk is full")
        with self.transport(), patch.object(Core, "persist", persist):
            with self.assertRaisesRegex(TriadError, "database or disk is full"):
                self.call("stop")
        self.assertEqual(sorted(self.stopped), [("supervisor", 1), ("worker", 1)])
        self.assertTrue((self.root / "state" / "reconcile.json").exists())

    def before_and_after_restart(self):
        yield self.core
        yield self.reopened()

    def reopened(self):
        self.core.store.close()
        self.core = Core(self.root / "state")
        return self.core

    def test_stale_credentials_refused_before_and_after_restart(self):
        self.failed_stop()
        self.assertEqual(self.core.session("worker")["state"], "alive")  # The stop was not recorded.
        for core in self.before_and_after_restart():
            for token in self.tokens.values():
                with self.assertRaisesRegex(TriadError, "revoked"):
                    core.identity(token)
                with self.assertRaisesRegex(TriadError, "revoked"):
                    self.call("heartbeat", token=token)
            # The Designer keeps read, status and control access.
            self.assertIn("worker-g1", self.call("status")["reconcile"]["fenced"])
            self.call("events")
            self.call("task", {"task": self.task["id"]})
            self.call("storage")

    def test_start_resume_assign_and_correct_refused_until_reconciled(self):
        self.failed_stop()
        for core in self.before_and_after_restart():
            for action, data in [("start", {"role": "worker", "profile": "demo-worker"}),
                                 ("replace", {"role": "worker"}), ("resume", {}),
                                 ("assign", {"task": self.task["id"]}),
                                 ("correct", {"task": self.task["id"], "instruction": "x"}),
                                 ("takeover", {"role": "worker"})]:
                with self.transport(), self.assertRaisesRegex(TriadError, "reconcile"):
                    self.call(action, data)
        self.assertEqual(self.core.store.meta("session_count", 0), 0)
        self.assertEqual(self.core.task(self.task["id"])["state"], "queued")

    def test_retried_stop_reconciles_and_allows_explicit_resume_and_start(self):
        self.failed_stop()
        self.reopened()
        with self.transport():
            self.assertEqual(self.call("stop")["state"], "stopped")
        self.assertFalse((self.root / "state" / "reconcile.json").exists())
        self.assertEqual({s["state"] for s in self.core.store.all("sessions")}, {"stopped"})
        self.assertIn("reconciled", [e["type"] for e in self.core.store.events()])
        for token in self.tokens.values():
            with self.assertRaisesRegex(TriadError, "revoked"):
                self.core.identity(token)
        self.assertEqual(self.reopened().fence, set())
        self.call("resume")
        with self.transport():
            started = self.call("start", {"role": "worker", "profile": "demo-worker"})
        self.assertEqual(started["generation"], 2)
        self.assertEqual(self.core.identity(self.core.session("worker")["token"]), ("worker", 2))

    def test_unreachable_session_stays_fenced_until_its_stop_is_confirmed(self):
        with self.transport(unreachable={"worker"}), self.assertRaisesRegex(TriadError, "worker stop unconfirmed"):
            self.call("stop")
        self.assertEqual(self.core.session("supervisor")["state"], "stopped")
        for core in self.before_and_after_restart():
            with self.assertRaisesRegex(TriadError, "revoked"):
                core.identity(self.tokens["worker"])
            with self.transport(), self.assertRaisesRegex(TriadError, "reconcile"):
                self.call("start", {"role": "worker", "profile": "demo-worker"})
        # Still unreachable: the retry attempts the stop again and keeps the fence.
        with self.transport(unreachable={"worker"}), self.assertRaises(TriadError):
            self.call("stop")
        self.assertIn("worker-g1", self.reopened().fence)
        with self.transport():
            self.assertEqual(self.call("stop")["state"], "stopped")
        self.assertIn(("worker", 1), self.stopped)
        self.assertEqual(self.core.fence, set())
        self.call("resume")
        with self.transport():
            self.assertEqual(self.call("start", {"role": "worker", "profile": "demo-worker"})["generation"], 2)

    def test_deadline_stop_that_cannot_be_recorded_fences_the_worker(self):
        self.call("assign", {"task": self.task["id"]}, token=self.tokens["supervisor"])
        with self.core.store.db:
            task = self.core.task(self.task["id"])
            task.update(assigned_at=0, timeout=1)
            self.core.store.put("tasks", task)

        def persist(core, errors, label, action):
            errors.append(f"{label} not recorded: database or disk is full")
        with self.transport(), patch.object(Core, "persist", persist):
            with self.assertRaisesRegex(TriadError, "Deadline enforcement not fully recorded"):
                self.core.tick()
        self.assertIn(("worker", 1), self.stopped)
        for core in self.before_and_after_restart():
            with self.assertRaisesRegex(TriadError, "revoked"):
                core.identity(self.tokens["worker"])
            self.assertEqual(core.identity(self.tokens["supervisor"]), ("supervisor", 1))
            with self.transport(), self.assertRaisesRegex(TriadError, "reconcile"):
                self.call("replace", {"role": "worker"}, token=self.tokens["supervisor"])


class InstalledDistributionTests(unittest.TestCase):
    def test_wheel_runs_demo_and_ssh_bundle_outside_checkout(self):
        try:
            import setuptools  # noqa: F401  Build backend only; nothing is downloaded.
        except ImportError:
            self.skipTest("setuptools is not installed")
        checkout = ENTRY.parent
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = temp / "source"
            source.mkdir()
            for name in ["pyproject.toml", "README.md", "LICENSE", "triad_entry.py"]:
                shutil.copy2(checkout / name, source / name)
            shutil.copytree(checkout / "triad", source / "triad", ignore=shutil.ignore_patterns("__pycache__"))
            env = {k: v for k, v in os.environ.items() if not k.startswith(("TRIAD_", "PYTHON"))}
            build = subprocess.run([sys.executable, "-c", "import setuptools.build_meta as b, sys; print(b.build_wheel(sys.argv[1]))",
                                    str(temp / "dist")], cwd=source, env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(build.returncode, 0, build.stderr[-2000:])
            wheel = temp / "dist" / build.stdout.strip().splitlines()[-1]
            self.assertIn("triad_entry.py", zipfile.ZipFile(wheel).namelist())
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(temp / "venv")], check=True, timeout=120)
            python = temp / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            purelib = subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                                     capture_output=True, text=True, check=True).stdout.strip()
            zipfile.ZipFile(wheel).extractall(purelib)
            outside = temp / "outside"
            outside.mkdir()

            def run(*args, timeout=180):
                return subprocess.run([str(python), "-I", *args], cwd=outside, env=env,
                                      capture_output=True, text=True, timeout=timeout)

            probe = run("-c", "import io, json, zipfile, triad, triad_entry\n"
                        "from triad.backends import ENTRY\nfrom triad.connections import runtime_bundle\n"
                        "names = zipfile.ZipFile(io.BytesIO(runtime_bundle()[1])).namelist()\n"
                        "print(json.dumps({'package': triad.__file__, 'entry': str(ENTRY), 'exists': ENTRY.is_file(), 'bundle': names}))")
            self.assertEqual(probe.returncode, 0, probe.stderr[-2000:])
            info = json.loads(probe.stdout)
            self.assertTrue(Path(info["package"]).resolve().is_relative_to(Path(purelib).resolve()))
            self.assertTrue(info["exists"])
            self.assertTrue(Path(info["entry"]).resolve().is_relative_to(Path(purelib).resolve()))
            self.assertIn("triad_entry.py", info["bundle"])
            self.assertIn("triad/runtime.py", info["bundle"])
            demo = run("-m", "triad", "demo", "--directory", str(temp / "demo"), "--backend", "local")
            self.assertEqual(demo.returncode, 0, demo.stdout[-2000:] + demo.stderr[-2000:])
            self.assertTrue(json.loads(demo.stdout)["accepted"].startswith("task-"))
            sessions = sorted((temp / "demo" / "state" / "sessions").glob("*/host.json"))
            self.assertEqual(len(sessions), 2)
            for path in sessions:
                self.assertFalse(alive(path.parent), f"{path.parent.name} survived the installed demo")


if __name__ == "__main__":
    unittest.main()
