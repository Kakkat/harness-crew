"""Optional Jev review gate for acceptance checks. Standard library only.

A task can declare a check that asks TypeSafe's Jev model one yes/no question about the
Worker's changes; the check passes only when Jev is confident in the expected answer. The
controller never calls Jev itself: this is an ordinary check command, so its output becomes
bounded evidence like any other.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

from .util import TriadError

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
DEFAULT_KEY_FILE = Path.home() / ".config" / "typesafe" / "api_key"
# Jev's input limit is about 64K tokens; larger material fails the gate instead of being truncated.
MAX_MATERIAL_CHARS = 200_000


def api_key(key_file=None):
    """TYPESAFE_API_KEY, else a key file: the plain key, or JSON with an "api_key" field."""
    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"].strip()
    path = Path(key_file or os.environ.get("TYPESAFE_API_KEY_FILE") or DEFAULT_KEY_FILE).expanduser()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise TriadError(f"No TypeSafe API key: set TYPESAFE_API_KEY or create {path}") from None
    if text.startswith("{"):
        try:
            return json.loads(text)["api_key"]
        except (ValueError, KeyError):
            raise TriadError(f"{path} is JSON without an api_key field") from None
    return text


def git_diff(base):
    """Changes since base, including untracked files, which `git diff` alone omits."""
    def git(*args):
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True, check=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise TriadError(f"git {args[0]} failed: {getattr(exc, 'stderr', '') or exc}") from exc
    parts = [git("diff", base, "--")]
    for name in git("ls-files", "--others", "--exclude-standard").splitlines():
        try:
            content = Path(name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            content = "(binary or unreadable file)"
        parts.append(f"new untracked file {name}\n" + "".join(f"+{line}\n" for line in content.splitlines()))
    return "".join(parts)


def ask_many(questions, state, key, model=None, timeout=30):
    """Ask several yes/no questions about one state; return ({name: P(yes)}, model)."""
    base = os.environ.get("TYPESAFE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    body = json.dumps({"state": state, "model": model or os.environ.get("TYPESAFE_DEFAULT_MODEL", DEFAULT_MODEL),
                       "questions": {name: {"type": "noul", "instructions": text} for name, text in questions.items()}}).encode()
    request = urllib.request.Request(base + "/v1/systemone", data=body, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "harness-crew"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as exc:
        raise TriadError(f"Jev request failed: HTTP {exc.code} {exc.read()[:200].decode('utf-8', 'replace')}") from exc
    except (OSError, ValueError) as exc:
        raise TriadError(f"Jev request failed: {exc}") from exc
    try:
        return {name: float(answer["answers"][name]["noul"]) for name in questions}, answer.get("model", "?")
    except (KeyError, TypeError, ValueError):
        raise TriadError(f"Unexpected Jev response: {json.dumps(answer)[:200]}") from None


def ask(question, material, key, model=None, timeout=30):
    """Return Jev's probability that the answer to a yes/no question is yes."""
    scores, used = ask_many({"gate": question}, {"material": material}, key, model, timeout)
    return scores["gate"], used


def gate(question, expect, minimum, material, key, model=None):
    """Pass only when Jev gives the expected answer with at least `minimum` confidence."""
    if not material.strip():
        raise TriadError("Nothing to judge: the material is empty")
    if len(material) > MAX_MATERIAL_CHARS:
        raise TriadError(f"Material is {len(material)} characters; the gate limit is {MAX_MATERIAL_CHARS}. Narrow it")
    p_yes, used = ask(question, material, key, model)
    confidence = p_yes if expect == "yes" else 1 - p_yes
    return confidence >= minimum, p_yes, used
