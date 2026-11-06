# M3.2.3 — One-entry operator workflow (Windows pilot)

**Goal:** Reduce repeated manual commands without weakening the existing authentication,
operator approval, or review gates. This change is an orchestration convenience layer,
not a new service or agent framework. The `workspace.ps1` entrypoint is a very small
PowerShell wrapper around tested Python code at `scripts/workspace_cli.py`.

## Requirements

- A local clone of `kefyusuf/agent-shared-workspace` at the feature branch containing this change.
- Windows PowerShell, the Python `py` launcher and Python 3.11+.
- Installed and authenticated `codex` and/or `claude` commands for real provider tests.
- An **isolated/disposable** local project with no secrets. This remains a prototype.
- CLI configuration is modified only by the explicit `connect` command. Existing
  MCP registrations are left untouched; investigate if a stale registration points
  to a previous checkout.

Run from the repository root. No `PYTHONPATH` manipulation or manual MCP JSON/TOML
writing is needed for the scripted path.

## Standard pilot

### Terminal 1: One-time setup

```powershell
.\workspace.ps1 setup
.\workspace.ps1 connect
.\workspace.ps1 doctor
.\workspace.ps1 test
```

`setup` creates `.local/workspace.db`, a local operator, two separate agent identities
(`codex`, `claude`), and an idempotently keyed pilot task. **One-time credentials are
printed to that trusted terminal**: store them securely OFF the repository and do
not redirect the terminal output to a transcript, CI run, agent context, or issue.
A rerun skips an initialized workspace without printing or rotating credentials.
It cannot recover lost credentials. `setup` never downloads or installs Codex/Claude.

`connect` uses the installed clients' MCP management commands and registers a local
`agent_workspace` stdio server pointing to an **absolute path** to the small
`scripts/mcp_entry.py` bridge. It stores **no credential** in those configurations.
It will not overwrite an existing same-named server. This registration is specific
to the checkout path: run the clients' configuration commands again if you move it.

### Terminal 2: Broker

```powershell
.\workspace.ps1 broker
```

The broker runs on loopback TCP port `8765` in the foreground; stop with Ctrl+C.
Use `--port 9000` **before** a subcommand to select another port, e.g.
`.\workspace.ps1 --port 9000 broker`. Use that same value for each client launcher.

### Terminals 3 and 4: Agent clients

```powershell
.\workspace.ps1 run codex
.\workspace.ps1 run claude
```

Each invocation prompts for the corresponding **agent** credential and runs the
existing read-only preflight. If preflight fails, the provider is **not** launched.
Credentials are passed in the child process environment, never as CLI arguments.
Codex also receives an ephemeral config override forwarding only the named
`AW_AGENT_CREDENTIAL` and `AW_BROKER_PORT` variables to the MCP subprocess.
Claude Code inherits these from the launcher process. The script does not manage
remote model authentication or install CLIs.

Verify **actual MCP tool calls** in each provider, not only self-reported prose:
`workspace_identity`, `task_list`, `mail_send`/`mail_inbox`, `task_claim`,
`context_publish`/`context_handoff`/`context_inbox`/`context_get`. A successful
preflight proves only our local adapter and broker can communicate; it does
**not** prove a real Codex or Claude Code session is compatible.

## Other commands

| Command | Meaning |
| --- | --- |
| `.\workspace.ps1 doctor` | Prints no-secret readiness booleans: Python, DB, broker, provider presence |
| `.\workspace.ps1 test` | Executes the complete Python unittest suite |
| `.\workspace.ps1 enroll extra-agent` | Prompts for operator credential; enrolls another agent |
| `.\workspace.ps1 task --key pilot-two` | Prompts for operator credential; adds one idempotent task |
| `.\workspace.ps1 connect codex` | Registers only the Codex MCP client if not already registered |
| `.\workspace.ps1 -h` | Help and supported options |

For non-Windows usage, the same orchestration logic can be invoked as
`python scripts/workspace_cli.py ...`; it has not yet received provider-specific
integration testing on other operating systems.

The `doctor` command additionally reports `codex_mcp_registered` and
`claude_mcp_registered` using each installed CLI's read-only `mcp get agent_workspace`
command. The command hides all CLI output, including configuration values.
A `true` value means the named registration exists, **not** that its executable
path is current, that credentials work, or that the real provider successfully
invoked an MCP tool. Both real-provider and OS-isolation gates remain open in
[Issue #7](https://github.com/kefyusuf/agent-shared-workspace/issues/7).

## Security and known limitations

- Both the broker and `workspace.ps1` must run on the **same machine**. The socket
  is bound to loopback, not publicly accessible. This alone does not secure the DB.
- Any process running with the same OS user's filesystem permissions can open the
  plaintext SQLite database. Credentials in child process environments can be
  inspected by sufficiently privileged local processes. **No OS sandbox exists.**
- Real Codex and Claude Code CLIs are not installed in CI. The orchestration and
  secret-handling code is unit tested, but PowerShell host execution and real
  provider compatibility still require the operator's Windows pilot.
- Never connect this prototype to sensitive workspaces, secrets, or untrusted
  agent processes before OS isolation, review, and host compatibility gates pass.
- The operator credential must never be used for `run` or an MCP configuration.
- CLI subprocess output may contain provider-generated text: do not share raw
  transcripts without reviewing/redacting them.
- PRs remain stacked and should not be merged before independent review and CI.

## Non-goals

No web Kanban, AI model execution, native session sharing, auto-update daemon,
external dependency installer, encrypted secret store, OS user provisioning, or
change to MCP protocol semantics. Those are separate decisions and milestones.
