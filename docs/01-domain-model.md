# M0.2 — Domain Model and Lifecycles

**Status:** Proposed. This is a conceptual model, not a database schema.

## Aggregate roots and records

| Entity | Purpose | Minimum identifiers / fields |
|---|---|---|
| Project | Scope boundary for all records | `project_id`, `name`, `root_reference`, `created_at` |
| Agent | Stable logical participant | `agent_id`, `display_name`, `provider_kind`, `capabilities`, `status` |
| Session | Individual connection/execution instance | `session_id`, `agent_id`, `project_id`, `created_at`, `last_seen_at` |
| Task | Unit of coordinated work | `task_id`, `project_id`, `title`, `description`, `acceptance_criteria`, `status`, `version`, `review_required` |
| Lease | Temporary exclusive authority | `lease_id`, `resource_type`, `resource_id`, `holder_session_id`, `expires_at`, `fence_token` |
| Message | Durable, directed communication | `message_id`, `project_id`, `sender_agent_id`, `recipient_agent_id`, `body`, `reply_to`, `created_at`, `idempotency_key` |
| Receipt | Per-recipient delivery state | `message_id`, `recipient_agent_id`, `read_at`, `acknowledged_at` |
| MemoryEntry | Sourced observation / finding | `memory_id`, `project_id`, `kind`, `content`, `source_refs`, `recorded_by`, `status` |
| Decision | Explicitly governed project knowledge | `decision_id`, `project_id`, `statement`, `rationale`, `status`, `approved_by`, `supersedes` |
| ContextSnapshot | Immutable handoff artifact | `snapshot_id`, `task_id`, `author_session_id`, `summary`, `source_refs`, `visibility`, `content_hash`, `created_at` |
| EvidenceRef | Reference to test, commit, review or artifact | `evidence_id`, `task_id`, `type`, `locator`, `recorded_by`, `verification_status` |
| AuditEvent | Append-only provenance | `event_id`, `project_id`, `actor_id`, `action`, `subject_ref`, `payload_digest`, `occurred_at` |

## Relationships

- A Project has many Agents, Sessions, Tasks, Messages, MemoryEntries, Decisions, and AuditEvents.
- A Session belongs to one Agent and one Project. Session identity is different from stable Agent identity.
- A Task has at most one **valid active exclusive claim**, represented by a Lease.
- A Message has exactly one sender and one recipient in the MVP. Broadcast and group mail are deferred.
- A ContextSnapshot belongs to a Task, has an originating Session, and is immutable; new snapshots supersede prior ones by reference.
- A Decision may cite Task, MemoryEntry, EvidenceRef, and another Decision as provenance.
- An AuditEvent records attempted or successful state changes as separately defined by the contract. The authoritative state and event log must commit atomically.

## Task lifecycle

`Backlog -> Ready -> InProgress -> Review -> Done`

`InProgress -> Blocked` and `Blocked -> Ready` are supported. `Review -> InProgress` allows requesting changes. `Cancelled` is a terminal state from any nonterminal state with recorded reason. A Done task is not reopened in place; create a new task or an explicitly linked follow-up.

### Task transition rules

1. Task creation starts in `Backlog` or `Ready` with clear acceptance criteria.
2. Only a valid active lease holder may transition `Ready -> InProgress` or publish in-progress task completion/handoff state.
3. `InProgress -> Review` requires a handoff snapshot and references to evidence or an explicit evidence-not-applicable explanation.
4. `Review -> Done` requires a reviewer approval by a distinct authorized actor when `review_required=true`, plus all mandatory evidence checks.
5. An expired lease blocks further owner-only mutations; recovery must explicitly reassign the task.
6. Every transition includes `expected_version` to reject stale writes.

## Lease lifecycle

`Available -> Claimed -> Renewed* -> Released | Expired | Revoked`

A successful claim atomically generates a monotonically increasing **fencing token** for the resource. Expiration alone does not make a previously issued token safe: every protected mutation verifies the current token. Wall-clock timestamps do not replace fencing tokens.

## Message lifecycle

`Stored -> AvailableToRecipient -> Read -> Acknowledged`

`Read` is not equivalent to `Acknowledged`. Acknowledgment is an explicit durable act. No guarantee is made that a recipient LLM has processed, reasoned about, or acted on an acknowledged message. Delivery is **at least once** when an agent retries; application-level effects use idempotency keys.

## Memory and decision lifecycle

- MemoryEntry: `Proposed -> Accepted | Rejected`; accepted entries may become `Superseded`.
- Decision: `Proposed -> Accepted | Rejected`; `Accepted -> Superseded` only through a newer governed decision.
- Unverified observations never automatically turn into authoritative project decisions.
- An entry's content is versioned/immutable once accepted; corrections are new revisions linked by provenance.

## Context snapshot contract

Each snapshot contains: objective, current status, work completed, files/symbols changed (references, not copied contents), decisions or assumptions, validation results, unresolved questions, next recommended action, timestamp, project/task/session identifiers, source references, and visibility scope. Large attachments are linked, not indiscriminately embedded.

Visibility in MVP: `project` (other authorized agents on that project) or `private` (author and authorized operator). External provider session exports are **not** implicitly accessible.

## Event and cursor semantics

A monotonically increasing `event_id` orders committed state-change events within a project. `changes_since(cursor, limit)` returns deterministic ordered pages and a continuation cursor. Cursor means 'last processed committed event', not a wall-clock timestamp. Events are append-only and cannot be edited by agents.
