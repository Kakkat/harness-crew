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

## Review of 6a03d86 (Linux, 2026-09-21)

Four findings from the review of `6a03d86` were fixed and validated on Ubuntu Linux with Python 3.12.3, setuptools 68.1.2, and tmux. Regressions are in `tests/test_review_regressions.py`. All six fail against `6a03d86`; that run used a copy with only the two test-imported constants added to `store.py`.

- Startup authorization: a host waits for an affirmative controller reply before spawning. Test: a revoked Worker's delayed host started during a controller outage waits without spawning. Once the controller returns, the host exits on `revoked` and the marker is never created. Before the fix, the marker was recreated during the outage.
- Check quiescence: checks run under a `contain-check` runner that exits only when its containment boundary is empty. Two tests use a check child that redirects all streams to DEVNULL and calls `setsid()`. When that child lingers for 2 seconds, the run finishes only after the child's final write and is then accepted. When it lingers for 120 seconds with a 3-second timeout, the run times out, is invalid, and the child is dead. Before the fix, the first task was accepted while the child was alive, and the second task was accepted instead of timing out.
- Stop with a full database: one test fills the live controller's real database to the 16 MiB hard cap, including the event log's last page. `stop` then returns `database or disk is full`, and both live hosts (Worker and Supervisor) are confirmed terminated by process identity. `status` shows `reconcile.json`. After the filler is dropped, `stop` records both sessions, logs `reconciled`, and clears the file. A second test fills to the ordinary 15 MiB limit: `create_task` is refused, while `stop` and `shutdown` succeed from reserved capacity. Before the fix, neither host was stopped.
- Packaging: a wheel is built from a temporary source copy with `setuptools.build_meta` (no network), then extracted into a `venv --without-pip`. Run with `python -I` from outside the checkout: `triad_entry.py` is inside site-packages, `runtime_bundle()` contains it, and `python -m triad demo --backend local` is accepted with both hosts stopped. Before the fix, `triad_entry.py` was missing from the wheel.
- `python3 -m unittest discover -s tests`: **68 tests passed, none skipped** (72.6 s), run with an enclosing Triad session's `TRIAD_*` variables present. Test modules now scrub those variables. Before this change, inherited variables caused 10 integration failures and left controllers running when `setUp` failed; each fixture controller now has a cleanup hook.
- Demo from the checkout with `--backend local` and `--backend tmux`: both tasks accepted; no demo tmux sessions or processes remained.

Limits of this round: the macOS/BSD process-group fallback was not executed. On non-Linux POSIX, a check descendant that leaves the process group is not observed. Checks that intentionally leave a background process running now time out instead of passing.

## Designer validation of the review fixes (2026-09-21)

Run from clean exports of `eb9840d`, after the repair task was accepted through Triad.

- Windows, Python 3.13.14 with psmux: **68 tests passed**, with 5 POSIX/Linux-only tests skipped. All six regressions passed. That includes both check-containment tests, which run the Windows Job Object active-process count, and the installed-wheel test.
- Linux: 68 tests passed. The `--backend local` and `--backend tmux` demos were accepted.
- Live SSH over loopback on Linux: the remote tmux Worker's task was accepted. A backend missing on the remote host was rejected before a generation was reserved. After `kill -9` on the controller and `up`, the same remote Worker completed a second task. No sessions, hosts or tunnel helpers remained.
- The repair itself went through Triad. The Worker was Claude Opus (high effort) on Linux. The Supervisor was Claude Opus (low effort), after the Codex Supervisor hit its usage limit. It was accepted on attempt 2 with fresh evidence: `regressions` (6 passed) and `full-suite` (68 passed).

## Follow-up: reconciliation fence (Linux, 2026-09-21)

Validated on Ubuntu Linux with Python 3.12.3. Gap fixed: when `stop` terminated sessions but could not record it (SQLite hard-full), `reconcile.json` was written, but the stale `sessions` rows still authenticated. The old Worker credential was accepted both before and after a controller restart.

- `stop` now fences first. It adds each targeted generation (`role-gN`) and `job` to an in-memory fence and writes the fence to `reconcile.json` (`fenced`) before any termination. After that it attempts every process stop, whether or not database writes succeed. `Core` reloads the fence at startup. An unreadable record, or one without `fenced`, fences every session.
- While the fence is nonempty, fenced credentials are refused as revoked, so a delayed host fails its startup heartbeat. `start`, `replace`, `resume`, `assign`, `correct`, `run_start` and `takeover` are also refused. The Designer keeps `status`, `events`, `task`, `storage`, `evidence`, `stop` and `shutdown`. Only a `stop` whose writes all commit clears the fence and removes the file. Explicit `resume` and `start` then work normally.
- `interrupt`, `replace`, and the `start` that stops a failed generation all use `stop_role`, which follows the same rule. It fences the generation durably before terminating. If the termination succeeds but `record_stopped` cannot be written, the credential stays revoked, including after a restart, until `stop` reconciles. A recorded stop lifts that generation's fence. If termination fails (host unreachable), nothing was stopped and the fence is rolled back, as before.
- A session whose stop is unconfirmed (host unreachable) stays fenced across restarts. Each retried `stop` attempts it again. A deadline stop that cannot be recorded, or is unconfirmed, fences the expired Worker generation; the Supervisor keeps its credential.
- New regressions in `tests/test_review_regressions.py`. Seven of them fail against `eb9840d`. The eighth (a recorded interrupt lifts its fence and leaves no false revocation) guards against over-fencing, and so passes there too:
  - Unit, using the design's reproduction (persist collects errors without writing; transport stop succeeds): stale Worker and Supervisor credentials are refused before and after `Core(root)` is reopened, while Designer reads still work. `start`, `replace`, `resume`, `assign`, `correct` and `takeover` are refused before and after restart, with no generation reserved. A retried stop reconciles; `resume` and `start` then create generation 2 with a working credential. An unreachable Worker stays fenced until its stop is confirmed. A deadline stop that cannot be recorded fences only the Worker. An `interrupt` or `replace` whose stop cannot be recorded revokes the old Worker credential before and after restart; the Supervisor keeps its own, and `start` is refused with no generation reserved. This test also fails against the first follow-up commit `f8eca93`.
  - Integration, with a real database filled to 16 MiB: `stop` fails and both hosts are terminated. Each old credential's `heartbeat` is refused as revoked, and `start`/`resume` are refused, both before and after killing and relaunching the controller. After the filler is dropped, `stop` reconciles (a `reconciled` event, `reconcile.json` removed). The old Worker credential is still refused. `resume` and `start` bring up Worker generation 2.
