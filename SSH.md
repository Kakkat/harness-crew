# Sessions on SSH-connected Linux hosts

The execution boundary is now:

```text
Role -> Session service -> Host connection -> Session backend -> Harness adapter
                           local / SSH        local / tmux       any profile
                                              psmux / tsmux
```

Designer and the controller can stay on Windows while Supervisor and Worker run on different Linux machines. Supervisor may also run locally. Each role selects its own `--host`, `--backend`, and `--profile`.

The controller remains the single durable authority. It does not open the remote repository through a Windows path. Fingerprints and checks execute on the canonical workspace host; evidence remains there and can be fetched through the same CLI.

## Configure hosts

Copy `examples/hosts.json` and set actual SSH destinations, Linux home directories, and existing workspaces. A minimal configuration is:

```json
{
  "builder": {
    "kind": "ssh",
    "target": "user@linux-pc",
    "root": "/home/user/.triad",
    "python": "python3"
  },
  "reviewer": {
    "kind": "ssh",
    "target": "another-ssh-alias",
    "root": "/home/user/.triad",
    "workspace": "/home/user/review-checkout",
    "python": "python3"
  }
}
```

`target` may be an SSH config alias or `user@host`. Optional fields are `identity` (key filename on the controller), `port`, `config_file`, `jump` (ProxyJump destination), `ssh` (local OpenSSH executable), and `reverse_port` (fixed remote loopback port). Without `reverse_port`, Triad selects a free unprivileged port and persists it for reconnection. Distinct jobs need distinct reverse ports.

Requirements:

- OpenSSH client on the controller.
- Working noninteractive SSH authentication and an already trusted host key. Triad keeps strict host-key checking enabled.
- Python 3.11+ and the selected multiplexer installed on each Linux host.
- Existing repository/working directories and installed, authenticated harness commands on their selected hosts.
- SSH server permits remote TCP forwarding. Triad requests a loopback-only reverse listener; no public HTTP listener is needed.
- Other local accounts on each Linux host are trusted. The reverse listener is a loopback TCP port, so while a tunnel is down another local account could bind that port and receive remote sessions' requests, including their session credentials.
- Do not configure the SSH server to force remote forwards to public interfaces. Triad's control API still requires role credentials, but its intended network boundary is loopback plus SSH.

The remote `root` must be an absolute dedicated directory, not `~`, `/`, or a relative path. SSH authentication is delegated to OpenSSH/ssh-agent/config; no passwords or private key contents are stored in Triad's database.

## Check a host, then start a job

The host check uploads only Triad's small Python source bundle and reports Python/platform/multiplexer availability. It does not start an AI or install dependencies.

```powershell
python -m triad host-check --hosts-file hosts.json --host builder --backend tmux

python -m triad --state C:/work/remote-job init --hosts-file hosts.json --workspace-host builder --workspace /home/user/project --backend tmux
python -m triad --state C:/work/remote-job up
python -m triad --state C:/work/remote-job design --file examples/design.md
python -m triad --state C:/work/remote-job create-task --file examples/remote-task.json

python -m triad --state C:/work/remote-job start worker --host builder --backend tmux --profile demo-worker
python -m triad --state C:/work/remote-job start supervisor --host reviewer --workspace /home/user/review-checkout --backend tmux --profile demo-supervisor
```

These demo profiles are deterministic conformance executables. Use a dedicated test repository for the greeting task. To use AI coding harnesses, register cooperative or JSONL profiles as described in `README.md`, then change only `--profile`.

Portable profiles can use `{python}`, `{entry}`, `{bootstrap}`, and `{workspace}`. These expand on the selected execution host. Do not put Windows executable paths into a Linux launch profile. Acceptance checks may use `{python}` to select the Worker's Python executable.

For a local Supervisor and remote Worker:

```powershell
python -m triad --state C:/work/remote-job start supervisor --host local --workspace C:/work/review-checkout --backend psmux --profile supervisor-cli
```

The Supervisor workspace is a separate existing directory. Triad does not silently copy or synchronize repositories between hosts. The Supervisor can review task metadata and fetch remote evidence through the controller.

## I/O, connection recovery and shutdown

