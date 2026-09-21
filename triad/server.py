from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
import time

from .core import Core
from .util import FileLock, TriadError, atomic_json


def tick_safely(core, last_error=None):
    """Run policy maintenance. A failure is recorded once and never stops the control API."""
    try:
        core.tick()
        return None
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:2000]
        if message != last_error:
            try:
                with core.store.db:
                    core.store.event("controller_error", {"error": message})
            except sqlite3.Error:
                pass
        return message


def serve(root, port=0):
    root = Path(root).resolve()
    with FileLock(root / "controller.lock"):
        core = Core(root)
        # Separate state directories may not control the same writable workspace.
        workspace_connection = core.connections.get(core.config.get("workspace_host", "local"))
        workspace_lock_path = (root / "workspace.lock" if workspace_connection.kind == "ssh" else
            Path(core.config["workspace"]).parent / (
                ".triad-" + __import__("hashlib").sha256(core.config["workspace"].casefold().encode()).hexdigest()[:16] + ".lock"))
        with FileLock(workspace_lock_path):
            ownership = workspace_lock_path.with_suffix(".json")
            from .backends import alive
            from .util import read_json
            if ownership.exists():
                previous = read_json(ownership)
                previous_root = Path(previous["state"])
                if previous_root != root:
                    specs = list((previous_root / "sessions").glob("*/spec.json"))
                    if not previous_root.exists() or any(alive(s.parent) is not False for s in specs):
                        raise TriadError(
                            f"Workspace has an unreconciled owner in another state directory ({previous_root}). "
                            f"Stop its sessions there; if that state is gone and nothing runs, delete {ownership}")
            atomic_json(ownership, {"state": str(root)})
            with core.store.db:
                core.store.set_meta("shutdown", False)
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    try:
                        if self.path != "/rpc":
                            raise TriadError("Unknown endpoint")
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 65536:
                            raise TriadError("Request size must be 1–65536 bytes")
                        if self.headers.get("Origin"):
                            raise TriadError("Browser-origin requests are not supported")
                        self.connection.settimeout(5)
                        request = json.loads(self.rfile.read(length))
                        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                        result = {"ok": True, "result": core.rpc(token, request)}
                        status = 200
                    except (TriadError, ValueError, KeyError, TypeError, OSError, sqlite3.Error,
                            subprocess.SubprocessError) as exc:
                        result, status = {"ok": False, "error": str(exc)}, 400
                    body = json.dumps(result).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # Request result was already durably stored.

            saved_endpoint = root / "endpoint.json"
            # Reuse the controller port so surviving reverse tunnels still point here.
            if not port and saved_endpoint.exists():
                port = int(read_json(saved_endpoint)["url"].rsplit(":", 1)[1])
            server = HTTPServer(("127.0.0.1", port), Handler)
            server.timeout = 0.5
            atomic_json(root / "endpoint.json", {"url": f"http://127.0.0.1:{server.server_port}"})
            print(f"Triad controller: http://127.0.0.1:{server.server_port}  state={root}", flush=True)
            last_tick = 0
            tick_error = None
            try:
                while True:
                    server.handle_request()
                    if core.store.meta("shutdown", False):
                        break
                    if time.monotonic() - last_tick > 2:
                        tick_error = tick_safely(core, tick_error)
                        last_tick = time.monotonic()
            except KeyboardInterrupt:
                pass  # Deliberately detach controller; explicit 'stop' owns session shutdown.
            finally:
                server.server_close()
                core.connections.close()
                core.store.close()
