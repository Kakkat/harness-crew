from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .backends import ENTRY
from .client import Client
from .core import initialize
from .util import TriadError, atomic_json, read_json, uid


def parser():
    p = argparse.ArgumentParser(description="Designer -> Supervisor -> Worker, independent of AI vendor")
    p.add_argument("--state", default=os.environ.get("TRIAD_STATE", ".triad"), help="State directory, outside the workspace")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Initialize local job state")
    init.add_argument("--workspace", required=True)
    init.add_argument("--hosts-file", help="JSON map of host aliases to local/SSH connection definitions")
    init.add_argument("--workspace-host", default="local")
    init.add_argument("--backend", choices=["local", "psmux", "tmux", "tsmux"], default="local")
    serve = sub.add_parser("serve", help="Run controller in foreground")
    serve.add_argument("--port", type=int, default=0)
    sub.add_parser("up", help="Start detached local controller")
    profile = sub.add_parser("profile", help="Register an interchangeable harness launch profile")
    profile.add_argument("name")
    profile.add_argument("--mode", choices=["jsonl", "cooperative"], required=True)
    profile.add_argument("--argv-file", required=True, help="JSON file containing executable argument array")
    design = sub.add_parser("design")
    design.add_argument("--file", required=True)
    task = sub.add_parser("create-task")
    task.add_argument("--file", required=True, help="JSON with objective, constraints, named checks")
    for name in ["status", "storage", "pause", "resume", "stop", "down", "ready"]:
        command = sub.add_parser(name)
        if name == "storage":
            command.add_argument("--remote", action="store_true", help="Also measure state on SSH hosts")
    events = sub.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    for name in ["start", "replace"]:
        q = sub.add_parser(name)
        q.add_argument("role", choices=["supervisor", "worker"])
        q.add_argument("--profile", required=name == "start")
        q.add_argument("--backend", choices=["local", "psmux", "tmux", "tsmux"])
        q.add_argument("--host", help="Execution host alias; defaults to the workspace host")
        q.add_argument("--workspace", help="Existing directory on that host (Supervisor may use a separate checkout)")
    for name in ["interrupt", "observe", "attach"]:
        q = sub.add_parser(name)
        q.add_argument("role", choices=["supervisor", "worker"])
    inbox = sub.add_parser("inbox", help="Receive one message; --wait uses no model calls")
    inbox.add_argument("--wait", action="store_true")
    inbox.add_argument("--timeout", type=float, default=3600)
    ack = sub.add_parser("ack")
    ack.add_argument("--message", required=True)
    for name in ["task", "assign", "accept"]:
        q = sub.add_parser(name)
        q.add_argument("--task", required=True)
    correct = sub.add_parser("correct")
    correct.add_argument("--task", required=True)
    correct.add_argument("--instruction", required=True)
    check = sub.add_parser("check", help="Execute a declared acceptance check and capture bounded evidence")
    check.add_argument("--task", required=True)
    check.add_argument("--name", required=True)
    result = sub.add_parser("result")
    result.add_argument("--task", required=True)
    result.add_argument("--summary", required=True)
    result.add_argument("--evidence", nargs="+", required=True)
    for name in ["progress", "blocked"]:
        q = sub.add_parser(name)
        q.add_argument("--task", required=True)
        q.add_argument("--summary" if name == "progress" else "--reason", required=True)
    escalation = sub.add_parser("escalate")
    escalation.add_argument("--reason", required=True)
    escalation.add_argument("--recommendation", required=True)
    host = sub.add_parser("host", help=argparse.SUPPRESS)
    host.add_argument("--spec", required=True)
    gate = sub.add_parser("gate", help="Check command: pass only if Jev confidently gives the expected yes/no answer")
    gate.add_argument("question")
    gate.add_argument("--expect", choices=["yes", "no"], required=True)
    gate.add_argument("--min", type=float, default=0.8, help="Required confidence in the expected answer (default 0.8)")
    material = gate.add_mutually_exclusive_group()
    material.add_argument("--diff", metavar="BASE", help="Judge `git diff BASE` plus untracked files in the workspace")
    material.add_argument("--file", help="Judge a file's contents")
    material.add_argument("--text", help="Judge this text (default: stdin)")
    gate.add_argument("--model", help="Jev model (default jev-latest)")
    gate.add_argument("--key-file", help="File with the TypeSafe API key (default ~/.config/typesafe/api_key)")
    audit = sub.add_parser("review", help="Advisory check: first-pass Jev review; prints the functions that stand out")
    audit.add_argument("paths", nargs="+", help="Files or folders (Python split by function)")
    audit.add_argument("--context", help="One-paragraph project description, to reduce false alarms")
    audit.add_argument("--min", type=float, default=0.7, help="Lowest probability that can be flagged (default 0.7)")
    audit.add_argument("--model", help="Jev model (default jev-latest)")
    audit.add_argument("--key-file", help="File with the TypeSafe API key (default ~/.config/typesafe/api_key)")
    contained = sub.add_parser("contain-check", help=argparse.SUPPRESS)
    contained.add_argument("--status", required=True)
    contained.add_argument("argv", nargs=argparse.REMAINDER)
    sub.add_parser("remote-rpc", help="Internal SSH helper")
    probe = sub.add_parser("host-check", help="Check SSH, Python and multiplexer without starting an AI session")
    probe.add_argument("--hosts-file", required=True)
    probe.add_argument("--host", required=True)
    probe.add_argument("--backend", default="tmux", choices=["local", "tmux", "tsmux", "psmux"])
    evidence = sub.add_parser("evidence", help="Read bounded evidence from its owning host")
    evidence.add_argument("--run", required=True)
    evidence.add_argument("--file", choices=["stdout.log", "stderr.log", "completion.json"], default="stdout.log")
    evidence.add_argument("--bytes", type=int, default=16384)
    agent = sub.add_parser("demo-agent", help=argparse.SUPPRESS)
    agent.add_argument("role", choices=["supervisor", "worker"])
    demo = sub.add_parser("demo", help="Run a complete deterministic job; no AI account or packages needed")
    demo.add_argument("--directory", required=True, help="A new directory for both demo workspace and state")
    demo.add_argument("--backend", choices=["local", "psmux", "tmux", "tsmux"], default="local")
    return p


