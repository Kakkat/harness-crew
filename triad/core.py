from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import sqlite3
import sys

from .backends import BACKENDS, ENTRY, alive, backend
from .connections import Connections, ConnectionUnavailable, validate_host
from .store import Store
from .util import TriadError, atomic_json, fingerprint, now, read_json, uid

ROLES = {"supervisor", "worker"}
READS = {"status", "events", "task", "observe", "storage", "evidence"}
SESSION_ACTIONS = {"inbox", "ack", "ready", "heartbeat", "progress"}
# Pauses that only the Designer may lift. The Supervisor may resume its own and deadline pauses.
DESIGNER_PAUSES = {"designer", "escalation", "takeover"}
# Idempotent emergency controls: they may use reserved storage and keep no request receipt.
CONTROLS = {"stop", "shutdown"}


def initialize(root, workspace, backend_name="local", hosts=None, workspace_host="local"):
    root = Path(root).resolve()
    hosts = {"local": {"kind": "local"}, **(hosts or {})}
    for name, definition in hosts.items():
        validate_host(name, definition)
    if workspace_host not in hosts:
        raise TriadError("Workspace host is not configured")
    if hosts[workspace_host]["kind"] == "local":
        workspace = Path(workspace).resolve()
        if not workspace.is_dir():
            raise TriadError("Workspace must be an existing directory")
        if root == workspace or workspace in root.parents:
            raise TriadError("State directory must be outside the workspace")
    elif not str(workspace).startswith("/") or ".." in PurePosixPath(workspace).parts:
        raise TriadError("Remote workspace must be an absolute Linux path")
    root.mkdir(parents=True, exist_ok=True)
    if (root / "config.json").exists():
        raise TriadError("State directory is already initialized")
    if os.name != "nt":
        root.chmod(0o700)  # Before the admin token exists on disk.
    config = {"version": 1, "job": uid("job"), "workspace": str(workspace),
              "backend": backend_name, "hosts": hosts, "workspace_host": workspace_host,
              "admin_token": secrets.token_hex(32),
              "log_bytes": 1024 * 1024, "storage_bytes": 128 * 1024 * 1024,
              "max_sessions": 32, "max_runs": 64, "excludes": [],
              "profiles": {
                  "demo-supervisor": {"mode": "jsonl", "argv": ["{python}", "{entry}", "demo-agent", "supervisor"]},
                  "demo-worker": {"mode": "jsonl", "argv": ["{python}", "{entry}", "demo-agent", "worker"]}}}
    if not Connections(root, config).get(workspace_host).exists(str(workspace)):
        raise TriadError("Workspace does not exist on its configured host")
    atomic_json(root / "config.json", config)
    if os.name != "nt":
        (root / "config.json").chmod(0o600)
    store = Store(root)
    with store.db:
        store.set_meta("job", {"id": config["job"], "state": "running", "design": None})
        store.event("initialized", {"workspace": str(workspace)})
    store.close()
    return {"state": str(root), "workspace": str(workspace), "backend": backend_name}


