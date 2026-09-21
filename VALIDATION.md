# Validation

Validated on Windows with Python 3.13 and native psmux 3.3.6.

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

Scope limits: the conformance harnesses are deterministic executables. Live paid AI sessions were not invoked. Optional Codex and Claude launch-profile syntax was checked using installed CLI help, but their authentication, sandbox configuration, and compliance with the cooperative protocol require live validation. SSH network operations in the new tests are mocked; no SSH/Linux destination was supplied or configured on this PC. Reverse tunnels and POSIX/tmux/tsmux paths require live testing on the selected hosts. See SSH.md for the host-check and two-host example.

No external packages, browser runtimes, model weights, or caches were downloaded. Storage limits apply to Triad-owned state and captured logs, not to an external harness's own caches or build outputs.
