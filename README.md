# Agent Shared Workspace

Local-first, provider-independent coordination workspace for multiple coding agents.

## Project goals

Shared task ownership and Kanban state, persistent agent-to-agent mailbox, governed architecture decisions, searchable technical memory, and permissioned context handoff.

## Architecture

- Python 3.11+ domain API, SQLite with transactional task claims and fencing tokens.
- Accepted decisions (Shared Brain) are separated from agent-reported (Shared Memory) findings.
- Immutable context snapshots can be explicitly handed off to another agent.
- M3.1 introduces a loopback-only authenticated HTTP broker; it is not yet an MCP server or OS sandbox.

## Repository status

**Repository initialized; the verified M3.1 source snapshot is pending import through a feature branch and pull request.** Do not interpret this README as a deployed or tested GitHub build. Local runtime files, database files and credentials must never be committed.

## Roadmap

M0 contracts → M1 coordination/review/recovery → M2 shared knowledge/memory/context → M3.1 local broker → M3.2 MCP integration → M3.3 minimal Kanban → M4 real multi-agent pilot.

No live provider integration or production-security claim is made at this stage.