class Core:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config = read_json(self.root / "config.json")
        self.store = Store(self.root)
        self.connections = Connections(self.root, self.config)
        self.next_host_probe = {}
        self.terminated = set()  # (role, generation) confirmed stopped by this controller
        self.shutdown_requested = False

    def identity(self, token):
        if secrets.compare_digest(token, self.config["admin_token"]):
            return "designer", 0
        for session in self.store.all("sessions"):
            if secrets.compare_digest(token, session["token"]):
                if session["state"] in {"stopped", "exited"}:
                    raise TriadError("Session authority has been revoked")
                return session["role"], session["generation"]
        raise TriadError("Invalid or stale session credential")

    def authorize(self, who, action):
        role, _ = who
        if role == "designer":
            return
        allowed = READS | SESSION_ACTIONS
        if role == "supervisor":
            allowed |= {"assign", "accept", "correct", "replace", "start", "interrupt", "escalate", "pause", "resume"}
        if role == "worker":
            allowed |= {"result", "run_start", "run_finish", "blocked"}
        if action not in allowed:
            raise TriadError(f"Role {role} cannot perform {action}")

    def rpc(self, token, request):
        if not isinstance(request, dict) or set(request) - {"id", "action", "data"}:
            raise TriadError("Invalid request envelope")
        request_id, action, data = request.get("id"), request.get("action"), request.get("data", {})
        if not isinstance(request_id, str) or len(request_id) > 100 or not isinstance(data, dict):
            raise TriadError("Request requires a bounded ID and object body")
        who = self.identity(token)
        self.authorize(who, action)
        identity = f"{who[0]}:{who[1]}"
        body = json.dumps(data, sort_keys=True)
        # Polling and heartbeats are observations: never accumulate millions of receipts.
        cache = action not in READS | {"heartbeat"} | CONTROLS
        with self.store.reserve() if action in CONTROLS else nullcontext(), self.store.db:
            previous = self.store.db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if previous:
                if (previous["identity"], previous["action"], previous["body"]) != (identity, action, body):
                    raise TriadError("Request ID reused with different content or authority")
                return json.loads(previous["result"])
            function = getattr(self, "do_" + str(action), None)
            if not function:
                raise TriadError("Unknown action")
            result = function(who, data)
            if cache and result is not None:
                self.store.db.execute("INSERT INTO requests VALUES(?,?,?,?,?)",
                                      (request_id, identity, action, body, json.dumps(result)))
            return result

    def session(self, role):
        session = self.store.get("sessions", role)
        if not session:
            raise TriadError(f"No {role} session")
        return session

    def task(self, task_id):
        task = self.store.get("tasks", task_id)
        if not task:
            raise TriadError("Unknown task")
        return task

    def snapshot(self):
        return self.connections.get(self.config.get("workspace_host", "local")).snapshot(
            self.config["workspace"], self.config.get("excludes", []))

    def notify(self, kind, data):
        self.store.event(kind, data)
        session = self.store.get("sessions", "supervisor")
        if session and session["state"] not in {"stopped", "exited"}:
            return self.store.enqueue("supervisor", session["generation"], kind, data)

    def current(self, who, data):
        task = self.task(data["task"])
        if task.get("generation") != who[1] or task["state"] not in {"assigned", "working", "awaiting_verification"}:
            raise TriadError("Task does not belong to this active Worker generation")
        return task

    def do_status(self, who, data):
        sessions = [{k: v for k, v in s.items() if k not in {"token", "profile_data", "spec", "host_config"}}
                    for s in self.store.all("sessions")]
        return {"job": self.store.meta("job"), "sessions": sessions,
                "tasks": self.store.all("tasks"), "runs": self.store.all("runs"),
                "workspace": {"host": self.config.get("workspace_host", "local"), "path": self.config["workspace"]},
                "connection_errors": dict(self.connections.last_error),
                "reconcile": read_json(self.root / "reconcile.json") if (self.root / "reconcile.json").exists() else None}

    def do_events(self, who, data):
        return self.store.events(int(data.get("after", 0)))

    def do_task(self, who, data):
        return self.task(data["task"])

    def do_storage(self, who, data):
        files = list(self.root.rglob("*"))
        used = sum(p.stat().st_size for p in files if p.is_file())
        result = {"used_bytes": used, "budget_bytes": self.config["storage_bytes"],
                  "per_log_bytes": self.config["log_bytes"], "database_limit_bytes": 16 * 1024 * 1024}
        if data.get("remote"):
            result["remote_hosts"] = {}
            names = {s.get("host", "local") for s in self.store.all("sessions")}
            names.add(self.config.get("workspace_host", "local"))
            for name in names:
                connection = self.connections.get(name)
                if connection.kind == "ssh":
                    try:
                        result["remote_hosts"][name] = {"used_bytes": connection.call("storage", {"root": connection.job_root()}), "root": connection.job_root()}
                    except TriadError as exc:
                        result["remote_hosts"][name] = {"error": str(exc)}
        return result

    def storage_check(self, reserve=0):
        if self.do_storage(None, {})["used_bytes"] + reserve > self.config["storage_bytes"]:
            raise TriadError("Storage budget reached; archive completed state or increase its explicit budget")

    def do_design(self, who, data):
        text = data.get("text", "")
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise TriadError("Design must contain 1–32000 characters")
        job = self.store.meta("job")
        if any(t["state"] in {"assigned", "working", "awaiting_verification"} for t in self.store.all("tasks")):
            raise TriadError("Quiesce or replace active work before revising the design")
        job["design"] = {"revision": (job.get("design") or {}).get("revision", 0) + 1, "text": text}
        self.store.set_meta("job", job)
        self.notify("design", job["design"])
        return job["design"]

    def do_create_task(self, who, data):
        if not self.store.meta("job").get("design"):
            raise TriadError("Submit the Designer's design first")
        if not data.get("objective") or not data.get("checks"):
            raise TriadError("Task requires an objective and at least one named executable check")
        names = set()
        for check in data["checks"]:
            argv = check.get("argv")
            name = check.get("name")
            if not isinstance(name, str) or not name or name in names or not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                raise TriadError("Checks require unique names and nonempty argument vectors")
            timeout = check.get("timeout", 300)
            if not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
                raise TriadError("Check timeout must be between 0 and 86400 seconds")
            names.add(name)
        task = {"id": uid("task"), "revision": 1, "objective": data["objective"],
                "constraints": data.get("constraints", []), "checks": data["checks"],
                "state": "queued", "attempt": 0, "timeout": data.get("timeout", 3600),
                "design_revision": self.store.meta("job")["design"]["revision"]}
        if not isinstance(task["timeout"], (int, float)) or not 0 < task["timeout"] <= 86400:
            raise TriadError("Task timeout must be between 0 and 86400 seconds")
        self.store.put("tasks", task)
        self.notify("task_created", task)
        return task

    def do_start(self, who, data):
        role = data.get("role")
        if role not in ROLES or (who[0] == "supervisor" and role != "worker"):
            raise TriadError("Supervisor may start only Worker; roles are supervisor and worker")
        old = self.store.get("sessions", role)
        if old and old["state"] not in {"stopped", "exited"} and not old.get("start_error"):
            raise TriadError("Session already exists; use replacement for a new generation")
        if old and old["state"] != "stopped":
            # An exited group leader is not proof its descendants stopped, and a failed start
            # may still have spawned. Confirm termination before another generation.
            self.stop_role(role)
        if self.store.meta("job")["state"] == "stopped":
            raise TriadError("Resume the job before starting sessions")
        self.storage_check(3 * self.config["log_bytes"])
        count = self.store.meta("session_count", 0)
        if count >= self.config["max_sessions"]:
            raise TriadError("Session generation budget reached")
        self.config = read_json(self.root / "config.json")
        # Host routing for existing sessions is pinned; profile commands may be refreshed.
        self.connections.config = self.config
        profile_name = data["profile"]
        profile = self.config["profiles"].get(profile_name)
        if not profile or profile.get("mode") not in {"jsonl", "cooperative"}:
            raise TriadError("Profile must select jsonl or cooperative mode")
        if not isinstance(profile.get("argv"), list) or not profile["argv"] or not all(isinstance(x, str) for x in profile["argv"]):
            raise TriadError("Profile requires an executable argument vector")
        backend_name = data.get("backend", self.config["backend"])
        if backend_name not in BACKENDS:
            raise TriadError(f"Unknown backend: {backend_name}")
        if backend_name == "local" and profile["mode"] == "cooperative":
            raise TriadError("Interactive cooperative profiles require psmux/tmux/tsmux")
        host_name = data.get("host", self.config.get("workspace_host", "local"))
        connection = self.connections.get(host_name)
        workspace = data.get("workspace") or (
            self.config["workspace"] if host_name == self.config.get("workspace_host", "local")
            else connection.config.get("workspace"))
        if not workspace or not connection.exists(workspace):
            raise TriadError("Session needs an existing workspace on its selected host")
        if role == "worker" and (host_name != self.config.get("workspace_host", "local") or workspace != self.config["workspace"]):
            raise TriadError("Worker must use the job's canonical workspace host/path; repository migration must be explicit")
        # A spawn that cannot happen must not reserve (and spend) a generation.
        connection.check_backend(backend_name)
        generation = (old or {}).get("generation", 0) + 1
        session_dir = self.root / "sessions" / f"{role}-g{generation}"
        session_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(32)
        session = {"role": role, "generation": generation, "state": "starting", "turn": "unknown",
                   "profile": profile_name, "backend": backend_name, "host": host_name,
                   "host_config": connection.config, "workspace": workspace, "created": now(), "heartbeat": None,
                   "token": token, "spec": str(session_dir / "spec.json")}
        recovery = {"design": self.store.meta("job").get("design"), "tasks": self.store.all("tasks"),
                    "runs": self.store.all("runs"), "snapshot": self.snapshot()}
        atomic_json(session_dir / "handoff.json", recovery)
        bootstrap = session_dir / "bootstrap.md"
        execution_dir = (str(PurePosixPath(connection.job_root()) / "sessions" / f"{role}-g{generation}")
                         if connection.kind == "ssh" else str(session_dir))
        execution_bootstrap = execution_dir + "/bootstrap.md"
        cli = f'"{connection.python}" "{connection.entry}"'
        bootstrap_text = (
            f"You are the {role.upper()} logical role. Read {execution_dir}/handoff.json.\n"
            "The controller is infrastructure and may run on another host. Keep implementation decisions within the design.\n"
            f"Use {cli} --help for the role API. TRIAD_STATE and TRIAD_TOKEN are already set.\n"
            f"Begin with: {cli} ready\nThen: {cli} inbox --wait\n"
            "Acknowledge every delivered message using ack --message ID. Read one message at a time.\n"
            "Worker: inspect, implement, run named checks, fix and repeat independently. Report result or blocked.\n"
            "Supervisor: start/reuse Worker; assign tasks; interpret results; accept only current evidence; escalate design issues.\n"
            "After handling each message call ready, then inbox --wait again. This marks an explicit protocol boundary.\n"
            "Never communicate Worker results directly to Designer. Keep summaries short and reference run IDs.\n"
            "Do not launch detached/unowned processes, bypass process containment, or edit controller state.\n")
        bootstrap.write_text(bootstrap_text, encoding="utf-8")
        argv = [part.replace("{bootstrap}", execution_bootstrap).replace("{workspace}", workspace)
                .replace("{python}", connection.python).replace("{entry}", connection.entry)
                for part in profile["argv"]]
        spec = {**session, "state_dir": connection.job_root(), "workspace": workspace,
                "mode": profile["mode"], "argv": argv, "log_bytes": self.config["log_bytes"],
                "session_name": f"triad-{self.config['job'][-8:]}-{role}-g{generation}"}
        atomic_json(Path(session["spec"]), spec)
        self.store.put("sessions", session)
        self.store.set_meta("session_count", count + 1)
        self.store.event("session_reserved", {"role": role, "generation": generation})
        # Reserve before spawning. A crash leaves a discoverable generation, never an invisible process.
        self.store.db.commit()
        try:
            spec = connection.prepare(spec, bootstrap_text, recovery)
            atomic_json(Path(session["spec"]), spec)
            transport = connection.session("start", session)
        except Exception as exc:
            # A later start confirms this generation stopped, then replaces it.
            session.update(state="unknown", start_error=f"{type(exc).__name__}: {exc}"[:1000])
            self.store.put("sessions", session)
            self.store.db.commit()
            raise
        self.store.event("session_started", {"role": role, "generation": generation, "transport": transport})
        if role == "supervisor":
            self.store.enqueue(role, generation, "recover", recovery)
        return {k: v for k, v in session.items() if k not in {"token", "host_config"}} | {"bootstrap": execution_bootstrap}

    def do_heartbeat(self, who, data):
        if who[0] not in ROLES:
            raise TriadError("Heartbeat requires a session credential")
        session = self.session(who[0])
        session["heartbeat"] = now()
        session.pop("start_error", None)  # A live host proves the start succeeded after all.
        if session["state"] in {"starting", "unknown"}:
            session["state"] = "alive"
        self.store.put("sessions", session)
        return {"job_state": self.store.meta("job")["state"]}

    def do_ready(self, who, data):
        if who[0] not in ROLES:
            raise TriadError("Ready requires a session credential")
        session = self.session(who[0])
        outstanding = self.store.db.execute(
            "SELECT id FROM messages WHERE recipient=? AND generation=? AND state='submitted'",
            who).fetchone()
        if outstanding:
            raise TriadError(f"Acknowledge submitted message {outstanding['id']} before declaring ready")
        if who[0] == "worker" and any(r["state"] in {"running", "unknown"} for r in self.store.all("runs")):
            raise TriadError("A verification command is still running")
        session.update(state="alive", turn="ready", heartbeat=now())
        self.store.put("sessions", session)
        self.store.event("turn_ended", {"role": who[0], "generation": who[1]})
        if who[0] == "worker":
            self.notify("worker_ready", {"generation": who[1]})
        return {"ready": True}

    def do_inbox(self, who, data):
        if who[0] not in ROLES:
            raise TriadError("Inbox requires a session credential")
        session = self.session(who[0])
        job = self.store.meta("job")
        if job["state"] == "stopped":
            return None
        if data.get("redeliver"):
            # A reply lost after submission stays recoverable: this is not a new dispatch.
            row = self.store.db.execute(
                "SELECT data FROM messages WHERE recipient=? AND generation=? AND state='submitted' ORDER BY created,rowid LIMIT 1",
                who).fetchone()
            if row:
                return json.loads(row["data"]) | {"redelivered": True}
        if ((who[0] == "worker" and job["state"] != "running")
                or job.get("takeover") == who[0] or session["turn"] != "ready"):
            return None
        row = self.store.db.execute(
            "SELECT * FROM messages WHERE recipient=? AND generation=? AND state='queued' ORDER BY created,rowid LIMIT 1", who).fetchone()
        if not row:
            return None
        self.store.db.execute("UPDATE messages SET state='submitted' WHERE id=?", (row["id"],))
        session["turn"] = "running"
        self.store.put("sessions", session)
        self.store.event("message_submitted", {"id": row["id"], "role": who[0]})
        return json.loads(row["data"])

    def do_ack(self, who, data):
        row = self.store.db.execute("SELECT * FROM messages WHERE id=?", (data["message"],)).fetchone()
        if not row or (row["recipient"], row["generation"]) != who:
            raise TriadError("Message does not belong to this session")
        if row["state"] not in {"submitted", "acknowledged"}:
            raise TriadError("Message was not submitted")
        self.store.db.execute("UPDATE messages SET state='acknowledged' WHERE id=?", (row["id"],))
        self.store.event("message_acknowledged", {"id": row["id"]})
        return {"acknowledged": row["id"]}

    def do_assign(self, who, data):
        if self.store.meta("job")["state"] != "running":
            raise TriadError("Job is paused or stopped")
        session = self.session("worker")
        if session["state"] != "alive" or session["turn"] != "ready":
            raise TriadError("Worker is not ready")
        tasks = self.store.all("tasks")
        if any(t["state"] in {"assigned", "working", "awaiting_verification"} for t in tasks):
            raise TriadError("One active task is allowed; resolve the current task first")
        task = self.task(data["task"])
        if task["state"] not in {"queued", "blocked"}:
            raise TriadError("Task is not assignable")
        if task["design_revision"] != self.store.meta("job")["design"]["revision"]:
            raise TriadError("Task has an old design revision; create a revised task")
        task.update(state="assigned", generation=session["generation"], attempt=task["attempt"] + 1,
                    assigned_at=now(), result=None, deadline_reported=False, stall_reported=False)
        self.store.put("tasks", task)
        return self.store.enqueue("worker", session["generation"], "assign", task)

    def do_progress(self, who, data):
        summary = data.get("summary", "")
        if not isinstance(summary, str) or len(summary) > 2000:
            raise TriadError("Progress summary exceeds 2000 characters")
        if who[0] == "worker":
            task = self.current(who, data)
            task.update(state="working", progress=summary, progress_at=now(), stall_reported=False)
            self.store.put("tasks", task)
        self.store.event("progress", {"role": who[0], **data})
        return {"recorded": True}

    def do_result(self, who, data):
        task = self.current(who, data)
        if any(r["state"] in {"running", "unknown"} for r in self.store.all("runs")):
            raise TriadError("Cannot submit result while a verification command runs")
        if not isinstance(data.get("summary"), str) or len(data["summary"]) > 4000:
            raise TriadError("Result needs a summary of at most 4000 characters")
        evidence = data.get("evidence", [])
        if not isinstance(evidence, list) or not evidence:
            raise TriadError("Candidate completion requires evidence run IDs")
        for run_id in evidence:
            run = self.store.get("runs", run_id)
            if not run or (run["task"], run["attempt"], run["generation"]) != (task["id"], task["attempt"], who[1]):
                raise TriadError("Evidence belongs to a different task or attempt")
        task.update(state="awaiting_verification", result={"summary": data["summary"], "evidence": evidence,
                    "snapshot": self.snapshot(), "open_issues": data.get("open_issues", [])})
        self.store.put("tasks", task)
        self.notify("result", {"task": task["id"], **task["result"]})
        return task

    def do_blocked(self, who, data):
        task = self.current(who, data)
        task.update(state="blocked", reason=str(data.get("reason", "Unspecified blocker"))[:4000])
        self.store.put("tasks", task)
        self.notify("blocked", {"task": task["id"], "reason": task["reason"]})
        return task

    def do_run_start(self, who, data):
        task = self.current(who, data)
        if self.store.meta("job")["state"] != "running":
            raise TriadError("Job is not running")
        if any(r["state"] in {"running", "unknown"} for r in self.store.all("runs")):
            raise TriadError("Only one verification command at a time")
        if len(self.store.all("runs")) >= self.config["max_runs"]:
            raise TriadError("Run budget reached")
        check = next((c for c in task["checks"] if c["name"] == data["check"]), None)
        if not check:
            raise TriadError("Unknown acceptance check")
        self.storage_check(2 * self.config["log_bytes"])
        connection = self.connections.for_session(self.session("worker"))
        if connection.kind == "ssh":
            used = connection.call("storage", {"root": connection.job_root()})
            if used + 2 * self.config["log_bytes"] > self.config["storage_bytes"]:
                raise TriadError("Remote evidence storage budget reached")
        run = {"id": uid("run"), "task": task["id"], "attempt": task["attempt"], "generation": who[1],
               "host": connection.name, "evidence_root": connection.job_root(),
               "check": check["name"], "timeout": check.get("timeout", 300),
               # {python} and {entry} resolve on the Worker's host, so checks can call this harness's CLI (e.g. `gate`).
               "argv": [a.replace("{python}", connection.python).replace("{entry}", connection.entry) for a in check["argv"]],
               "cwd": self.config["workspace"], "before": self.snapshot(), "state": "running", "started": now()}
        self.store.put("runs", run)
        self.store.event("run_started", run)
        return run | {"log_bytes": self.config["log_bytes"]}

    def do_run_finish(self, who, data):
        run = self.store.get("runs", data["run"])
        if not run or run["generation"] != who[1] or run["state"] != "running":
            raise TriadError("Run is not active in this generation")
        code = data.get("exit_code")
        if not isinstance(code, int):
            raise TriadError("Run requires an integer exit code")
        run.update(state="finished" if data.get("quiescent", True) else "unknown", finished=now(), exit_code=code, timed_out=bool(data.get("timed_out")),
                   after=self.snapshot(), truncated=bool(data.get("truncated")))
        run["valid"] = run["state"] == "finished" and code == 0 and not run["timed_out"] and run["before"] == run["after"]
        self.store.put("runs", run)
        self.store.event("run_finished", run)
        return run

    def do_accept(self, who, data):
        task = self.task(data["task"])
        session = self.session("worker")
        if task["state"] != "awaiting_verification" or session["turn"] != "ready":
            raise TriadError("Need a candidate result and a quiescent Worker")
        snapshot = self.snapshot()
        if task["result"]["snapshot"] != snapshot:
            raise TriadError("Workspace changed after result; evidence is stale")
        if task["result"].get("open_issues"):
            raise TriadError("Resolve reported open issues before acceptance")
        runs = [self.store.get("runs", r) for r in task["result"]["evidence"]]
        for check in task["checks"]:
            candidates = [r for r in self.store.all("runs") if r["task"] == task["id"] and
                          r["attempt"] == task["attempt"] and r["check"] == check["name"]]
            latest = candidates[-1] if candidates else None
            if not latest or latest not in runs or not latest.get("valid") or latest["after"] != snapshot:
                raise TriadError(f"Missing current passing evidence: {check['name']}")
        task.update(state="accepted", accepted_at=now(), accepted_by=who[0])
        self.store.put("tasks", task)
        self.store.event("phase_complete", {"task": task["id"], "summary": task["result"]["summary"]})
        return task

    def do_correct(self, who, data):
        if self.store.meta("job")["state"] != "running":
            raise TriadError("Job is paused or stopped")
        task = self.task(data["task"])
        session = self.session("worker")
        if (session["state"] in {"stopped", "exited"} or task.get("generation") != session["generation"]
                or task["state"] not in {"assigned", "working", "awaiting_verification", "blocked"}):
            raise TriadError("Cannot correct this task in the current generation")
        if any(t["id"] != task["id"] and t["state"] in {"assigned", "working", "awaiting_verification"}
               for t in self.store.all("tasks")):
            raise TriadError("One active task is allowed; resolve the current task first")
        if task["design_revision"] != self.store.meta("job")["design"]["revision"]:
            raise TriadError("Task has an old design revision; create a revised task")
        task.update(state="working", result=None)
        self.store.put("tasks", task)
        return self.store.enqueue("worker", session["generation"], "correct",
                                  {"task": task, "instruction": str(data["instruction"])[:8000]})

    def stop_role(self, role):
        session = self.session(role)
        if session["state"] == "stopped":
            return session
        self.terminate(session)
        return self.record_stopped(session)

    def terminate(self, session):
        """Stop a session's processes. Uses only its spec and host identity, never new writes."""
        key = (session["role"], session["generation"])
        if key not in self.terminated:
            self.connections.for_session(session).session("stop", session)
            self.terminated.add(key)

    def persist(self, errors, label, action):
        """Record control state using reserved capacity. A storage failure is kept, not raised."""
        try:
            with self.store.reserve():
                action()
                self.store.db.commit()
        except sqlite3.Error as exc:
            self.store.db.rollback()
            errors.append(f"{label} not recorded: {exc}")

    def record_reconcile(self, errors):
        record = {"time": now(), "errors": errors,
                  "terminated": [f"{role}-g{generation}" for role, generation in sorted(self.terminated)]}
        try:
            atomic_json(self.root / "reconcile.json", record)
        except OSError:
            pass  # The error is still reported to the caller.

    def record_stopped(self, session):
        role = session["role"]
        session.update(state="stopped", turn="ended")
        self.store.put("sessions", session)
        self.store.db.execute("UPDATE messages SET state='abandoned' WHERE recipient=? AND generation=? AND state IN ('queued','submitted')",
                              (role, session["generation"]))
        if role == "worker":
            for run in self.store.all("runs"):
                if run["state"] in {"running", "unknown"}:
                    run.update(state="interrupted", valid=False)
                    self.store.put("runs", run)
            for task in self.store.all("tasks"):
                if task["state"] in {"assigned", "working", "awaiting_verification"}:
                    task.update(state="blocked", reason="Worker stopped; reconcile preserved workspace before reassignment")
                    self.store.put("tasks", task)
        self.store.event("session_stopped", {"role": role, "generation": session["generation"]})
        return session

    def refuse_during_takeover(self, who):
        if who[0] == "supervisor" and self.store.meta("job").get("takeover"):
            raise TriadError("A human has taken over a session; wait for the Designer to resume")

    def do_interrupt(self, who, data):
        role = data.get("role", "worker")
        if who[0] == "supervisor" and role != "worker":
            raise TriadError("Supervisor can interrupt only Worker")
        self.refuse_during_takeover(who)
        self.stop_role(role)
        return {"stopped": role, "note": "Portable hard interruption; restart creates a new generation"}

    def do_replace(self, who, data):
        role = data.get("role", "worker")
        if who[0] == "supervisor" and role != "worker":
            raise TriadError("Supervisor can replace only Worker")
        self.refuse_during_takeover(who)
        old = self.stop_role(role)
        self.store.db.commit()
        return self.do_start(who, {"role": role, "profile": data.get("profile", old["profile"]),
                                  "backend": data.get("backend", old["backend"]),
                                  "host": data.get("host", old.get("host", "local")),
                                  "workspace": data.get("workspace", old.get("workspace", self.config["workspace"]))})

    def do_pause(self, who, data, reason=None):
        job = self.store.meta("job")
        # A later pause never downgrades one that only the Designer may lift.
        if not (job["state"] == "paused" and job.get("paused_by") in DESIGNER_PAUSES):
            job["paused_by"] = reason or who[0]
        job["state"] = "paused"
        self.store.set_meta("job", job)
        self.store.event("paused", {"by": job["paused_by"], "note": "Dispatch paused; an active bounded task may continue"})
        return job

    def do_resume(self, who, data):
        job = self.store.meta("job")
        if who[0] != "designer" and job["state"] != "running" and (
                job.get("takeover") or job.get("paused_by", "designer") in DESIGNER_PAUSES):
            raise TriadError("Only the Designer can resume after a Designer pause, takeover or escalation")
        job["state"] = "running"
        job.pop("takeover", None)
        job.pop("paused_by", None)
        self.store.set_meta("job", job)
        self.store.event("resumed", {})
        return job

    def do_escalate(self, who, data):
        self.do_pause(who, {}, reason="escalation")
        self.store.event("escalation", data)
        return {"escalated": True, "details": data}

    def do_takeover(self, who, data):
        session = self.session(data["role"])
        if session["turn"] != "ready" or any(r["state"] in {"running", "unknown"} for r in self.store.all("runs")):
            raise TriadError("Wait for a ready session and completed commands before takeover")
        command = self.connections.for_session(session).session("attach", session)
        self.do_pause(who, {}, reason="takeover")
        job = self.store.meta("job")
        job["takeover"] = data["role"]
        self.store.set_meta("job", job)
        self.store.event("human_takeover", {"role": data["role"]})
        return {"argv": command, "note": "Resume explicitly after reconciling human edits"}

    def do_observe(self, who, data):
        session = self.session(data["role"])
        return {"output": self.connections.for_session(session).session("capture", session)}

    def do_evidence(self, who, data):
        run = self.store.get("runs", data["run"])
        if not run:
            raise TriadError("Unknown evidence run")
        connection = self.connections.get(run.get("host", "local"))
        base = run.get("evidence_root", str(self.root))
        directory = str(PurePosixPath(base) / "evidence" / run["id"]) if connection.kind == "ssh" else str(Path(base) / "evidence" / run["id"])
        return {"run": run["id"], "host": connection.name, "output": connection.call("evidence", {
            "directory": directory, "file": data.get("file", "stdout.log"), "bytes": data.get("bytes", 16384)})}

    def do_stop(self, who, data):
        # Emergency path: every owned session is stopped even when the database cannot record
        # it. Failures are collected, reported, and kept in reconcile.json for a later stop.
        errors = []
        self.persist(errors, "pause", lambda: self.do_pause(who, {}))
        for role in ["worker", "supervisor"]:
            session = self.store.get("sessions", role)
            if not session or session["state"] == "stopped":
                continue
            try:
                self.terminate(session)
            except Exception as exc:
                errors.append(f"{role} stop unconfirmed: {exc}")
                continue
            self.persist(errors, f"{role} stop", lambda: self.record_stopped(session))
        if errors:
            self.record_reconcile(errors)
            raise TriadError("Stop incomplete; " + "; ".join(errors) +
                             ". Terminated sessions stay stopped; resolve the cause and run stop again to reconcile")
        reconcile = self.root / "reconcile.json"

        def finish():
            job = self.store.meta("job")
            job["state"] = "stopped"
            self.store.set_meta("job", job)
            if reconcile.exists():
                self.store.event("reconciled", read_json(reconcile))
            self.store.event("stopped", {})
        self.persist(errors, "job stop", finish)
        if errors:
            self.record_reconcile(errors)
            raise TriadError(f"All sessions stopped; {errors[0]}. Run stop again once storage is available")
        reconcile.unlink(missing_ok=True)
        return self.store.meta("job")

    def do_shutdown(self, who, data):
        stopped = all(s["state"] == "stopped" or (s["role"], s["generation"]) in self.terminated
                      for s in self.store.all("sessions"))
        if self.store.meta("job")["state"] != "stopped" and not stopped:
            raise TriadError("Stop owned sessions before shutting down the controller")
        # Also held in memory: a full database must not keep the controller running.
        self.shutdown_requested = True
        errors = []
        self.persist(errors, "shutdown", lambda: self.store.set_meta("shutdown", True))
        return {"shutdown": True, **({"errors": errors} if errors else {})}

    def tick(self):
        self.connections.tick(self.store.all("sessions"))
        with self.store.db:
            for task in self.store.all("tasks"):
                if task["state"] not in {"assigned", "working"}:
                    continue
                age = now() - task["assigned_at"]
                if age > task["timeout"] and not task.get("deadline_reported"):
                    self.store.db.commit()
                    errors = []

                    def report():
                        task["deadline_reported"] = True
                        self.store.put("tasks", task)
                        self.notify("task_deadline", {"task": task["id"], "timeout": task["timeout"]})
                        self.do_pause(("designer", 0), {}, reason="deadline")
                    self.persist(errors, "deadline", report)
                    # Deadline enforcement is deterministic policy, not a new reasoning role.
                    # The Worker is stopped even when the deadline could not be recorded.
                    worker = self.session("worker")
                    try:
                        if worker["state"] != "stopped":
                            self.terminate(worker)
                            self.persist(errors, "worker stop", lambda: self.record_stopped(worker))
                    except TriadError as exc:
                        self.persist(errors, "stop_unconfirmed", lambda: self.store.event("stop_unconfirmed", {"error": str(exc)}))
                    if errors:
                        self.record_reconcile(errors)
                        raise TriadError("Deadline enforcement not fully recorded: " + "; ".join(errors))
                elif now() - task.get("progress_at", task["assigned_at"]) > 300 and not task.get("stall_reported"):
                    task["stall_reported"] = True
                    self.store.put("tasks", task)
                    self.notify("progress_review", {"task": task["id"], "note": "Quiet is not failure; inspect active commands"})
            for session in self.store.all("sessions"):
                if session["state"] in {"stopped", "exited"}:
                    continue
                try:
                    connection = self.connections.for_session(session)
                except TriadError as exc:
                    if session["state"] != "unknown":
                        session["state"] = "unknown"
                        self.store.put("sessions", session)
                        self.store.event("host_configuration_mismatch", {"role": session["role"], "error": str(exc)})
                    continue
                key = (session["role"], session["generation"])
                observed = None
                if connection.kind == "local" or (
                    now() - (session["heartbeat"] or session["created"]) > 10 and now() >= self.next_host_probe.get(key, 0)):
                    try:
                        observed = connection.session("alive", session)
                    except TriadError as exc:
                        self.connections.last_error[connection.name] = str(exc)
                        if session["state"] != "unknown":
                            session["state"] = "unknown"
                            self.store.put("sessions", session)
                            self.notify("session_unreachable", {"role": session["role"], "host": connection.name})
                    self.next_host_probe[key] = now() + 15
                if observed is False:
                    session.update(state="exited", turn="unknown")
                    self.store.put("sessions", session)
                    self.notify("session_exited", {"role": session["role"], "generation": session["generation"]})
                elif now() - (session["heartbeat"] or session["created"]) > 60 and session["state"] != "unknown":
                    session["state"] = "unknown"
                    self.store.put("sessions", session)
                    self.notify("session_unreachable", {"role": session["role"], "generation": session["generation"]})
