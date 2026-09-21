"""Deterministic conformance harness, not an AI model."""
import json
import sys
from pathlib import Path

from .client import Client
from .runtime import run_check
from .util import TriadError, uid


def emit(action, data=None):
    print(json.dumps({"type": "action", "id": uid(), "action": action, "data": data or {}}), flush=True)


def agent(role):
    client = Client()
    emit("ready")
    for line in sys.stdin:
        envelope = json.loads(line)
        if envelope["type"] != "message":
            continue
        message = envelope["message"]
        client.call("ack", {"message": message["id"]}, retries=3)
        try:
            if role == "worker" and message["type"] in {"assign", "correct"}:
                task = message["body"] if message["type"] == "assign" else message["body"]["task"]
                Path("greeting.txt").write_text("hello triad\n", encoding="utf-8")
                evidence = [run_check(client, task["id"], c["name"]) for c in task["checks"]]
                if all(r["valid"] for r in evidence):
                    client.call("result", {"task": task["id"], "summary": "Greeting implemented and checked.",
                                           "evidence": [r["id"] for r in evidence]})
                else:
                    client.call("blocked", {"task": task["id"], "reason": "Demo check failed"})
            elif role == "supervisor":
                status = client.call("status")
                for task in status["tasks"]:
                    if task["state"] == "awaiting_verification":
                        try:
                            client.call("accept", {"task": task["id"]})
                        except TriadError:
                            pass  # Worker-ready event will cause the next acceptance attempt.
                status = client.call("status")
                if not any(t["state"] in {"assigned", "working", "awaiting_verification"} for t in status["tasks"]):
                    queued = next((t for t in status["tasks"] if t["state"] == "queued"), None)
                    if queued:
                        try:
                            client.call("assign", {"task": queued["id"]})
                        except TriadError:
                            pass  # Wait for Worker startup/readiness.
        finally:
            emit("ready")
