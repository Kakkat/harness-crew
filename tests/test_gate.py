from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from triad.backends import ENTRY
from triad.gate import MAX_MATERIAL_CHARS, api_key, gate, git_diff
from triad.util import TriadError


class FakeTypeSafe:
    """Local stand-in for the TypeSafe API: records requests and returns a canned reply."""

    def __init__(self):
        self.requests, self.status, self.reply = [], 200, {"model": "jev-test", "answers": {"gate": {"type": "noul", "noul": 0.1}}}
        self.responder = None  # Optional: body -> reply, for multi-question requests.
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                reply = fake.responder(body) if fake.responder else fake.reply
                payload = json.dumps(reply).encode() if isinstance(reply, dict) else reply
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def answer(self, p_yes):
        self.status, self.reply = 200, {"model": "jev-test", "answers": {"gate": {"type": "noul", "noul": p_yes}}}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class GateTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTypeSafe()
        self.env = patch.dict(os.environ, {"TYPESAFE_BASE_URL": self.fake.url, "TYPESAFE_API_KEY": "test-key"})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.env.stop()
        self.fake.close()
        self.temp.cleanup()

    def test_passes_only_with_confident_expected_answer(self):
        for p_yes, expect, passed in [(0.05, "no", True), (0.3, "no", False), (0.95, "yes", True), (0.75, "yes", False)]:
            with self.subTest(p_yes=p_yes, expect=expect):
                self.fake.answer(p_yes)
                self.assertEqual(gate("Adds a dependency?", expect, 0.8, "diff text", "test-key")[:2], (passed, p_yes))

    def test_sends_one_noul_question_with_bearer_key(self):
        gate("Adds a dependency?", "no", 0.8, "+import requests", "test-key", model="jev-preview")
        request = self.fake.requests[-1]
        self.assertEqual((request["path"], request["auth"]), ("/v1/systemone", "Bearer test-key"))
        self.assertEqual(request["body"], {"state": {"material": "+import requests"}, "model": "jev-preview",
                                           "questions": {"gate": {"type": "noul", "instructions": "Adds a dependency?"}}})

    def test_failures_fail_closed(self):
        self.fake.status, self.fake.reply = 401, {"error": "invalid key"}
        with self.assertRaisesRegex(TriadError, "HTTP 401"):
            gate("q", "no", 0.8, "material", "bad-key")
        self.fake.status, self.fake.reply = 200, b"not json"
        with self.assertRaisesRegex(TriadError, "failed"):
            gate("q", "no", 0.8, "material", "test-key")
        self.fake.reply = {"answers": {}}
        with self.assertRaisesRegex(TriadError, "Unexpected"):
            gate("q", "no", 0.8, "material", "test-key")
        sent = len(self.fake.requests)
        for material in ["  \n", "x" * (MAX_MATERIAL_CHARS + 1)]:
            with self.assertRaises(TriadError):
                gate("q", "no", 0.8, material, "test-key")
        self.assertEqual(len(self.fake.requests), sent)  # Empty or oversized material is never sent.

    def test_key_from_environment_or_file(self):
        self.assertEqual(api_key(), "test-key")
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            plain, as_json = self.root / "key", self.root / "credentials"
            plain.write_text("plain-key\n")
            as_json.write_text(json.dumps({"api_key": "json-key"}))
            self.assertEqual((api_key(plain), api_key(as_json)), ("plain-key", "json-key"))
            with self.assertRaisesRegex(TriadError, "No TypeSafe API key"):
                api_key(self.root / "missing")

    def test_diff_includes_untracked_files(self):
        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.root, check=True,
                           capture_output=True)
        git("init", "-q")
        (self.root / "app.py").write_text("print('hi')\n")
        git("add", "app.py")
        git("commit", "-qm", "base")
        (self.root / "app.py").write_text("import requests\nprint('hi')\n")
        (self.root / "requirements.txt").write_text("requests==2.32.3\n")
        cwd = os.getcwd()
        try:
            os.chdir(self.root)
            diff = git_diff("HEAD")
        finally:
            os.chdir(cwd)
        self.assertIn("+import requests", diff)
        self.assertIn("new untracked file requirements.txt\n+requests==2.32.3", diff)

    def test_cli_exit_codes(self):
        def run(p_yes=None, status=200):
            if p_yes is not None:
                self.fake.answer(p_yes)
            self.fake.status = status
            return subprocess.run([sys.executable, str(ENTRY), "gate", "Adds a dependency?", "--expect", "no",
                                   "--text", "+import requests"], capture_output=True, text=True, timeout=30)
        passed, failed = run(0.02), run(0.97)
        self.assertEqual((passed.returncode, failed.returncode), (0, 1))
        self.assertIn("gate PASS: P(yes)=2.0%", passed.stdout)
        self.assertIn("gate FAIL: P(yes)=97.0%", failed.stdout)
        self.fake.reply = {"error": "down"}
        self.assertEqual(run(status=503).returncode, 2)  # Errors fail the check too.


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTypeSafe()
        # Jev stand-in: only code that calls eval() looks like injection; everything else scores low.
        self.fake.responder = lambda body: {"model": "jev-test", "answers": {
            name: {"type": "noul", "noul": 0.95 if name == "injection" and "eval(" in body["state"]["code"] else 0.05}
            for name in body["questions"]}}
        self.env = patch.dict(os.environ, {"TYPESAFE_BASE_URL": self.fake.url, "TYPESAFE_API_KEY": "test-key"})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "calc.py").write_text("def safe(x):\n    y = x + 1\n    return y\n\n"
                                           "def risky(text):\n    value = eval(text)\n    return value\n")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "hook.sample").write_text("eval(dangerous)\n" * 3)  # Must be skipped.

    def tearDown(self):
        self.env.stop()
        self.fake.close()
        self.temp.cleanup()

    def test_review_flags_only_the_standout_function(self):
        from triad.review import digest, review
        scored, flags = review([self.root], "test-key")
        self.assertEqual(sorted(u["name"] for u in scored), ["risky", "safe"])  # .git skipped.
        self.assertEqual([(q, u["name"]) for _, q, u in flags], [("injection", "risky")])
        self.assertEqual(len(self.fake.requests), 2)  # One request per function, all questions together.
        self.assertIn("injection", digest(scored, flags))

    def test_cli_prints_digest(self):
        result = subprocess.run([sys.executable, str(ENTRY), "review", str(self.root)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIn("2 pieces x 8 questions; 1 flag(s)", result.stdout)
        self.assertRegex(result.stdout, r"95%\s+injection\s+\S+calc.py:5\s+risky")


if __name__ == "__main__":
    unittest.main()
