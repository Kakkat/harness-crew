from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

from .backends import kill_posix_group, kill_posix_tree, process_identity
from .adapters import adapter
from .client import Client
from .util import CappedLog, FileLock, TriadError, atomic_json, now, read_json, uid


def contain_host():
    """Ordinary descendants stay in this host's process group / Windows Job Object."""
    if os.name != "nt":
        if os.getpgrp() != os.getpid():
            os.setsid()
        if sys.platform.startswith("linux"):
            # Orphaned descendants, including ones that call setsid(), are reparented to this
            # host instead of init, so stopping the host can still find them in its tree.
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(ctypes.c_int(36), ctypes.c_ulong(1), ctypes.c_ulong(0),  # PR_SET_CHILD_SUBREAPER
                          ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
                raise TriadError(f"Cannot adopt orphaned descendants: {os.strerror(ctypes.get_errno())}")
        return None
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway allowed.
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise TriadError(f"Cannot establish Windows process containment: {ctypes.get_last_error()}")
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise TriadError(f"Cannot assign host to Windows job: {ctypes.get_last_error()}")
    # Keep this handle alive until the host exits. Closing it earlier also kills this host.
    return handle


def reap_adopted(harness_pid):
    """Collect exited orphans adopted as subreaper; leave the harness's own status to Popen."""
    while True:
        try:
            info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return
        if info is None or info.si_pid == harness_pid:
            return
        os.waitpid(info.si_pid, 0)


def authorize_startup(client, timeout):
    """Startup handshake. Spawn nothing without an affirmative reply from the controller.

    A session revoked before this host recorded its identity was stopped without a signal,
    and only the controller knows that. During an outage this host waits (it is already
    recorded, so a stop still finds it); it never assumes it is authorized."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return client.call("heartbeat")
        except TriadError as exc:
            if not str(exc).startswith("Controller unavailable"):
                raise
            if time.monotonic() >= deadline:
                raise TriadError(f"No startup authorization within {timeout} seconds; harness not started") from exc
        time.sleep(0.5)


def host(spec_path):
    spec_path = Path(spec_path).resolve()
    spec = read_json(spec_path)
    root = spec_path.parent
    lock = FileLock(root / "host.lock").acquire()
    log = CappedLog(root / "output.log", spec["log_bytes"])
    job_handle = None
    writer_lock = None
    proc = None
    metadata = {"pid": os.getpid(), "identity": process_identity(os.getpid()),
                "started": now(), "contained": False, "exited": False}
    try:
        job_handle = contain_host()
        metadata["contained"] = True
        if spec["role"] == "worker":
            import hashlib
            workspace = Path(spec["workspace"]).resolve()
            suffix = hashlib.sha256(str(workspace).encode()).hexdigest()[:20]
            # Held by the session host, not by the SSH connection or controller.
            writer_lock = FileLock(workspace.parent / (".triad-writer-" + suffix + ".lock")).acquire()
        atomic_json(root / "host.json", metadata)
        client = Client(spec["state_dir"], spec["token"])
        authorize_startup(client, spec.get("startup_timeout", 600))
        env = os.environ.copy()
        env.update(TRIAD_STATE=spec["state_dir"], TRIAD_TOKEN=spec["token"],
                   TRIAD_ROLE=spec["role"], TRIAD_GENERATION=str(spec["generation"]),
                   PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
        harness = adapter(spec["mode"])
        structured = harness.capabilities.structured_stdio
        proc = subprocess.Popen(spec["argv"], cwd=spec["workspace"], env=env,
                                stdin=subprocess.PIPE if structured else None,
                                stdout=subprocess.PIPE if structured else None,
                                stderr=subprocess.PIPE if structured else None)
        output = queue.Queue(maxsize=256)

        def stdout_reader():
            while True:
                line = proc.stdout.readline(65537)
                if not line:
                    break
                log.write(line)
                try:
                    output.put(harness.decode(line))
                except (ValueError, TriadError) as exc:
                    output.put({"protocol_error": str(exc)})
                    return

        def stderr_reader():
            while chunk := proc.stderr.read(4096):
                # stderr is untrusted diagnostics, never protocol events.
                with stderr_lock:
                    stderr_log.write(chunk)

        stderr_lock = threading.Lock()
        stderr_log = CappedLog(root / "stderr.log", spec["log_bytes"])
        if structured:
            threading.Thread(target=stdout_reader, daemon=True).start()
            threading.Thread(target=stderr_reader, daemon=True).start()
        next_poll, next_heartbeat = 0, 0
        pending = None
        pending_request = None
        inbox_request = None
        subreaper = sys.platform.startswith("linux")

        def send(value):
            proc.stdin.write(harness.encode_message(value))
            proc.stdin.flush()

        while proc.poll() is None:
            if subreaper:
                reap_adopted(proc.pid)
            clock = time.monotonic()
            if clock >= next_heartbeat:
                try:
                    client.call("heartbeat")
                except TriadError:
                    pass  # Current bounded work survives a controller outage.
                next_heartbeat = clock + 2
            if structured:
                if pending is None:
                    try:
                        pending = output.get_nowait()
                        pending.setdefault("id", uid("adapter"))
                        # The harness ID only correlates the reply. The host owns idempotency, so a
                        # reused harness ID can never replay an earlier action's saved result.
                        pending_request = f"{spec['role']}-{spec['generation']}-{uid('action')}"
                    except queue.Empty:
                        pass
                if pending:
                    if "protocol_error" in pending:
                        raise TriadError(pending["protocol_error"])
                    if pending.get("type") != "action":
                        raise TriadError("JSONL stdout requires type=action; write diagnostics to stderr")
                    try:
                        result = client.call(pending["action"], pending.get("data", {}),
                                             request_id=pending_request)
                    except TriadError as exc:
                        if str(exc).startswith("Controller unavailable"):
                            time.sleep(0.5)
                            continue
                        send({"type": "action_result", "id": pending["id"], "ok": False, "error": str(exc)})
                    else:
                        send({"type": "action_result", "id": pending["id"], "ok": True, "result": result})
                    pending = None
                if clock >= next_poll:
                    # Retain request ID across an uncertain HTTP reply. Never submit a prompt twice.
                    inbox_request = inbox_request or uid("receive")
                    try:
                        message = client.call("inbox", request_id=inbox_request)
                        inbox_request = None
                        if message:
                            atomic_json(root / "last_delivery.json", {"message": message, "state": "dispatching"})
                            send({"type": "message", "message": message})
                            atomic_json(root / "last_delivery.json", {"message": message, "state": "written"})
                    except TriadError:
                        pass
                    next_poll = clock + 0.25
            time.sleep(0.03)
    except BaseException as exc:
        log.write(f"\nHost stopped: {type(exc).__name__}: {exc}\n")
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        metadata.update(exited=True, finished=now())
        atomic_json(root / "host.json", metadata)
        log.close()
        lock.close()
        if os.name != "nt" and metadata["contained"]:
            # This host is leaving; no background writer may outlive its session. Adopted
            # orphans outside the group go first, then the group, including ourselves, so even
            # descendants ignoring SIGTERM cannot survive.
            try:
                kill_posix_tree(os.getpid(), include_root=False)
            finally:
                os.killpg(os.getpgrp(), signal.SIGKILL)
        # Windows closes the job handle on process exit, killing all remaining descendants.


def job_active_processes(handle):
    """Number of live processes in a Windows Job Object."""
    from ctypes import wintypes

    class Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                 wintypes.DWORD, ctypes.c_void_p]
    info = Accounting()
    if not kernel.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
        raise TriadError(f"Cannot inspect check containment: {ctypes.get_last_error()}")
    return info.ActiveProcesses


def contain_check(status_path, argv):
    """Run one check inside its own containment boundary and exit only once it is empty.

    Output pipes say nothing about descendants that redirected their output, so quiescence
    is observed directly: on Linux this runner is the child subreaper of everything the
    check starts (including setsid/double-fork orphans) and waits until it has no children;
    other POSIX systems watch the check's process group; Windows watches its Job Object."""
    handle = contain_host()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    except OSError as exc:
        print(f"Cannot start check: {exc}", file=sys.stderr, flush=True)
        atomic_json(Path(status_path), {"exit_code": -1, "quiescent": True})
        return 1
    code = proc.wait()
    if sys.platform.startswith("linux"):
        while True:
            try:
                os.wait()  # Blocks until an adopted descendant exits.
            except ChildProcessError:
                break
    elif os.name == "nt":
        while job_active_processes(handle) > 1:  # This runner is the remaining member.
            time.sleep(0.1)
    else:
        from .backends import posix_processes
        group, me = os.getpgrp(), str(os.getpid())
        while any(row[0] != me and int(row[1]) == group and not row[2].startswith("Z")
                  for row in posix_processes("pid=,pgid=,stat=")):
            time.sleep(0.1)
    atomic_json(Path(status_path), {"exit_code": code, "quiescent": True})
    return 0


def run_check(client, task_id, name):
    run = client.call("run_start", {"task": task_id, "check": name}, retries=3)
    root = client.root / "evidence" / run["id"]
    root.mkdir(parents=True, exist_ok=True)
    logs = [CappedLog(root / "stdout.log", run["log_bytes"]),
            CappedLog(root / "stderr.log", run["log_bytes"])]
    status_path = root / "containment.json"
    timed_out = False
    quiescent = True
    proc = None
    try:
        from .backends import ENTRY
        # The runner, not the check, is the observed process: it outlives every descendant.
        proc = subprocess.Popen([sys.executable, "-B", str(ENTRY), "contain-check", "--status", str(status_path),
                                 "--", *run["argv"]], cwd=run["cwd"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

        def drain(stream, log):
            while chunk := stream.read(8192):
                log.write(chunk)

        threads = [threading.Thread(target=drain, args=(stream, log), daemon=True)
                   for stream, log in zip([proc.stdout, proc.stderr], logs)]
        for thread in threads:
            thread.start()
        try:
            code = proc.wait(timeout=run["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                # Killing the runner closes the check's job handle, which kills every member.
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                try:
                    kill_posix_group(proc.pid, 10, kill_posix_tree(proc.pid))
                except TriadError as exc:
                    logs[1].write(f"\n{exc}\n")
                    quiescent = False
            code = proc.wait(timeout=10)
        else:
            status = read_json(status_path) if status_path.exists() else None
            if status and status.get("quiescent"):
                code = status["exit_code"]
            else:
                # The runner died before observing an empty boundary: survivors are unknown.
                logs[1].write("\n[TRIAD: check containment ended before its descendants were confirmed stopped]\n")
                code, quiescent = -1, False
                if os.name != "nt":
                    try:
                        kill_posix_group(proc.pid, 10)
                    except TriadError:
                        pass
        for thread in threads:
            thread.join(timeout=2)
        if any(t.is_alive() for t in threads):
            # A child retained the output pipe: it must not count as a quiescent successful check.
            code = -1
            quiescent = False
            if os.name != "nt":
                kill_posix_tree(proc.pid)
    except OSError as exc:
        logs[1].write(str(exc))
        code = -1
    finally:
        # A retained pipe may still have a reader. Keep that run blocked until the
        # Supervisor terminates the contained Worker; do not claim quiescence.
        if quiescent:
            for log in logs:
                log.close()
    record = {"run": run["id"], "exit_code": code, "timed_out": timed_out,
              "truncated": any(log.truncated for log in logs), "quiescent": quiescent}
    atomic_json(root / "completion.json", record)
    return client.call("run_finish", record, retries=5)
