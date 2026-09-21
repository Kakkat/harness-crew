# Triad

A small orchestration system with a local controller and three logical roles. Sessions may run locally or on SSH-connected Linux machines:

```text
Designer (design contract / CLI)
    -> persistent Supervisor session
        -> persistent Worker session
            -> one writable repository/workspace
```

The controller has no model and makes no design decisions. Harnesses are external commands behind a protocol adapter; processes live behind a separate session backend. No AI provider, account, SDK, browser, or third-party Python package is required by the controller.

Requires Python 3.11+. Run from this directory with `python -m triad`, or use the absolute `triad_entry.py` path from any directory. Installing the package is optional.

For a Supervisor and Worker on different PCs, see [SSH host setup](SSH.md). Host connections, multiplexers, and harness profiles are separate choices.

## Run a complete example

```powershell
python -m triad demo --directory C:/temp/triad-example --backend psmux
```

Use a **new directory**. On Linux use `--backend tmux`; `--backend local` needs no multiplexer. The demo runs two deterministic conformance harnesses, creates a file, executes a check, obtains Supervisor acceptance, and stops its sessions and controller. It is a real orchestration test, **not an AI coding demonstration**. Results and bounded evidence stay in the requested directory.

## Use real coding harnesses

1. Choose a dedicated checkout or worktree. Triad never clones, resets, or cleans it automatically.
2. Initialize a state directory **outside** that checkout.
3. Register a harness command for each role.
4. Submit a design and task with executable acceptance checks.
5. Start the Supervisor and Worker.

```powershell
python -m triad --state C:/work/triad-state init --workspace C:/work/my-checkout --backend psmux
python -m triad --state C:/work/triad-state up
python -m triad --state C:/work/triad-state profile supervisor-cli --mode cooperative --argv-file examples/codex.argv.json
python -m triad --state C:/work/triad-state profile worker-cli --mode cooperative --argv-file examples/claude.argv.json
python -m triad --state C:/work/triad-state design --file examples/design.md
python -m triad --state C:/work/triad-state create-task --file examples/task.json
python -m triad --state C:/work/triad-state start worker --profile worker-cli
python -m triad --state C:/work/triad-state start supervisor --profile supervisor-cli
python -m triad --state C:/work/triad-state status
```

The vendor-named files are **optional launch configurations**, not controller dependencies. Either role can use either file or an entirely different command. Their positional prompt syntax was checked against the installed CLIs' help; live AI behavior and authentication were not exercised in automated tests. Resolve executable paths explicitly if your multiplexer has a different PATH.

Profiles are JSON argument arrays, never shell command strings. Supported substitutions are `{bootstrap}` and `{workspace}`. The generated bootstrap contains the role, current handoff, CLI path, and mailbox procedure. A typical interactive command accepts a prompt telling it to read that file. If your harness has a different interface, adjust the profile or write a structured bridge.

Harness-native workspace trust, shell/network permissions, and login prompts still apply. Observe the session for setup prompts and resolve them using the harness's normal controls. Triad does not disable permission checks or invent credentials. Some harness sandboxes must explicitly permit the local controller and the state/evidence directory. Native background-daemon modes that escape the owned process tree must not be enabled.

For a real project, replace the example design, task, and checks with its actual requirements. The sample `task.json` uses `python` on PATH; an absolute interpreter path makes checks more reproducible.

## Control and observation

```text
triad --state PATH status
triad --state PATH events --after 100
triad --state PATH storage
triad --state PATH observe worker
triad --state PATH pause
triad --state PATH attach worker
triad --state PATH resume
triad --state PATH replace worker --profile another-profile
triad --state PATH replace supervisor --profile supervisor-cli
triad --state PATH stop
triad --state PATH down
```

Use `python -m triad` in place of `triad` without installation.

