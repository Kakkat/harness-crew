# Validation

Validated on Windows with Python 3.13 and native psmux 3.3.6.

The list below predates the 2026-09-21 review fixes. After the fixes, the Windows suite was re-run (Python 3.13.14, psmux, over an SSH session): **62 tests passed**, with 5 POSIX/Linux-only tests skipped. The cooperative psmux replacement test passed, and the psmux demo was accepted with no sessions left behind.

- `python -m unittest discover -s tests -v`: **43 tests passed**, including real subprocess/psmux integration tests and SSH connection contract tests.
- Complete local-backend demo: task accepted by persistent Supervisor, with captured passing evidence; sessions and controller stopped afterward.
- Complete psmux demo: same accepted outcome with both roles hosted by psmux.
- Cross-adapter replacement: local JSONL Worker replaced by cooperative psmux Worker; unchanged Supervisor completed and accepted the job.
- Reused the same sessions for two consecutive tasks.
- Killed/restarted controller while Worker remained alive; job completed using the same Worker PID.
- Replaced Worker while preserving an existing dirty file; old credential rejected.
- Timed-out command rejected as evidence.
- Three-megabyte output stream capped to one MiB and marked truncated.
- Stale snapshots, running commands, missing evidence, later failed checks, duplicate IDs, and unauthorized actions rejected.
- Demo state/log footprint before shutdown: approximately **95 KB** per job.
- SSH connection tests: lossless remote argument quoting, strict host-key/noninteractive options, identity/jump-host configuration, explicit network failure, remote path preservation, separate role hosts, source bundle contents, and refusing replacement while the old host is unreachable.

## Linux (2026-09-21)

Validated on Ubuntu Linux with Python 3.12.3 and tmux, after the review fixes.

- `python3 -m unittest discover -s tests -v`: **62 tests passed, none skipped**. This includes the cooperative Worker test, which now runs in tmux on POSIX and psmux on Windows. All 19 new regression tests fail against the pre-fix code.
- Complete demo with `--backend local` and `--backend tmux`: task accepted, sessions and controller stopped afterward.
- Live SSH, with the controller and the SSH host on the same machine (`ssh localhost`, strict host keys, batch mode). Worker ran over SSH in remote tmux; Supervisor ran locally. The reverse tunnel carried all remote traffic. Remote evidence, `storage --remote`, and `observe` all worked, and the task was accepted.
- SSH recovery: a backend missing on the remote host was rejected before any generation was reserved. After `kill -9` on the controller and `up`, the same remote Worker process completed a second task. `down` left no tmux sessions, hosts, or tunnel helpers behind.
- Reproduced before the fix, and confirmed fixed after it: a start with an uninstalled backend wedged `start`, `replace`, `stop`, and `down`; after a host exited and its PID was reused, `start` and `down` also wedged.
- Found by the live SSH run and fixed: the remote tunnel helper outlived its SSH session, leaving one process per tunnel on the remote host.

Scope limits: the conformance harnesses are deterministic executables. Live paid AI sessions were not invoked. Optional Codex and Claude launch-profile syntax was checked using installed CLI help, but their authentication, sandbox configuration, and compliance with the cooperative protocol require live validation. SSH network operations in the unit tests are mocked. The live SSH run above used loopback SSH on one Linux machine; a Windows controller driving a separate Linux host, and tsmux, still require live testing. See SSH.md for the host-check and two-host example.

No external packages, browser runtimes, model weights, or caches were downloaded. Storage limits apply to Triad-owned state and captured logs, not to an external harness's own caches or build outputs.
