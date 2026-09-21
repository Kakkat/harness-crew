"""Small remote execution helper. Invoked over authenticated SSH, not a public server."""
import json
import os
from pathlib import Path
import socket
import sys

from .backends import alive, backend, stop_session
from .util import TriadError, atomic_json, fingerprint


def operation(name, data):
    if name == "probe":
        import platform
        import shutil
        return {"platform": platform.system(), "python": sys.version.split()[0],
                "python_executable": sys.executable, "backend": data["backend"],
                "backend_executable": shutil.which(data["backend"]) if data["backend"] != "local" else sys.executable}
    if name == "exists":
        return Path(data["path"]).is_dir()
    if name == "snapshot":
        root = Path(data["workspace"])
        if not root.is_dir():
            raise TriadError("Workspace does not exist on this host")
        return fingerprint(root, data.get("excludes", []))
    if name == "free_port":
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    if name == "storage":
        root = Path(data["root"])
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink()) if root.exists() else 0
    if name == "prepare":
        os.umask(0o077)
        root = Path(data["root"]).resolve()
        directory = Path(data["session_dir"]).resolve()
        if root not in directory.parents:
            raise TriadError("Session directory must be inside remote job state")
        workspace = Path(data["spec"]["workspace"]).resolve()
        if not workspace.is_dir() or workspace == root or workspace in root.parents:
            raise TriadError("Remote workspace must exist and state must be outside it")
        used = operation("storage", {"root": str(root)})
        if used + data["spec"]["log_bytes"] * 3 > data["budget"]:
            raise TriadError("Remote state storage budget reached")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(directory / "spec.json", data["spec"])
        atomic_json(directory / "handoff.json", data["handoff"])
        (directory / "bootstrap.md").write_text(data["bootstrap"], encoding="utf-8")
        atomic_json(root / "endpoint.json", {"url": data["endpoint"]})
        return {"prepared": str(directory)}
    if name.startswith("session_"):
        action = name.removeprefix("session_")
        if action == "alive":
            return alive(Path(data["spec"]).parent)
        if action == "stop":
            return stop_session(data["backend"], data["spec"])
        if action not in {"start", "capture", "attach", "ring"}:
            raise TriadError("Unsupported session operation")
        return getattr(backend(data["backend"]), action)(data["spec"])
    if name == "evidence":
        directory = Path(data["directory"])
        file = data.get("file", "stdout.log")
        if file not in {"stdout.log", "stderr.log", "completion.json"}:
            raise TriadError("Unsupported evidence file")
        path = directory / file
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - min(data.get("bytes", 16384), 65536)))
            return stream.read(65536).decode("utf-8", errors="replace")
    raise TriadError("Unsupported host operation")


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(1024 * 1024))
        result = {"ok": True, "result": operation(request["operation"], request["data"])}
    except (TriadError, OSError, ValueError, KeyError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result), flush=True)
