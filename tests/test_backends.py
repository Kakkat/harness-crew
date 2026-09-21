import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from triad.backends import stop_host, stop_session
from triad.util import atomic_json


class StopHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def host(self, pid, identity, contained=True):
        atomic_json(self.root / "host.json", {"pid": pid, "identity": identity,
                                              "contained": contained, "exited": True})

    def test_session_without_host_identity_never_spawned(self):
        stop_host(self.root)
        with patch("triad.backends.shutil.which", return_value=None):
            stop_session("tmux", str(self.root / "spec.json"))  # Multiplexer since uninstalled.

    def test_recycled_pid_is_not_signalled(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.host(unrelated.pid, "an-earlier-process")
            stop_host(self.root)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_exited_host_that_never_confirmed_containment_is_stopped(self):
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait()
        self.host(exited.pid, "gone", contained=False)
        stop_host(self.root)


if __name__ == "__main__":
    unittest.main()
