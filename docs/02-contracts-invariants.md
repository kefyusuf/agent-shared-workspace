# M0.3 — Contracts and Non-negotiable Invariants

**Status:** Proposed. Behavioral contracts are transport-neutral; MCP is an adapter, not the domain authority.

## Common request and response envelope

Each mutating request includes: `project_id`, authenticated `actor_session_id` (server-bound; not trusted from free-form payload), `request_id` / `idempotency_key`, optional `expected_version`, and operation-specific arguments. Protected task operations also carry the current `fence_token`.

Each result provides: outcome (`success` or structured error), resource identifiers, current resource version when applicable, and a committed `event_id` for successful state changes. Errors must have stable machine-readable codes.

**Stable errors:** `UNAUTHENTICATED`, `FORBIDDEN`, `NOT_FOUND`, `VALIDATION_FAILED`, `VERSION_CONFLICT`, `LEASE_HELD`, `LEASE_EXPIRED`, `STALE_FENCE`, `INVALID_TRANSITION`, `REVIEW_REQUIRED`, `EVIDENCE_MISSING`, `IDEMPOTENCY_CONFLICT`, `CURSOR_INVALID`, `PAYLOAD_REJECTED`.

## Invariants

**INV-01: Project isolation.** Every operation is scoped to one project; no cross-project reads/writes without a separately authorized scope.

**INV-02: Bound identity.** The authenticated transport session determines actor identity. A client-supplied `agent_id` cannot impersonate another agent.

**INV-03: Single valid claim.** A task cannot have two simultaneously valid exclusive claims. Claim and fencing-token issuance are one atomic transaction.

**INV-04: Stale-session exclusion.** Expired, revoked, or superseded fencing tokens cannot perform protected writes, even if the former owner reconnects.

**INV-05: Optimistic concurrency.** Task and decision writes require an expected version; concurrent stale writes fail explicitly.

**INV-06: Atomic persistence.** Authoritative state mutations and their audit events commit together or not at all.

**INV-07: Durable messages.** An acknowledged send is persisted, even if the recipient is disconnected or the server restarts.

**INV-08: Idempotency.** Replaying the same logical mutating request with the same key and payload returns the original result; reusing its key with different content fails.

**INV-09: Explicit delivery acknowledgment.** Inbox reading does not silently acknowledge messages. A read cursor is separate from acknowledgment.

**INV-10: Governed knowledge.** A raw observation cannot become an accepted architectural rule through retrieval, summarization, or self-approval by an unprivileged agent.

**INV-11: Permissioned context.** Native session transcripts, hidden reasoning, environment variables, credentials, and private snapshots are never auto-exposed to other agents.

**INV-12: Completion evidence.** A task requiring review cannot enter Done without a distinct authorized reviewer approval and required evidence or a justified, authorized exception.

**INV-13: Referential provenance.** Every accepted decision, published handoff, and completion approval identifies its author, time, task/project, and relevant source references.

**INV-14: Monotonic change feed.** Committed events are read in stable order; pagination does not drop events at boundaries.

**INV-15: No false execution guarantee.** Storing or acknowledging a message does not claim that the recipient agent executed instructions in it.

## Minimal logical operations

The initial MCP surface may group these operations into seven tools; an eventual implementation can split actions if ergonomic or security requirements justify it.

| Group | Operations | Authorization rule |
|---|---|---|
| `workspace_brief` | `get` | Any project-authorized session; only approved rules and permitted task summaries |
| `tasks` | `create`, `get`, `list`, `transition`, `review` | Task writes are role/lease/version controlled; self-review disallowed when review required |
| `messages` | `send`, `inbox`, `get`, `ack` | Sender is authenticated; only recipient or operator may read/ack |
| `memory` | `propose`, `search`, `get`, `decide` | Any eligible agent may propose; only an authorized reviewer may accept/reject |
| `contexts` | `publish`, `get`, `list` | Author may publish sanitized snapshots; reads respect visibility |
| `changes` | `since` | Project-scoped and subject to visibility filters |
| `leases` | `claim`, `renew`, `release` | Atomic resource claim; bound to current session and fencing token |

### Message send

**Input:** `recipient_agent_id`, `body`, optional `reply_to`, `idempotency_key`.

**Output:** persisted `message_id`, `event_id`, delivery state `stored`, and a stable recipient-scoped order key.

**Failure modes:** unknown recipient, unauthorized scope, size cap exceeded, duplicate key with different payload, storage failure. Sender chooses content but cannot forge the source identity.

Messages are immutable after acceptance. Replies reference `message_id`; unsolicited asynchronous agent execution is not implied.

### Task claim and update

**Claim input:** `task_id`, `expected_version`, `requested_lease_duration` (bounded by server policy).

**Claim output:** `lease_id`, `fence_token`, `expires_at`, new task version, `event_id`.

**Update input:** `task_id`, `expected_version`, current `fence_token`, requested transition, relevant evidence/handoff references.

**Rule:** Lease expiry allows recovery but does not transfer task ownership automatically without an explicit successful new claim. A crash leaves an auditable stale owner/lease history.

### Context publication and retrieval

**Publish input:** `task_id`, structured handoff fields, visibility, allowed source references, `idempotency_key`.

**Retrieve input:** task or snapshot identifier and optional section selector; return a bounded result and links to permitted sources. A snapshot remains immutable, with future snapshots citing the earlier version.

**Rule:** Filesystem paths in references are interpreted only within an allowed project root, after canonicalization; no unrestricted file-reading primitive is introduced by this contract.

### Knowledge approval

A proposal has type (`observation`, `finding`, `failure`, `decision`), text, provenance and proposer. An authorized reviewer can accept/reject and must provide rationale. Accepted knowledge may be superseded through a new review event. Search results distinguish **accepted** from **proposed** entries without relying only on text wording.

### Changes since cursor

**Input:** `cursor`, `limit` and optional entity filters.  
**Output:** ordered visible events, `next_cursor`, `has_more`. Filtering does not expose private metadata. The cursor advances across filtered-out committed events safely; the contract must not silently omit subsequently visible events.

## Review and evidence

For the pilot, `review_required=true` by default. Evidence can be a commit reference, test report, diff or manual verification report. The evidence record must differentiate `submitted` from `verified`; a path or free-form claim is not proof of passing tests. A task can be sent to Review with failed tests if the failure is explicitly documented, but cannot reach Done unless acceptance criteria and review rules are satisfied or an authorized exception is recorded.

## Example: race outcome

Given two sessions A and B claiming the same Ready task with the same initial version, exactly one receives a lease; the other receives `VERSION_CONFLICT` or `LEASE_HELD`. After A's lease expires and B obtains a newer fence token, A's attempted transition fails with `STALE_FENCE` regardless of client clocks or previously fetched task state.

## Future compatibility

Native session access and external A2A agents can become adapters; they may not bypass core authorization, lease or evidence rules. Provider adapter capability detection must distinguish `supported`, `unsupported`, and `unknown`, rather than claiming features based on an agent's name.
