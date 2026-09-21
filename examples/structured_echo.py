"""Minimal structured protocol fixture. A real bridge delegates to its own harness."""
import json
import sys
import uuid


def action(name, data=None):
    print(json.dumps({"type": "action", "id": uuid.uuid4().hex,
                      "action": name, "data": data or {}}), flush=True)


action("ready")
for line in sys.stdin:
    value = json.loads(line)
    if value.get("type") == "message":
        message = value["message"]
        action("ack", {"message": message["id"]})
        print(f"Received {message['type']} ({message['id']})", file=sys.stderr, flush=True)
        action("ready")