- `observe` is read-only: terminal snapshot or bounded captured output.
- `attach` is interactive takeover: requires a ready role with no unresolved checks; pauses dispatch before launching the multiplexer client. Resume explicitly after reconciling human edits.
- `pause` stops new Worker dispatch. Already running bounded work may continue; Supervisor can still receive feedback. Interactive takeover also pauses the selected role's inbox.
- `interrupt worker` stops that generation and its ordinary descendants. Portable v1 interruption is termination, not a vendor-specific soft cancel.
- `replace` confirms termination, preserves files, writes a handoff, revokes old credentials, and creates a new generation. Blocked tasks require an explicit new assignment.
- `stop` stops both sessions but keeps the controller available for inspection.
- `down` stops sessions and then exits the controller.
- Closing a terminal or killing only the controller leaves persistent sessions alive. Run `up` again to reconnect. A reboot loses native processes but preserves task state.

Only the Supervisor normally assigns and accepts tasks. The human/Designer credential also has recovery controls. Worker credentials cannot accept tasks, start sessions, or issue Supervisor decisions. Credentials protect against routing mistakes, not a malicious process under the same OS user.

## Harness adapter contracts

### Cooperative adapter

An existing CLI harness reads a bootstrap and uses shell tools to call Triad. It receives these environment variables: `TRIAD_STATE`, `TRIAD_TOKEN`, `TRIAD_ROLE`, `TRIAD_GENERATION`.

Its loop is:

```text
ready
inbox --wait
ack --message MESSAGE_ID
perform role work
ready
inbox --wait
...
```

`inbox --wait` is a blocking local poll; it makes no model calls. A Worker performs `check --task TASK_ID --name CHECK_NAME`, fixes failures, and finally calls:

```text
result --task TASK_ID --summary "What changed" --evidence RUN_ID [RUN_ID ...]
```

It may instead call `blocked --task TASK_ID --reason "Specific blocker"`. Supervisor uses `assign`, `correct`, `accept`, `replace`, and `escalate`. `--help` documents arguments.

`ready` is an explicit promise that the preceding work is quiescent. This adapter trusts cooperative protocol behavior; it cannot prove an arbitrary terminal agent has stopped reasoning or launching undeclared commands. It does not screen-scrape prompts or equate silence with completion. No subsequent prompt is injected into a running terminal command: messages are retrieved by the agent itself.

### Structured JSONL adapter

Use a persistent executable with JSON Lines on stdin/stdout. Put diagnostics on stderr. Maximum protocol record: 64 KiB.

Harness -> host:

```json
{"type":"action","id":"ready-1","action":"ready","data":{}}
```

Host -> harness:

```json
{"type":"action_result","id":"ready-1","ok":true,"result":{"ready":true}}
{"type":"message","message":{"v":1,"id":"m-...","seq":12,"to":"worker","generation":1,"type":"assign","body":{"id":"task-..."}}}
```

The harness explicitly acknowledges each message, uses role-authorized actions, and emits `ready` after handling it. All CLI actions map to RPC action names with underscores. Errors return `ok:false` and `error`. Native SDK/CLI streams require an adapter bridge that translates their events into this protocol; arbitrary vendor JSON is not automatically compatible.

`triad/adapters.py` owns harness framing/capabilities. `triad/backends.py` owns local/psmux/tmux/tsmux process transport. No provider-specific branch exists in the controller. The demo harness is a small reference implementation of the role behavior; `examples/structured_echo.py` illustrates action framing.

## Recovery and correctness

- SQLite atomically records task changes and outgoing messages. The controller is its only writer.
- Requests carry idempotency IDs. An identical retry returns the saved result; changed content under the same ID is rejected.
- Submitted messages are not blindly replayed. If a client loses a reply, retry the same request ID. A crashed host with uncertain prompt delivery requires replacement/reconciliation.
- Startup reserves a deterministic generation before spawning. If startup is incomplete, inspect that generation; do not start a competing writer.
- Every session has a generation-scoped credential. Replaced Workers cannot submit valid late results.
- Windows hosts use a Job Object; POSIX hosts use a process group. Only ordinary contained descendants are supported. Do not daemonize or escape the group.
- A workspace lock plus durable owner reference prevents a second state directory from taking over a workspace with unreconciled sessions.
- A handoff stores design, tasks, checks, runs, and the current content fingerprint. Native model context is optional and not yet resumed automatically.
- Task deadlines default to one hour; named check deadlines default to five minutes. A task deadline pauses the job and attempts to stop its Worker. A five-minute progress gap creates a review event, not a success/failure judgment.