Each SSH host gets a reverse tunnel from a remote loopback port to the controller's loopback HTTP port. Remote sessions use their own role credentials and the unchanged mailbox protocol through that tunnel. The tunnel process performs no reasoning. OpenSSH provides command execution, forwarding, keepalives, and forwarding-failure detection. [SSH manual](https://man.openbsd.org/ssh.1), [SSH configuration manual](https://man.openbsd.org/ssh_config.5).

Remote session hosts run inside the chosen multiplexer, independently of the SSH channel used to create them. Disconnecting SSH does not terminate a tmux session. The current bounded task may continue; new control requests wait/retry according to the existing delivery protocol.

Triad reconnects failed tunnels with bounded delays. The controller reuses its saved local port after restart and can adopt a still-running owned tunnel. Process identity checks guard against confusing a recycled PID with an owned SSH process. Tunnel maintenance runs separately from the request handler.

An unreachable host yields `unknown`/connection errors, never a fabricated `exited` result. Replacement and shutdown require a successful remote stop and confirmation that the owned process group has stopped. If the network is unavailable, the old Worker is not replaced with another writer. Normal descendant processes remain in the session's process group. The Linux session host is also a child subreaper, so descendants that call `setsid()` stay in its tree and are stopped with it. Escaping through another service manager is unsupported.

The Worker session host holds a filesystem lock on its Linux workspace. The lock stays held across controller/SSH disconnections. Supervisor can move to another configured host through `replace supervisor --host ... --workspace ...`; its new generation receives the durable handoff. Worker replacement can change harness/multiplexer on its canonical host. Moving a repository to a different host is a separate explicit migration, not an automatic side effect of replacing an agent.

Host routing is pinned in each session record. Restore the original host definition before controlling an existing session if configuration was changed. Restart the controller after editing global host settings. Register all intended hosts before initializing a job.

```powershell
python -m triad --state C:/work/remote-job status
python -m triad --state C:/work/remote-job observe worker
python -m triad --state C:/work/remote-job evidence --run RUN_ID --file stderr.log
python -m triad --state C:/work/remote-job storage --remote
python -m triad --state C:/work/remote-job replace supervisor --host builder --workspace /home/user/project --profile supervisor-cli
python -m triad --state C:/work/remote-job down
```

`attach worker` still requires a safe takeover boundary, pauses dispatch, and opens `ssh -tt` followed by the remote multiplexer attachment command. `observe` remains read-only. Interactive native login/trust setup can also be done directly on the named multiplexer session; reconcile state before resuming automation.

## Files and storage

On the controller: SQLite, routing metadata, handoff/spec copies, and the SSH tunnel's process identity.

On each Linux host:

```text
/home/user/.triad/
  runtime/<source-hash>/    # Python source only; no package installation
  jobs/<job-id>/
    endpoint.json
    sessions/<role>-gN/
    evidence/<run-id>/
```

Only the session's scoped credential is uploaded; the controller's administrator credential and SSH private keys are not uploaded. Session artifacts are created with a restrictive umask. This remains a cooperative same-user trust model, not an adversarial sandbox.

Per-log caps and remote state admission checks apply on the execution host. `storage --remote` reports each host's job-state footprint; an unreachable host is reported as an error rather than zero bytes. Source versions remain in `runtime/` so active sessions do not depend on files being overwritten during an upgrade. Old source versions are not automatically deleted; they are small and are outside the per-job state budget. Harness caches, build artifacts, and model downloads remain outside Triad's budget.

## Validation boundary

Automated tests exercise host selection, SSH argument quoting, strict authentication options, network failures, remote path handling, separate Supervisor/Worker hosts, remote snapshots, and refusal to replace an unreachable Worker. Existing real local-process and Windows psmux tests continue to run.

A live SSH/Linux run has been performed with the controller and the SSH host on the same Linux machine (SSH to `localhost`): reverse tunnel, remote tmux Worker, remote evidence and storage, controller kill and restart with the remote Worker surviving, and clean shutdown (see VALIDATION.md). A Windows controller driving a separate Linux machine has not yet been exercised. Run `host-check` and the deterministic two-host example on your selected machines before a production job.
