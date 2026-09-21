"""Harness protocol adapters. Session backends never import this module."""
from dataclasses import dataclass
import json
from typing import Protocol

from .util import TriadError


@dataclass(frozen=True)
class Capabilities:
    structured_stdio: bool
    needs_terminal: bool
    completion: str
    native_context_resume: bool = False


class HarnessAdapter(Protocol):
    capabilities: Capabilities

    def decode(self, record: bytes) -> dict: ...
    def encode_message(self, message: dict) -> bytes: ...


class JsonlAdapter:
    capabilities = Capabilities(True, False, "explicit-ready-event")

    def decode(self, record):
        if len(record) > 65536 or not record.endswith(b"\n"):
            raise TriadError("Unterminated or oversized JSONL record")
        event = json.loads(record)
        if not isinstance(event, dict) or event.get("type") != "action":
            raise TriadError("JSONL stdout requires type=action; use stderr for diagnostics")
        if not isinstance(event.get("action"), str) or not isinstance(event.get("data", {}), dict):
            raise TriadError("Invalid harness action")
        return event

    def encode_message(self, message):
        return (json.dumps(message) + "\n").encode()


class CooperativeAdapter:
    # The harness calls inbox/ack/ready through the CLI. No terminal screen scraping.
    capabilities = Capabilities(False, True, "cooperative-mailbox-boundary")

    def decode(self, record):
        raise TriadError("Terminal output is not a control protocol")

    def encode_message(self, message):
        raise TriadError("Cooperative prompts are retrieved from the durable inbox")


def adapter(mode) -> HarnessAdapter:
    if mode == "jsonl":
        return JsonlAdapter()
    if mode == "cooperative":
        return CooperativeAdapter()
    raise TriadError(f"Unsupported harness protocol: {mode}")
