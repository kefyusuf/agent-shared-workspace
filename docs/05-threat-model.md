# M0.6 — Threat Model and Trust Boundaries

**Status:** Proposed. Security-oriented design assessment, not a security audit.

## Assets

- Approved project rules and decision history
- Task ownership, review approvals and completion evidence
- Agent-to-agent messages and context snapshots
- Local repository metadata and referenced files
- Authentication bindings, secrets and private session information
- Audit events and the event cursor

## Actors and boundaries

1. **Operator:** Local human with approval and configuration authority.
2. **Agent process:** Untrusted for authorization purposes even if configured by the operator. Agent instructions and tool outputs may be adversarial.
3. **MCP adapter:** Authenticates/binds a client session; it must not treat a claimed payload identity as authoritative.
4. **Core domain:** Applies permissions, invariants and transaction rules.
5. **Storage:** Persistent local state. A compromised host/storage is outside the MVP's strong threat protections; local backup and file permissions are still needed.
6. **Repository and tool results:** External text and code are untrusted input, including prompt-injection content.

## Risks and MVP controls

| Risk | Example | Required control |
|---|---|---|
| Impersonation | Agent A sends a message as B | Server-bound identity and source validation |
| Task takeover | Expired A session edits B's task | Version check plus lease fencing token |
| Knowledge poisoning | Agent stores 'disable tests' as approved policy | Proposal vs approval workflow, separate reviewer |
| Context exfiltration | Agent exposes private transcript or `.env` | Explicit visibility ACL, safe reference resolver, payload screening, no arbitrary private file read |
| Prompt injection | Repo file directs assistant to ignore permissions | All retrieved content is data; core enforces permission regardless of model instruction |
| Replay / duplicate operations | Retry sends two messages or approvals | Idempotency key with payload binding |
| Lost work | Write committed but event missing | Transactionally coupled state and audit event |
| False completion | Agent says 'tests passed' without proof | Evidence status, reviewer check and reasoned exception |
| Directory traversal | `../../secrets` reference | Canonical project-root confinement and symlink checks |
| Data over-collection | Every prompt/transcript copied to memory | Structured selective snapshots, size limits, opt-in context publication |
| Stale memory | Old rule shown as current | Supersession links, status filtering and provenance |
| Privacy in logs | Secret stored in error/audit event | Redaction at ingestion and output, minimal event payload/digest |
| Resource exhaustion | Endless messages/snapshots | Request caps, retention configuration, pagination and storage quotas |

## Minimum authorization model

- `operator`: manage project, agent enrollment, policy and approvals.
- `agent`: read permitted project knowledge, propose memory, send/ack own messages, claim and update own tasks, publish permitted snapshots.
- `reviewer`: explicitly granted ability to approve decisions and tasks other than those authored by the same identity where separation is required.

Roles are **capability sets**, not labels that automatically grant all data access. Session identity, project membership, capability, resource visibility and resource ownership all participate in authorization checks.

## Explicit non-guarantees

The MVP does not provide secure execution sandboxes, comprehensive secret detection, prevention of malicious code execution by an external agent, encrypted multi-host transport, or the ability to inspect hidden provider internals. Secret-scanning heuristics cannot guarantee detection; minimize captured data and enforce allowlisted source access.

## Human review boundaries

Approving a project decision, overriding mandatory evidence, finalizing a task and changing authorization policy require explicit appropriate authority. Autonomous agent messages cannot elevate their own privileges by requesting it in text.

## Pre-M1 safety checks

- Define how the local client authenticates without relying on spoofable `agent_id` strings.
- Specify project-root resource read policy.
- Fix max sizes and retention windows for messages/snapshots/events.
- Decide whether startup enrollment is operator-approved, which is the recommended baseline.
