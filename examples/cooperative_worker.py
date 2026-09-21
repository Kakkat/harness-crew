"""Non-AI conformance worker using the same mailbox API as an interactive harness."""
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from triad.client import Client
from triad.runtime import run_check
from triad.util import TriadError

client = Client()
client.call("ready", retries=3)
while True:
    try:
        message = client.call("inbox", retries=3)
    except TriadError:
        time.sleep(0.5)
        continue
    if message is None:
        time.sleep(0.25)
        continue
    client.call("ack", {"message": message["id"]})
    if message["type"] in {"assign", "correct"}:
        task = message["body"] if message["type"] == "assign" else message["body"]["task"]
        Path("greeting.txt").write_text("hello triad\n", encoding="utf-8")
        runs = [run_check(client, task["id"], c["name"]) for c in task["checks"]]
        if all(r["valid"] for r in runs):
            client.call("result", {"task": task["id"], "summary": "Cooperative worker completed greeting.",
                                   "evidence": [r["id"] for r in runs]})
        else:
            client.call("blocked", {"task": task["id"], "reason": "Check failed"})
    client.call("ready")
