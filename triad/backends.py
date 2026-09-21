"""Process/session transport only. No knowledge of AI vendors or task semantics."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time

from .util import TriadError, read_json

ENTRY = Path(__file__).resolve().parents[1] / "triad_entry.py"


def process_identity(pid):
    """PID plus creation identity, avoiding accidental signals to a recycled PID."""
    if os.name == "nt":
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        values = [wintypes.FILETIME() for _ in range(4)]
        try:
            exit_code = wintypes.DWORD()
            if not k.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return None
            if not k.GetProcessTimes(handle, *(ctypes.byref(x) for x in values)):
                return None
            return str(values[0].dwHighDateTime << 32 | values[0].dwLowDateTime)
        finally:
            k.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            parts = stat.read_text().rsplit(")", 1)[1].split()
            return None if parts[0] == "Z" else parts[19]
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
        return result.stdout.strip() or None
    except (OSError, IndexError):
        return None


def alive(root):
    path = Path(root) / "host.json"
    if not path.exists():
        return None
    data = read_json(path)
    return bool(data.get("identity") and process_identity(data["pid"]) == data["identity"])


def stop_host(root, timeout=10):
    root = Path(root)
    if os.name != "nt":
        path = root / "host.json"
        if not path.exists():
            raise TriadError("No host identity; reconcile startup before replacing")
        data = read_json(path)
        current = process_identity(data["pid"])
        if current is not None and current != data["identity"]:
            raise TriadError("Host PID was recycled; refusing to signal an uncertain process group")
        if not data.get("contained"):
            raise TriadError("Host did not confirm process containment")
        # Also handles a dead group leader with surviving descendants.
        try:
            os.killpg(data["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout
        while posix_group_alive(data["pid"]) and time.monotonic() < deadline:
            time.sleep(0.1)
        if posix_group_alive(data["pid"]):
            raise TriadError("Owned process group did not terminate; workspace remains blocked")
        return
    if alive(root):
        data = read_json(root / "host.json")
        # A stopped host's Windows job or POSIX group owns all ordinary descendants.
        if not data.get("contained"):
            raise TriadError("Host did not confirm process containment; refusing automatic replacement")
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(data["pid"]), "/T", "/F"],
                           capture_output=True, timeout=timeout)
        else:
            try:
                os.killpg(data["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout
        while alive(root) and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive(root) and os.name != "nt":
            os.killpg(data["pid"], signal.SIGKILL)
            time.sleep(0.2)
        # The group leader may have died before a descendant ignoring SIGTERM.
        # Confirm/kill the owned group as well before permitting a replacement.
        if os.name != "nt":
            try:
                os.killpg(data["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        if alive(root):
            raise TriadError("Could not confirm host termination; workspace remains blocked")
    elif alive(root) is None:
        raise TriadError("No host identity; reconcile startup before stopping or replacing")


def posix_group_alive(pgid):
    result = subprocess.run(["ps", "-e", "-o", "pgid=,stat="], capture_output=True, text=True, check=True)
    return any(int(parts[0]) == pgid and not parts[1].startswith("Z")
               for line in result.stdout.splitlines() if len(parts := line.split()) == 2)


class LocalBackend:
    name = "local"

    def start(self, spec):
        proc = subprocess.Popen([sys.executable, "-B", str(ENTRY), "host", "--spec", str(spec)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=os.name != "nt",
                                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP |
                                               subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0)
        return {"pid": proc.pid, "backend": self.name}

    def stop(self, spec):
        stop_host(Path(spec).parent)

    def capture(self, spec):
        path = Path(spec).parent / "output.log"
        if not path.exists():
            return "No output yet."
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - 16384))
            return f.read().decode("utf-8", errors="replace")

    def attach(self, spec):
        raise TriadError("Local backend has log observation only; use a multiplexer for interactive attachment")


class MuxBackend(LocalBackend):
    def __init__(self, executable):
        self.executable = shutil.which(executable)
        self.name = executable
        if not self.executable:
            raise TriadError(f"Session backend is not installed: {executable}")

    def command(self, *args, check=True):
        p = subprocess.run([self.executable, *args], capture_output=True, text=True, timeout=20)
        if check and p.returncode:
            raise TriadError(p.stderr.strip() or p.stdout.strip() or "Multiplexer command failed")
        return p

    def start(self, spec):
        data = read_json(Path(spec))
        name = data["session_name"]
        if self.command("has-session", "-t", name, check=False).returncode == 0:
            return {"backend": self.name, "name": name, "adopted": True}
        argv = [sys.executable, "-B", str(ENTRY), "host", "--spec", str(spec)]
        if os.name == "nt":
            script = Path(spec).with_suffix(".ps1")
            script.write_text("& " + " ".join("'" + a.replace("'", "''") + "'" for a in argv) + "\n",
                              encoding="utf-8")
            command = subprocess.list2cmdline(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                              "-File", str(script)])
        else:
            command = "exec " + shlex.join(argv)
        self.command("new-session", "-d", "-s", name, "-c", data["workspace"], command)
        self.command("set-option", "-t", name, "history-limit", "2000", check=False)
        return {"backend": self.name, "name": name}

    def stop(self, spec):
        super().stop(spec)
        data = read_json(Path(spec))
        self.command("kill-session", "-t", data["session_name"], check=False)

    def capture(self, spec):
        data = read_json(Path(spec))
        p = self.command("capture-pane", "-p", "-t", data["session_name"], check=False)
        return p.stdout[-16384:] if p.returncode == 0 and p.stdout.strip() else super().capture(spec)

    def attach(self, spec):
        data = read_json(Path(spec))
        return [self.executable, "attach-session", "-t", data["session_name"]]


def backend(name):
    if name == "local":
        return LocalBackend()
    if name in {"psmux", "tmux", "tsmux"}:
        return MuxBackend(name)
    raise TriadError(f"Unknown backend: {name}")