- `python3 -B -m unittest discover -s tests`: **76 tests passed, none skipped** (82.1 s, local run before commit). The named `full-suite` check was run through the role CLI after committing; see the task result for its run ID.

Limits: the fence depends on writing `reconcile.json`. If the filesystem itself is full, so that this write also fails, the fence survives only in memory until the controller restarts. This round was not re-run on Windows.

Scope limits: the conformance harnesses are deterministic executables. Live paid AI sessions were not invoked. Optional Codex and Claude launch-profile syntax was checked using installed CLI help, but their authentication, sandbox configuration, and compliance with the cooperative protocol require live validation. SSH network operations in the unit tests are mocked. The live SSH run above used loopback SSH on one Linux machine; a Windows controller driving a separate Linux host, and tsmux, still require live testing. See SSH.md for the host-check and two-host example.

No external packages, browser runtimes, model weights, or caches were downloaded. Storage limits apply to Triad-owned state and captured logs, not to an external harness's own caches or build outputs.

## Optional Jev review gate (2026-09-21)

`triad gate` asks TypeSafe's Jev one yes/no question and passes only on a confident expected answer. It uses the standard library only; the controller makes no AI calls.

- Tests against a local fake TypeSafe server, never the real API: the threshold, the exact request and Bearer header, failing closed on HTTP errors, malformed replies, and empty or oversized input, key lookup, untracked files in `--diff`, CLI exit codes, and `{entry}` expansion in check commands. **75 tests passed** on Linux (Python 3.12). On Windows (Python 3.13), 75 passed with 5 POSIX-only tests skipped.
- Live, with the real Jev (`jev-1.13.0`), in two harness jobs using the scripted demo agents and `examples/jev-gated-task.json` unchanged:
  - Clean change: `greeting` passed; the gate passed with `P(yes)=2.0%`; the task was **accepted**.
  - A change that adds `import requests` and a `requirements.txt`: `greeting` passed; the gate failed with `P(yes)=99.0%`; the task was **blocked** and could not be accepted.

## Supervisor triage, doorbell and harness-run Jev review (2026-09-21)

**Tests:** 92 passed on Linux. On Windows, 92 passed with 7 POSIX/tmux-only skips. Live checks: the doorbell typed its notice verbatim into a tmux pane and a psmux pane, including literal key names such as `Enter`, and submitted it.

**Live runs.** The calculator example (`evaluate()` plus a CLI) was run with an interactive Haiku (max effort) Worker and an Opus (low effort) Supervisor in tmux, with no human intervention. There is one run per variant, so these results are indicative, not statistical. Quality means a hidden 10-point test set (Unicode digits, deep nesting, CLI tracebacks, plus the acceptance suite) that the agents never saw.

| | Old harness | A: Supervisor reads code | B3: Supervisor reads only the Jev digest | **C: default (reads code, digest as hints)** |
|---|---|---|---|---|
| Minutes until both tasks accepted | ~16 (10-min stall) | 4.8 | 3.1 | 5.3 |
| Supervisor wakes | 8 | 4 | 4 | 5 |
| Model calls after completion | 10 (idle polling) | 0 | 0 | 0 |
| Opus calls | 27 | 17 | 12 | 20 |
| Opus cached input / output tokens | 1.38M / 4.3K | 0.80M / 4.0K | 0.53M / 1.6K | 1.01M / 4.5K |
| Corrections | 1 (by hand) | 1 | 0 | 1 (four fixes) |
| Hidden quality | 4/10 | 10/10 | 6/10 | 10/10 |

Findings:
- The harness changes (triage, doorbell, exact commands in the bootstraps) are the main gain: no stall, half the Supervisor wakes, zero idle model calls, and a 3–5× shorter run.
- A digest-only Supervisor (B3) was the cheapest, but approved code with robustness bugs. Jev flags point at where to look; they are not a review.
- Live runs exposed three defects that were fixed before this record. Two were deadlocks: an agent that forgot `ready` was never rung, and a Worker became ready before the Supervisor existed. The third was a review prompt that swept in `.git`. `result`, `blocked` and `inbox` now end a turn, and a new Supervisor is told about a ready Worker.
