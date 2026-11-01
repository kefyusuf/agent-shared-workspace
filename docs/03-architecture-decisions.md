# M0.4 — Architecture Decision Records

**Status:** Proposed decision baseline; record acceptance explicitly at M0 gate.

## ADR-001 — Coordination core, not another agent runtime

**Decision:** The product owns shared coordination records; each coding agent runs in its existing client.  
**Why:** Reduce provider coupling, duplicate orchestration, and product scope.  
**Trade-off:** The core cannot promise to start, pause, or inspect native execution in every provider.  
**Revisit when:** A confirmed user requirement needs controlled execution beyond durable messaging.

## ADR-002 — One local authoritative store

**Decision:** Use a single transaction-capable local storage boundary for tasks, leases, messages, knowledge and events; SQLite is the implementation baseline, not a distributed design requirement.  
**Why:** Atomic claims, small operational footprint and simpler recovery.  
**Trade-off:** Remote multi-host concurrency and cloud access are excluded.  
**Revisit when:** Multi-machine operation is a validated product requirement.

## ADR-003 — MCP as the first external adapter

**Decision:** Expose a small MCP tool surface to compatible clients, while keeping domain operations protocol-independent.  
**Why:** MCP fits tool/data access across coding agents.  
**Trade-off:** MCP alone does not give remote agents universal session takeover or real-time activation.  
**Revisit when:** ACP session control or A2A inter-system delegation offers a measured required capability.

## ADR-004 — Explicit snapshots instead of transcript mirroring

**Decision:** Context sharing uses structured, immutable, permissioned task snapshots and links to evidence.  
**Why:** Lowers unnecessary prompt transfer and avoids conflating private provider context with public project knowledge.  
**Trade-off:** Agents must deliberately publish useful handoffs; summaries can omit details and therefore must cite sources.  
**Revisit when:** A native provider safely exposes a narrowly scoped, authorized export that improves the use case.

## ADR-005 — Brain and memory have distinct authority

**Decision:** Accepted design decisions/project policy constitute the Brain; memory includes observations, failures and provisional findings.  
**Why:** Prevent unreviewed agent assertions from silently becoming project rules.  
**Trade-off:** Human/operator approval adds friction.  
**Revisit when:** There is evidence that safely automated approval works for limited low-risk knowledge classes.

## ADR-006 — Task leases plus fencing tokens

**Decision:** Task ownership is leased and writes are fenced. File-level reservations remain a candidate extension and are not a substitute for Git isolation.  
**Why:** Handles concurrent claims, abandoned sessions, and stale agents.  
**Trade-off:** Every protected mutation must validate session, lease and fence token.  
**Revisit when:** Pilot conflict data justifies path-based reservations.

## ADR-007 — Append-only audit events and evidence gates

**Decision:** Changes produce traceable events atomically; review-required tasks cannot be marked complete solely by owner assertion.  
**Why:** Recoverability, explainability and confidence in shared state.  
**Trade-off:** Additional metadata and explicit approval work.  
**Revisit when:** A differentiated low-risk task policy is needed.

## ADR-008 — No semantic index at the start

**Decision:** Use structured retrieval and, if useful, local full-text search before embeddings, vector stores, graph indexes, or repo-wide parsing.  
**Why:** Establish measurable evidence of repeated lookup before investing in indexing complexity.  
**Trade-off:** Some natural-language semantic queries may be less powerful.  
**Revisit when:** Evaluation shows a persistent retrieval failure that full-text and structured filters cannot solve.

## ADR-009 — Separate source code worktrees where supported

**Decision:** Concurrent code-writing agents should work in isolated Git worktrees/branches. Shared task state is not a filesystem mutex and the core does not merge code automatically.  
**Why:** Reduce accidental overwrites and preserve reviewable diffs.  
**Trade-off:** Worktrees and branch reconciliation remain an explicit developer workflow.  
**Revisit when:** There is a concrete conflict-management need beyond Git-native operations.

## ADR-010 — Operator-first minimal UI, after core correctness

**Decision:** Start with service contracts and a CLI/test harness; build a small local Kanban view only after tasks, messages and leases pass tests.  
**Why:** UI is a projection of authoritative state, not a separate source of truth.  
**Trade-off:** Early usability relies on CLI/testing tools.  
**Revisit when:** A pilot demonstrates UI is required to execute acceptance tests.

## ADR-011 — Explicit provider capabilities

**Decision:** Every adapter declares capabilities instead of assuming uniform support for resume/load, push notification, conversation export, or file operation hooks.  
**Why:** Prevent false interoperability promises.  
**Trade-off:** Heterogeneous client experience is visible.  
**Revisit when:** Supported integrations converge on a standardized common capability set.
