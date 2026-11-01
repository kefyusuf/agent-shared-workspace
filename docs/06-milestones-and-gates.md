# M0.7 — Milestone Plan and Gates

**Status:** Proposed, stage-gated.

## M0 — Product Definition & Contracts (current)

**Deliverables:** Scope and user journeys; core vocabulary; domain lifecycle and invariants; ADRs; authorization boundaries; acceptance specifications; risk register; measurable pilot protocol.

**Exit gate:** Reviewer resolves the M1-blocking questions, confirms the scoped user journey and signs off on domain contracts and security assumptions. No code is needed.

## M1 — Persistent Coordination Core

**Only in scope:** local authoritative persistence, project/agent/session, tasks, task lease/versions, direct messages/receipts, event ledger, and minimal programmatic interface. Write tests first (RED), minimum implementation (GREEN), refactor and retest.

**Acceptance:** AT-01 to AT-06, AT-14 to AT-16, AT-19 and AT-22 pass; restart/retry behavior is reproducible. No UI, vector search, agent orchestration, or runtime integrations beyond a test harness.

## M2 — Knowledge & Context Gateway

**Only in scope:** proposed vs accepted decisions; observations, structured handoff snapshots, visibility permissions, source references, change cursor, minimal retrieval and project brief. Keep search simple and bounded.

**Acceptance:** AT-07 to AT-10, AT-17, AT-18, AT-20 and AT-21 pass. Reviewer sees accepted/superseded state and cited provenance.

## M3 — Agent Adapters & Operator Kanban

**Only in scope:** MCP adapter for at least two available clients, capability detection, simple localhost Kanban with task/owner/review states, inbox and context details. Agent clients remain separate processes; no universal push/wake guarantee.

**Acceptance:** AT-23 to AT-24 and the full two-client pilot walkthrough pass. UI matches core state after refresh/restart; it never silently edits database tables.

## M4 — Reliability & Efficiency Pilot

**Only in scope:** fault injection, concurrency tests, authentication/permission review, explicit reviewer controls, storage backup/recovery guidance, and comparison against no-workspace baseline.

**Acceptance:** No invariant violations under rehearsed failure cases; measured handoff outcomes with token/tool-call trade-offs and limitations. Document known unsupported client capabilities and recovery runbook.

## Decisions intentionally deferred

- Implementation language and framework
- Dedicated native ACP adapter
- A2A protocol integration
- Native session reattachment or context import
- File-level reservations (as distinct from task lease and Git worktrees)
- Vector search, repo graph indexing and dynamic code-symbol retrieval
- Multiple host machines and cloud storage
- Automated task decomposition, scheduler, supervisor agent

## Prioritization rule

Promote a deferred feature only when it addresses an observed failure in the current pilot, has a concrete acceptance test, and does not violate a previously adopted invariant. 'Useful someday' is not enough.

## Definition of Done for each implementation milestone

1. Existing tests remain green.
2. New negative cases fail before implementation and pass afterward.
3. No unauthorized state transitions can be demonstrated.
4. Documentation accurately reflects real behavior, not intent.
5. Commits and review evidence are traceable to the milestone.
6. Scope expansion is logged separately rather than silently merged into active work.