def start_controller(root):
    root = Path(root).resolve()
    try:
        Client(root).call("status")
        return {"already_running": True, "state": str(root)}
    except (TriadError, FileNotFoundError):
        pass
    proc = subprocess.Popen([sys.executable, str(ENTRY), "--state", str(root), "serve"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=os.name != "nt",
                            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                            if os.name == "nt" else 0)
    for _ in range(100):
        if proc.poll() is not None:
            raise TriadError("Controller exited; run 'serve' in foreground to see the error")
        try:
            Client(root).call("status")
            return {"pid": proc.pid, "state": str(root)}
        except (TriadError, FileNotFoundError):
            time.sleep(0.1)
    raise TriadError("Controller startup timed out")


def demo(directory, backend):
    root = Path(directory).resolve()
    if root.exists():
        raise TriadError("Demo requires a new directory to avoid modifying existing work")
    workspace, state = root / "workspace", root / "state"
    workspace.mkdir(parents=True)
    initialize(state, workspace, backend)
    start_controller(state)
    client = Client(state)
    try:
        client.call("design", {"text": "Create greeting.txt containing hello triad. Verify its exact contents."})
        task = client.call("create_task", {"objective": "Create the greeting file", "checks": [{"name": "greeting",
            "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('greeting.txt').read_text() == 'hello triad\\n'"],
            "timeout": 20}]})
        client.call("start", {"role": "worker", "profile": "demo-worker"})
        client.call("start", {"role": "supervisor", "profile": "demo-supervisor"})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = client.call("status")
            if status["tasks"][0]["state"] == "accepted":
                return {"accepted": task["id"], "workspace": str(workspace), "state": str(state),
                        "storage": client.call("storage")}
            time.sleep(0.25)
        raise TriadError(f"Demo timed out; inspect with --state {state} status / events / observe")
    finally:
        # Leave durable evidence available, but do not leave demo AI processes running.
        client.call("stop")
        client.call("shutdown")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    command = args.command
    try:
        if command == "init":
            result = initialize(args.state, args.workspace, args.backend,
                                read_json(Path(args.hosts_file)) if args.hosts_file else None, args.workspace_host)
        elif command == "serve":
            from .server import serve
            serve(args.state, args.port)
            return
        elif command == "host":
            from .runtime import host
            host(args.spec)
            return
        elif command == "gate":
            from .gate import api_key, gate, git_diff
            material = (git_diff(args.diff) if args.diff else Path(args.file).read_text(encoding="utf-8")
                        if args.file else args.text if args.text is not None else sys.stdin.read())
            passed, p_yes, model = gate(args.question, args.expect, args.min, material, api_key(args.key_file), args.model)
            print(f"gate {'PASS' if passed else 'FAIL'}: P(yes)={p_yes:.1%}, expected {args.expect} "
                  f"with at least {args.min:.0%} confidence ({model})")
            raise SystemExit(0 if passed else 1)
        elif command == "review":
            from .gate import api_key
            from .review import digest, review
            scored, flags = review(args.paths, api_key(args.key_file), args.context, args.model, args.min)
            print(digest(scored, flags))
            return
        elif command == "contain-check":
            from .runtime import contain_check
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            raise SystemExit(contain_check(args.status, argv))
        elif command == "remote-rpc":
            from .remote import main as remote_main
            remote_main()
            return
        elif command == "host-check":
            from .connections import Connections
            manager = Connections(Path(args.state).resolve(), {"job": "probe", "hosts": read_json(Path(args.hosts_file))})
            result = manager.get(args.host).call("probe", {"backend": args.backend})
        elif command == "demo-agent":
            from .demo import agent
            agent(args.role)
            return
        elif command == "up":
            result = start_controller(args.state)
        elif command == "demo":
            result = demo(args.directory, args.backend)
        elif command == "profile":
            if os.environ.get("TRIAD_TOKEN"):
                raise TriadError("Register profiles from the human terminal, not an agent session")
            path = Path(args.state).resolve() / "config.json"
            config = read_json(path)
            vector = read_json(Path(args.argv_file))
            if not isinstance(vector, list) or not vector or not all(isinstance(v, str) for v in vector):
                raise TriadError("Argument file must contain a nonempty string array")
            config["profiles"][args.name] = {"mode": args.mode, "argv": vector}
            atomic_json(path, config)
            result = {"profile": args.name, "mode": args.mode}
        else:
            client = Client(args.state)
            data = {k: v for k, v in vars(args).items() if k not in {"command", "state"} and v is not None}
            if command == "design":
                data = {"text": Path(args.file).read_text(encoding="utf-8-sig")}
            elif command == "create-task":
                data = read_json(Path(args.file))
            elif command == "check":
                from .runtime import run_check
                result = run_check(client, args.task, args.name)
                print(json.dumps(result, indent=2))
                if not result["valid"]:
                    raise SystemExit(1)
                return
            elif command == "inbox":
                deadline = time.monotonic() + args.timeout
                while True:
                    # Also returns a submitted but unacknowledged message whose reply was lost.
                    result = client.call("inbox", {"redeliver": True}, retries=3)
                    if result is not None or not args.wait or time.monotonic() >= deadline:
                        break
                    time.sleep(0.5)
                print(json.dumps(result, indent=2))
                return
            elif command == "attach":
                result = client.call("takeover", data)
                print(result["note"], flush=True)
                subprocess.run(result["argv"])
                return
            elif command == "down":
                client.call("stop")
                result = client.call("shutdown")
                print(json.dumps(result))
                return
            result = client.call(command.replace("-", "_"), data, retries=2)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (TriadError, OSError, ValueError) as exc:
        print(f"triad: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