No automatic implementation retry policy is hidden in the controller. Supervisor decides retry, correction, replacement, or escalation, within the task/run/session budgets.

## Acceptance and evidence

A Worker result is only a candidate. Acceptance requires:

1. Worker reached the explicit ready boundary and has no unresolved captured commands.
2. Every declared check has a current passing result from this attempt.
3. A later failing check is not concealed by an earlier pass.
4. Workspace contents match the tested result.
5. No reported open issues remain.
6. Supervisor explicitly accepts.

Fingerprints include untracked files and symlink targets, without following symlinks. `.git` and `__pycache__` are excluded. `config.json` allows additional excluded directory names; configure them narrowly. Large dependency trees can make fingerprinting slow. Verification commands that intentionally alter tracked/source files invalidate their own check: run generators/build preparation first, then use stable checks.

Evidence contains exact command arguments, cwd, timestamps, exit code, timeout/truncation flags, fingerprints, and bounded stdout/stderr. Toolchain/environment immutability is not enforced in v1; use pinned dependencies and a controlled environment for stronger reproducibility. Worker reports and local artifacts are not resistant to deliberate tampering by the same OS account.

## Storage and token limits

No packages, browser binaries, models, or runtime bundles are downloaded by Triad.

Defaults:

| Item | Limit |
|---|---:|
| Each captured stdout/stderr/session log | 1 MiB |
| SQLite database | 16 MiB |
| State-directory admission budget | 128 MiB |
| Sessions/generations per job | 32 |
| Verification runs per job | 64 |
| Protocol/request body | 64 KiB |
| Pending structured event buffer | 256 records |

Logs continue to drain after the cap; truncation is explicit. Empty inbox polls and heartbeats do not append message receipts. Supervisor sees structured events and retrieves relevant evidence, not full terminal transcripts.

The 128 MiB check gates new sessions and verification runs with a reservation allowance; it is not an OS filesystem quota. Small control records and already admitted processes can consume additional space. SQLite and each log have independent hard caps. Budgets can be changed deliberately in `config.json`; restart the controller after changes to global settings.

These caps cover **Triad-owned files only**. AI harness history, model downloads, npm/pip caches, build artifacts, browser data, and the Windows pagefile are outside Triad's storage budget. This distinction matters when diagnosing a 10 GB drop in SSD free space. `storage` reports Triad's own footprint without deleting anything.

## Tests and current scope

```powershell
python -m unittest discover -s tests -v
```

The automated tests cover authorization, stale generations, request deduplication, ambiguous delivery, persistence, exclusive ownership, changed source evidence, running/failed checks, bounded logs, controller crash/reconnect, reusable sessions, takeover, Worker replacement, command timeouts, and SSH host routing. They include switching a live job from a structured Worker to a cooperative Worker in native Windows psmux without changing Supervisor logic. The complete two-session demo has also been exercised against psmux.

V1 has one controller / one job / one Worker, with independent local or SSH host selection for Supervisor and Worker. Linux process paths, SSH tunnels, and the tmux/tsmux backend are implemented but need live platform acceptance testing on those hosts. Multiple Workers, automatic Git integration or repository migration, adversarial sandboxing, native provider context resume, and provider billing telemetry are intentionally not claimed.

The implementation is small enough to inspect manually: `core.py` owns decisions and state transitions, `store.py` persistence, `server.py` local transport, `runtime.py` session hosts/check execution, and `cli.py` the user interface.
