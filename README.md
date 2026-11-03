# Agent Shared Workspace — M3.2.2 MCP & Broker Hardening

**Status:** Tested local coordination core with a loopback-only HTTP broker (Python 3.11+, SQLite, no external runtime dependency). **Not production-ready.** Includes a handshake-era MCP stdio adapter tested with independent subprocesses, but no actual Codex/Claude Code CLI run, native session access or web Kanban yet.

M3.2.2 adds legacy MCP protocol-version negotiation and a two-second broker connection idle timeout to reject stalled body reads. See [hardening notes](docs/23-m3.2.2-handshake-and-broker-hardening.md). Real Codex/Claude Code compatibility remains unverified.

M3.2.1 adds a [Windows real-client compatibility pilot guide](docs/22-m3.2.1-provider-compatibility-gate.md), read-only [preflight](scripts/host_preflight.py), safe launcher, operator test-task creation and graceful session logout on normal stdio closure. **Actual Codex and Claude Code runs are still pending.**

M3.2 adds a **stdio MCP adapter** using independent agent identities over the M3.1 broker. See [M3.2 setup and security contracts](docs/21-m3.2-mcp-stdio.md).

M3.1 adds an **authenticated loopback broker** with strict RPC allowlisting, JSON size/Host/Origin checks and session revocation enforcement on top of M2.3. See [local broker contracts](docs/20-m3.1-local-broker.md).

M2.3 adds **permissioned, task-scoped context snapshots, explicit agent-to-agent handoff grants, recipient inbox, filtered source references and incremental metadata changes** to the M2.2 Shared Memory, M2.1 governed Shared Brain and M1 coordination/review/recovery layers.

## One-command interface (Windows pilot)

Run these commands from a trusted PowerShell terminal in the repository checkout.
`workspace.ps1` is a thin wrapper around `scripts/workspace_cli.py`.
**The entry point makes setup simpler; it does not remove the existing security/real-provider gates.**

```powershell
.\workspace.ps1 setup             # Once: create workspace, 2 agents, pilot task
.\workspace.ps1 connect           # Once: register installed Codex/Claude MCP clients
.\workspace.ps1 doctor            # No secrets; show local readiness
.\workspace.ps1 broker            # Keep this terminal open
```

In **two additional terminals**, launch each client separately (you will be
prompted for its *own* agent credential, issued by `setup`):

```powershell
.\workspace.ps1 run codex
.\workspace.ps1 run claude
```

To run tests, use `.\workspace.ps1 test`. If the workspace already exists,
`setup` will not replace it or regenerate credentials. Use `-h` for all available
commands, including `enroll` and `task`. Python 3.11+ and the `py` launcher are
required; Codex and Claude Code CLIs must be installed separately. **Never paste
or commit the one-time credentials**, and use only disposable test projects.

Full workflow, prerequisites, troubleshooting and limitations:
[Single-entry operator guide](docs/24-one-command-workflow.md).

## Verify locally

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python examples/context_handoff.py
```

PowerShell:

```powershell
$env:PYTHONPATH = "src"
py -m unittest discover -s tests -v
py examples/context_handoff.py
```

The five earlier scenarios still run (`examples/two_agents.py`, `review_cycle.py`, `recovery_security.py`, `knowledge_cycle.py`, `memory_cycle.py`). They are **simulations**, not live coding agents.

## M3.1 broker quick start

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m agent_workspace.broker init --db .local/workspace.db --name "Sample Project" --root .
PYTHONPATH=src python -m agent_workspace.broker serve --db .local/workspace.db --port 8765
```

`init` displays a one-time operator credential. Do not commit secrets, local DB or credentials. See [M3.1 security model and API](docs/20-m3.1-local-broker.md).

## M2.3 API

- `publish_context()` — immutable checkpoint from the current live task lease holder, with source refs and path labels (never reads files).
- `send_handoff()` — direct snapshot access grant to one named agent, with an action to take, without starting an agent or transferring the lease.
- `get_context()` — permissioned read with private message ref redaction and stale knowledge/memory ref filtering.
- `list_task_contexts()` — permissioned bounded task-specific previews.
- `handoff_inbox()` — per-agent inbox of snapshot handoffs, cursor-pagination.
- `context_changes()` — permission-filtered incremental event metadata.

See [M2.3 contracts and limitations](docs/18-m2.3-context-gateway.md) and [test report](docs/19-m2.3-test-results.txt). Older documents are retained as historical milestone records.

## Security boundary

Only the writer, an explicitly named handoff recipient, current live task lease holder, or project operator can read a snapshot. Source references are filtered on each read, but **free-text agent summaries/actions are not sanitized**; do not paste private transcripts or credentials into them. Snapshot contents are agent-reported/unverified and may include prompt-injection instructions. Stored data is plaintext. Giving untrusted agents direct SQLite access bypasses application-level controls. M3.1 now provides an authenticated loopback HTTP broker, but **OS-level isolation for untrusted same-user processes is not implemented**. Do not deploy this prototype with untrusted agents.

A snapshot is a structured checkpoint, **not shared native model context, live session replay, a semantic search result or a proven token-budget reduction**. Byte-limited fields and on-demand reads bound transferred content but no provider-specific tokenizer budget exists.

## Roadmap

- M0: Product, domain, security specifications drafted; final operator sign-off not recorded.
- M1.1–M1.3: Local coordination, evidence/review, recovery — acceptance-tested prototypes.
- M2.1: Governed Shared Knowledge — acceptance-tested local prototype.
- M2.2: Sourced Shared Memory + bounded FTS5 search — acceptance-tested local prototype.
- **M2.3: Permissioned Context Gateway, handoffs and incremental context updates — acceptance-tested local prototype.**
- M3.1: Authenticated loopback broker — tested locally, subject to OS isolation limitations.
- **M3.2: MCP stdio adapter — tested with separate subprocesses and a real broker; provider CLI compatibility still unverified.**
- M3.3: Minimal Kanban — not implemented.
- M4: Live multi-agent adverse-condition pilot and verified cost/quality measurements — not implemented.

GitHub PR and CI status must be verified separately from local test results.
