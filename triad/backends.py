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
BACKENDS = ("local", "psmux", "tmux", "tsmux")


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
    path = root / "host.json"
    if not path.exists():
        # A host records its identity before its startup handshake and spawns nothing earlier.
        # A host that starts after its session was revoked exits at that handshake.
        return
    data = read_json(path)
    if not data.get("identity"):
        raise TriadError("Host identity is unknown; reconcile its processes before replacing")
    current = process_identity(data["pid"])
    if current is not None and current != data["identity"]:
        # The PID names another process now. The kernel never reuses a PID that is still a
        # process-group ID, so the host and its whole group have already exited.
        return
    if current is None:
        # Exited host. Its Windows Job Object killed any remaining descendants, and a host
        # that never confirmed containment never spawned a harness.
        if os.name == "nt" or not data.get("contained"):
            return
        # A dead POSIX group leader may still have surviving descendants in its group.
        return kill_posix_group(data["pid"], timeout)
    if not data.get("contained"):
        raise TriadError("Host did not confirm process containment; refusing automatic replacement")
    if os.name == "nt":
        # A stopped host's Windows job owns all ordinary descendants.
        try:
            subprocess.run(["taskkill", "/PID", str(data["pid"]), "/T", "/F"],
                           capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TriadError(f"Could not stop host: {exc}") from exc
        deadline = time.monotonic() + timeout
        while alive(root) and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive(root):
            raise TriadError("Could not confirm host termination; workspace remains blocked")
        return
    # Stop the host first. As child subreaper it keeps orphaned descendants in its tree,
    # including ones that left its process group with setsid().
    kill_posix_group(data["pid"], timeout, kill_posix_tree(data["pid"]))


def kill_posix_group(pgid, timeout, owned=None):
    """SIGKILL a process group, then confirm it and any separately signalled processes are gone."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    def survivors():
        return posix_group_alive(pgid) or any(
            identity is not None and process_identity(pid) == identity for pid, identity in (owned or {}).items())

    deadline = time.monotonic() + timeout
    while survivors() and time.monotonic() < deadline:
        time.sleep(0.1)
    if survivors():
        raise TriadError("Owned process group did not terminate; workspace remains blocked")


def kill_posix_tree(pid, include_root=True):
    """SIGKILL a process subtree and return {pid: identity} of what was signalled.

    Members are stopped before each new listing, so none can fork or reparent unseen."""
    owned = {}
    if include_root:
        try:
            os.kill(pid, signal.SIGSTOP)
        except ProcessLookupError:
            return owned
        owned[pid] = process_identity(pid)
    for _ in range(50):
        children = {}
        for row in posix_processes("pid=,ppid="):
            children.setdefault(int(row[1]), []).append(int(row[0]))
        tree, found = [pid], False
        for parent in tree:
            for child in children.get(parent, ()):
                if child in tree:
                    continue
                tree.append(child)
                if child not in owned:
                    try:
                        os.kill(child, signal.SIGSTOP)
                    except ProcessLookupError:
                        continue
                    owned[child] = process_identity(child)
                    found = True
        if not found:
            break
    for process in reversed(list(owned)):
        try:
            os.kill(process, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return owned


def posix_processes(fields):
    try:
        result = subprocess.run(["ps", "-e", "-o", fields], capture_output=True, text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TriadError(f"Cannot list processes to confirm containment: {exc}") from exc
    return [line.split() for line in result.stdout.splitlines() if line.strip()]


def posix_group_alive(pgid):
    return any(len(row) == 2 and int(row[0]) == pgid and not row[1].startswith("Z")
               for row in posix_processes("pgid=,stat="))


def stop_session(name, spec):
    """Stop through the session's backend, or by host identity when that backend is unavailable."""
    try:
        transport = backend(name)
    except TriadError:
        return stop_host(Path(spec).parent)
    return transport.stop(spec)


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
        try:
            p = subprocess.run([self.executable, *args], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TriadError(f"Multiplexer command failed: {exc}") from exc
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
            # The BOM makes Windows PowerShell 5.1 read non-ASCII paths as UTF-8, not the ANSI code page.
            script.write_text("& " + " ".join("'" + a.replace("'", "''") + "'" for a in argv) + "\n",
                              encoding="utf-8-sig")
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
    if name in BACKENDS:
        return MuxBackend(name)
    raise TriadError(f"Unknown backend: {name}")
