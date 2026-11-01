# M0.1 — Product Definition

**Status:** Proposed  
**Version:** 0.1  
**Primary use case:** Two independent coding-agent clients working on one repository on one local computer.

## Problem statement

Coding agents usually maintain separate session contexts and task understanding. When more than one agent works on the same project, essential information is fragmented across conversations, terminals, scratch files, and commits. Consequences include repeated project exploration, duplicated or conflicting work, lost design decisions, stale context, unreliable task status, and expensive handoffs.

A shared state layer should allow agents to coordinate without needing privileged access to another provider's hidden reasoning or complete conversation history.

## Target users

**Primary:** Individual developers coordinating two or three independent coding agents (such as Codex, Claude Code, OpenCode or similar MCP-capable clients) against one local repository.

**Secondary (later):** Small teams with multiple machines or collaborative operators. This is not an MVP target.

## Jobs to be done

- As an agent, I need to learn the current task and approved project constraints without re-reading the entire codebase or an unrelated transcript.
- As an agent, I need to claim a task atomically and detect whether it is already being worked on.
- As an agent, I need to send a durable message to another agent, even when that agent is offline.
- As an agent, I need to publish a concise, sourced handoff and retrieve another agent's authorized context snapshot.
- As a reviewer, I need to see the evidence behind a claimed completion and decide whether to approve it.
- As an operator, I need to identify stale ownership, rejected knowledge proposals, and the provenance of project decisions.

## MVP outcomes

1. **Single shared truth for tasks:** Every connected agent observes a consistent task lifecycle and owner.
2. **Durable asynchronous communication:** Messages are persisted and acknowledged explicitly.
3. **Approved common knowledge:** Project rules and accepted decisions are distinct from tentative observations.
4. **Portable context handoff:** A human-readable and machine-readable summary with references survives client restarts.
5. **Auditable evidence:** Completion status can be traced to commits, tests, reviews, or documented exceptions.
6. **Minimal retrieval:** Agents can ask for only the changed or relevant information, rather than the full workspace history.

## In scope for the first end-to-end pilot

- One project, one host machine, local persistence.
- Agent registration, identity binding, and limited capabilities.
- Task creation, assignment claim, lease renewal, task status transitions, and review gate.
- Direct, durable agent-to-agent messages with inbox, acknowledgment, and deduplication.
- Proposed/accepted/superseded project decisions and sourced memory observations.
- Structured context snapshots tied to tasks and relevant files/evidence.
- Append-only audit events and change-since-cursor retrieval.
- Basic MCP adapter and a minimal operator interface after core correctness is established.
- Explicit permission checks and protection against publishing secrets.

## Out of scope

- Replacing or embedding a model runtime; executing agents autonomously.
- Reading hidden chain-of-thought, provider-internal state, or all private sessions.
- Cross-provider session migration and arbitrary native-session takeover.
- Automatically accepting agent-generated architectural rules.
- Real-time wake-up or push notification across every provider.
- Multi-host coordination, cloud services, centralized team auth, billing, and tenancy.
- Vector databases, graph RAG, language-specific indexing, intelligent task decomposition.
- Automatic code merge/deploy, self-healing, or unsupervised release decisions.
- Comprehensive analytics dashboard and advanced UI in the first core milestone.

## Value and evaluation

**Correctness gates (must pass):** Zero conflicting active task claims under race testing; durable messages and snapshots after process restart; strict read/write authorization; no Done transition without required review/evidence; deterministic handling of duplicate requests.

**Hypotheses to measure, not pre-declared facts:** Lower median handoff prompt tokens and fewer repeated discovery tool calls compared with an equivalent agent workflow without the workspace. Measure usability as well: extra coordination calls, manual intervention count and handoff failure rate.

**Pilot size:** At least two different agent clients, one non-trivial repository, and a repeatable set of 10–20 coordinated task/handoff exercises. Record outcomes even if the shared workspace increases cost.

## Explicit constraints

- The system must remain useful when only one agent is online.
- No decision depends on a specific model provider.
- A failed MCP request must not silently produce a different task owner or duplicate message.
- The operator retains authority over approved project rules and high-risk state transitions.

## M0 completion definition

The domain vocabulary is unambiguous; invariant and failure-mode tests are written; authorization boundaries are explicit; baseline ADRs are recorded; open questions that would block M1 are resolved. M0 does not require software or UI.
