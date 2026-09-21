"""Host transport, independent of session multiplexers and AI harnesses."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import zipfile

from .backends import ENTRY, alive, backend, process_identity
from .util import TriadError, atomic_json, fingerprint, read_json


class ConnectionUnavailable(TriadError):
    """Transport failed. Does not imply the remote process exited."""


def validate_host(name, config):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise TriadError("Host name must contain letters, numbers, underscores or hyphens")
    if config.get("kind") == "local":
        return
    if config.get("kind") != "ssh":
        raise TriadError("Host connection kind must be local or ssh")
    target = config.get("target", "")
    if not isinstance(target, str) or not target or target.startswith("-") or any(c.isspace() for c in target):
        raise TriadError("SSH target must be a config alias or user@host, not a command")
    root = config.get("root", "")
    if not isinstance(root, str) or not root.startswith("/") or root == "/" or ".." in PurePosixPath(root).parts:
        raise TriadError("SSH root must be an absolute, dedicated Linux directory (no ~ or ..)")
    if "reverse_port" in config and not 1024 <= int(config["reverse_port"]) <= 65535:
        raise TriadError("Reverse port must be between 1024 and 65535")
    if config.get("port") and not 1 <= int(config["port"]) <= 65535:
        raise TriadError("Invalid SSH port")


def runtime_bundle():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        files = [ENTRY, *sorted((ENTRY.parent / "triad").glob("*.py")),
                 *sorted((ENTRY.parent / "examples").glob("*.py"))]
        for path in files:
            item = zipfile.ZipInfo(path.relative_to(ENTRY.parent).as_posix(), (2020, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, path.read_bytes())
    content = buffer.getvalue()
    return hashlib.sha256(content).hexdigest()[:20], content


class LocalConnection:
    kind = "local"
    python = sys.executable
    entry = str(ENTRY)

    def __init__(self, name, config, manager):
        self.name, self.config, self.manager = name, config, manager

    def job_root(self):
        return str(self.manager.root)

    def call(self, operation, data):
        from .remote import operation as execute
        return execute(operation, data)

    def snapshot(self, workspace, excludes):
        return fingerprint(workspace, excludes)

    def exists(self, path):
        return Path(path).is_dir()

    def prepare(self, spec, bootstrap, handoff):
        return spec

    def session(self, operation, session):
        transport = backend(session["backend"])
        if operation == "alive":
            return alive(Path(session["spec"]).parent)
        return getattr(transport, operation)(session["spec"])


class SSHConnection(LocalConnection):
    kind = "ssh"

    def __init__(self, name, config, manager):
        super().__init__(name, config, manager)
        self.python = config.get("python", "python3")
        self.digest, self.bundle = runtime_bundle()
        self.runtime = str(PurePosixPath(config["root"]) / "runtime" / self.digest)
        self.entry = self.runtime + "/triad_entry.py"
        self.deployed = False

    def ssh_argv(self, *, tty=False, extra=()):
        # Identity, proxy jump and SSH config are routing configuration, not shell text.
        argv = [self.config.get("ssh", "ssh"), "-tt" if tty else "-T",
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
        for key, flag in [("identity", "-i"), ("port", "-p"), ("config_file", "-F"), ("jump", "-J")]:
            if self.config.get(key):
                argv.extend([flag, str(self.config[key])])
        return [*argv, *extra, self.config["target"]]

    def run(self, argv, payload=None, timeout=30):
        command = [*self.ssh_argv(), shlex.join([str(a) for a in argv])]
        try:
            result = subprocess.run(command, input=payload, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConnectionUnavailable(f"SSH host {self.name} is unreachable: {exc}") from exc
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise ConnectionUnavailable(f"SSH operation on {self.name} failed: {detail or result.returncode}")
        return result.stdout

    def deploy(self):
        if self.deployed:
            return
        # Code is immutable and addressed by its content hash; no package manager is run.
        script = (
            "import io,json,os,pathlib,sys,zipfile; os.umask(0o077); "
            "p=pathlib.Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=True); "
            "z=zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read())); "
            "assert all(not pathlib.PurePosixPath(n).is_absolute() and '..' not in pathlib.PurePosixPath(n).parts for n in z.namelist()); "
            "z.extractall(p); print('ready')"
        )
        self.run([self.python, "-c", script, self.runtime], self.bundle)
        self.deployed = True

    def call(self, operation, data):
        self.deploy()
        raw = self.run([self.python, "-B", self.entry, "remote-rpc"],
                       json.dumps({"operation": operation, "data": data}).encode())
        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise ConnectionUnavailable(f"Invalid helper response from {self.name}") from exc
        if not result.get("ok"):
            raise TriadError(f"Host {self.name}: {result.get('error', 'operation failed')}")
        return result["result"]

    def job_root(self):
        return str(PurePosixPath(self.config["root"]) / "jobs" / self.manager.config["job"])

    def snapshot(self, workspace, excludes):
        return self.call("snapshot", {"workspace": workspace, "excludes": excludes})

    def exists(self, path):
        return self.call("exists", {"path": path})

    def prepare(self, spec, bootstrap, handoff):
        self.manager.ensure_tunnel(self)
        remote_dir = str(PurePosixPath(self.job_root()) / "sessions" / f"{spec['role']}-g{spec['generation']}")
        remote = dict(spec, state_dir=self.job_root(), spec=remote_dir + "/spec.json")
        # The spec is rendered using this host's paths before this method is called.
        self.call("prepare", {"root": self.job_root(), "session_dir": remote_dir, "spec": remote,
                              "bootstrap": bootstrap, "handoff": handoff,
                              "endpoint": self.manager.tunnel_endpoint(self), "budget": self.manager.config["storage_bytes"]})
        return dict(spec, remote_spec=remote["spec"], remote_state=self.job_root(), runtime=self.runtime)

    def session(self, operation, session):
        spec = read_json(Path(session["spec"]))
        remote_spec = spec["remote_spec"]
        if operation == "attach":
            argv = self.call("session_attach", {"backend": session["backend"], "spec": remote_spec})
            return [*self.ssh_argv(tty=True), shlex.join(argv)]
        return self.call("session_" + operation, {"backend": session["backend"], "spec": remote_spec})


class Connections:
    def __init__(self, root, config):
        self.root, self.config = Path(root), config
        self.hosts = {"local": {"kind": "local"}, **config.get("hosts", {})}
        self.instances = {}
        self.tunnels = {}
        self.next_retry = {}
        self.last_error = {}
        self.tunnel_lock = threading.RLock()
        self.tunnel_thread = None
        self.closing = False

    def get(self, name="local"):
        if name not in self.hosts:
            raise TriadError(f"Unknown host: {name}")
        if name not in self.instances:
            config = self.hosts[name]
            validate_host(name, config)
            cls = SSHConnection if config["kind"] == "ssh" else LocalConnection
            self.instances[name] = cls(name, config, self)
        return self.instances[name]

    def for_session(self, session):
        name = session.get("host", "local")
        connection = self.get(name)
        if session.get("host_config") and session["host_config"] != connection.config:
            raise TriadError("Session host routing changed; restore its pinned host configuration before control")
        return connection

    def tunnel_path(self, connection):
        return self.root / "connections" / (connection.name + ".json")

    def tunnel_endpoint(self, connection):
        port = read_json(self.tunnel_path(connection))["remote_port"]
        return f"http://127.0.0.1:{port}"

    def ensure_tunnel(self, connection):
        with self.tunnel_lock:
            if self.closing:
                raise ConnectionUnavailable("Controller connection manager is closing")
            return self._ensure_tunnel(connection)

    def _ensure_tunnel(self, connection):
        path = self.tunnel_path(connection)
        current = read_json(path) if path.exists() else {}
        local_url = read_json(self.root / "endpoint.json")["url"]
        local_port = int(local_url.rsplit(":", 1)[1])
        routing = hashlib.sha256(json.dumps(connection.config, sort_keys=True).encode()).hexdigest()
        if current.get("pid") and process_identity(current["pid"]) == current.get("identity"):
            if current.get("local_port") == local_port and current.get("routing") == routing:
                return
            self.stop_tunnel(connection.name)
        remote_port = current.get("remote_port") or connection.config.get("reverse_port")
        if not remote_port:
            remote_port = connection.call("free_port", {})
        marker = "TRIAD_TUNNEL_READY"
        script = f"import time; print('{marker}',flush=True); time.sleep(31536000)"
        argv = [*connection.ssh_argv(extra=("-o", "ExitOnForwardFailure=yes", "-R",
                    f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}")),
                shlex.join([connection.python, "-u", "-c", script])]
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.tunnels[connection.name] = process
        ready = queue.Queue(maxsize=1)
        threading.Thread(target=lambda: ready.put(process.stdout.readline(256)), daemon=True).start()
        try:
            line = ready.get(timeout=15)
            if line.strip() != marker.encode():
                raise ConnectionUnavailable(f"SSH tunnel for {connection.name} failed to establish forwarding")
        except queue.Empty as exc:
            process.kill()
            process.wait(timeout=5)
            raise ConnectionUnavailable(f"SSH tunnel startup timed out for {connection.name}") from exc
        except Exception:
            process.kill()
            process.wait(timeout=5)
            raise
        atomic_json(path, {"pid": process.pid, "identity": process_identity(process.pid),
                           "local_port": local_port, "remote_port": remote_port, "routing": routing})
        # Drain diagnostic output without retaining an unbounded buffer.
        def drain(stream):
            while stream.read(4096):
                pass
        threading.Thread(target=drain, args=(process.stderr,), daemon=True).start()
        self.last_error.pop(connection.name, None)

    def stop_tunnel(self, name):
        path = self.root / "connections" / (name + ".json")
        if path.exists():
            record = read_json(path)
            if record.get("pid") and process_identity(record["pid"]) == record.get("identity"):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(record["pid"]), "/T", "/F"], capture_output=True, timeout=10)
                else:
                    import signal
                    os.kill(record["pid"], signal.SIGTERM)
        process = self.tunnels.pop(name, None)
        if process:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            for stream in (process.stdout, process.stderr):
                stream.close()

    def tick(self, sessions):
        if self.closing or (self.tunnel_thread and self.tunnel_thread.is_alive()):
            return
        self.tunnel_thread = threading.Thread(target=self._maintain, args=(sessions,), daemon=True)
        self.tunnel_thread.start()

    def _maintain(self, sessions):
        for name in {s.get("host", "local") for s in sessions if s["state"] not in {"stopped", "exited"}}:
            connection = self.get(name)
            if any(s.get("host", "local") == name and s.get("host_config") and s["host_config"] != connection.config for s in sessions):
                self.last_error[name] = "Session host configuration changed; restore pinned routing before reconnecting"
                continue
            if connection.kind != "ssh" or time.monotonic() < self.next_retry.get(name, 0):
                continue
            try:
                self.ensure_tunnel(connection)
            except (TriadError, OSError) as exc:
                self.last_error[name] = str(exc)
            self.next_retry[name] = time.monotonic() + 10

    def close(self):
        self.closing = True
        with self.tunnel_lock:
            for name in set(self.instances) | set(self.tunnels):
                if self.get(name).kind == "ssh":
                    self.stop_tunnel(name)
