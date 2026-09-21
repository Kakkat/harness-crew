import io
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
import zipfile

from triad.connections import Connections, SSHConnection, ConnectionUnavailable, runtime_bundle, validate_host
from triad.core import Core, initialize
from triad.util import TriadError, read_json, atomic_json, uid


HOST = {"kind": "ssh", "target": "build-box", "root": "/home/user/.triad", "python": "python3"}


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = Connections(self.root, {"job": "job-test", "hosts": {"linux": HOST}})
        self.connection = self.manager.get("linux")

    def tearDown(self):
        self.temp.cleanup()

    def test_ssh_target_is_not_a_shell_fragment(self):
        for target in ["-oProxyCommand=evil", "host with spaces", ""]:
            with self.assertRaises(TriadError):
                validate_host("linux", dict(HOST, target=target))

    def test_remote_root_requires_explicit_absolute_path(self):
        for root in ["~/.triad", "/", "/home/a/../b", "relative"]:
            with self.assertRaises(TriadError):
                validate_host("linux", dict(HOST, root=root))

    def test_remote_argument_quoting_is_lossless(self):
        argv = ["python3", "/path with spaces/entry.py", "literal;$(touch nope)", "a'b", "line\nnext"]
        with patch("triad.connections.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"ok", b"")) as run:
            self.assertEqual(self.connection.run(argv, b"payload"), b"ok")
            submitted = run.call_args.args[0]
            self.assertEqual(shlex.split(submitted[-1]), argv)
            self.assertEqual(run.call_args.kwargs["input"], b"payload")
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_ssh_uses_config_identity_jump_and_strict_host_keys(self):
        config = dict(HOST, identity="C:/keys/key with spaces", jump="bastion", port=2222, config_file="C:/ssh/config")
        connection = SSHConnection("linux", config, self.manager)
        args = connection.ssh_argv()
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("BatchMode=yes", args)
        self.assertEqual(args[args.index("-i") + 1], config["identity"])
        self.assertEqual(args[args.index("-J") + 1], "bastion")
        self.assertEqual(args[-1], "build-box")

    def test_network_error_is_not_process_exit(self):
        with patch("triad.connections.subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 8)):
            with self.assertRaises(ConnectionUnavailable):
                self.connection.run(["python3", "entry.py"])

    def test_ssh_failure_preserves_diagnostic(self):
        result = subprocess.CompletedProcess([], 255, b"", b"Connection refused")
        with patch("triad.connections.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ConnectionUnavailable, "Connection refused"):
                self.connection.run(["true"])

    def test_runtime_archive_contains_code_only_and_has_stable_digest(self):
        digest, payload = runtime_bundle()
        self.assertEqual(digest, runtime_bundle()[0])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            self.assertIn("triad_entry.py", names)
            self.assertIn("triad/remote.py", names)
            self.assertTrue(all(n.endswith(".py") for n in names))
            self.assertFalse(any("config.json" in n or "spec.json" in n for n in names))
        self.assertLess(len(payload), 128 * 1024)

    def test_remote_path_is_not_reinterpreted_as_windows_path(self):
        with patch.object(SSHConnection, "exists", return_value=True):
            initialize(self.root / "state", "/srv/repo", "tmux", {"linux": HOST}, "linux")
        config = read_json(self.root / "state" / "config.json")
        self.assertEqual(config["workspace"], "/srv/repo")
        self.assertEqual(config["workspace_host"], "linux")

    def test_attachment_executes_ssh_with_remote_argv(self):
        spec = self.root / "spec.json"
        atomic_json(spec, {"remote_spec": "/remote/spec.json"})
        with patch.object(self.connection, "call", return_value=["tmux", "attach-session", "-t", "session x"]):
            argv = self.connection.session("attach", {"spec": str(spec), "backend": "tmux"})
        self.assertIn("-tt", argv)
        self.assertEqual(shlex.split(argv[-1]), ["tmux", "attach-session", "-t", "session x"])


class RemoteCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with patch.object(SSHConnection, "exists", return_value=True):
            initialize(self.root / "state", "/srv/repo", "tmux", {"linux": HOST, "other": dict(HOST, target="other-box")}, "linux")
        self.core = Core(self.root / "state")
        self.token = self.core.config["admin_token"]

    def tearDown(self):
        self.core.store.close()
        self.temp.cleanup()

    def call(self, action, data):
        return self.core.rpc(self.token, {"id": uid(), "action": action, "data": data})

    def test_snapshot_executes_on_workspace_host(self):
        connection = self.core.connections.get("linux")
        with patch.object(connection, "snapshot", return_value="remote-digest") as snapshot:
            self.assertEqual(self.core.snapshot(), "remote-digest")
            snapshot.assert_called_once_with("/srv/repo", [])

    def test_unreachable_worker_cannot_be_replaced(self):
        session = {"role": "worker", "generation": 1, "state": "alive", "turn": "ready", "token": "old",
                   "host": "linux", "host_config": HOST, "profile": "demo-worker", "workspace": "/srv/repo",
                   "backend": "tmux", "spec": str(self.root / "spec.json")}
        with self.core.store.db:
            self.core.store.put("sessions", session)
        connection = self.core.connections.get("linux")
        with patch.object(connection, "session", side_effect=ConnectionUnavailable("network partition")):
            with self.assertRaises(ConnectionUnavailable):
                self.call("replace", {"role": "worker"})
        self.assertEqual(self.core.session("worker")["generation"], 1)
        self.assertEqual(self.core.session("worker")["token"], "old")

    def test_supervisor_and_worker_can_select_different_hosts(self):
        self.call("design", {"text": "Do the task on the canonical remote repository"})
        with patch.object(SSHConnection, "exists", return_value=True), \
             patch.object(SSHConnection, "snapshot", return_value="digest"), \
             patch.object(SSHConnection, "prepare", side_effect=lambda s, b, h: s), \
             patch.object(SSHConnection, "session", return_value={"started": True}):
            worker = self.call("start", {"role": "worker", "profile": "demo-worker", "host": "linux"})
            supervisor = self.call("start", {"role": "supervisor", "profile": "demo-supervisor", "host": "other", "workspace": "/srv/review"})
        self.assertEqual(worker["host"], "linux")
        self.assertEqual(supervisor["host"], "other")
        spec = read_json(Path(supervisor["spec"]))
        self.assertEqual(spec["workspace"], "/srv/review")
        self.assertEqual(spec["argv"][0], "python3")
        self.assertTrue(spec["argv"][1].startswith("/home/user/.triad/runtime/"))
        bootstrap = Path(supervisor["spec"]).with_name("bootstrap.md").read_text()
        self.assertNotIn(str(self.root), bootstrap)

    def test_worker_cannot_silently_change_repository_host(self):
        with patch.object(SSHConnection, "exists", return_value=True):
            with self.assertRaisesRegex(TriadError, "canonical"):
                self.call("start", {"role": "worker", "profile": "demo-worker", "host": "other", "workspace": "/srv/repo"})


if __name__ == "__main__":
    unittest.main()
