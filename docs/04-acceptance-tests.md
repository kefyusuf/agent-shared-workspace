# M0.5 — Executable Acceptance Scenarios (Specification)

**Status:** Specification only. These are scenarios for future automated tests; no tests have been run.

## Test conventions

- `A`, `B`, and `R` are different authenticated identities; `R` is an authorized reviewer.
- Clients must use independent sessions; restarting means the runtime process actually terminates and restarts.
- The system is tested through a domain API and later through MCP adapter integration, not by direct database edits.
- Each scenario is repeated where appropriate with storage faults, retries, and stale versions.

| ID | Scenario | Required result |
|---|---|---|
| AT-01 | Agent A and B concurrently claim a Ready task | Exactly one valid lease; the other returns a conflict |
| AT-02 | A's lease expires, B claims and A retries protected write | A gets `STALE_FENCE`; B's claim remains intact |
| AT-03 | A retries a successful claim/send with the same idempotency key | One logical mutation and one message/event effect |
| AT-04 | A reuses an idempotency key with different content | Explicit `IDEMPOTENCY_CONFLICT`; no new mutation |
| AT-05 | A sends a message to offline B and service restarts | B's inbox contains the unchanged message |
| AT-06 | B reads inbox without ack, reconnects | Message remains unacknowledged and retrievable |
| AT-07 | A attempts to read B's private snapshot or messages | Forbidden without content leakage |
| AT-08 | A proposes an architectural rule, then retrieves project brief | Rule is not presented as accepted or authoritative |
| AT-09 | R accepts a sourced decision | Subsequent project brief labels decision accepted with provenance |
| AT-10 | A publishes a task snapshot then ends its session | B can retrieve permitted handoff and source refs after restart |
| AT-11 | A tries to mark review-required task Done without approval | Transition rejected; task stays non-Done |
| AT-12 | Authorized reviewer approves with required evidence | Task may enter Done once rules and versions validate |
| AT-13 | Owner attempts to approve own review-required task | Explicit permission error |
| AT-14 | Two task transitions use the same expected version | One succeeds; stale transition gets version conflict |
| AT-15 | Service restarts after committing a state change | State and corresponding audit event both persist |
| AT-16 | Simulated failure before transaction commit | Neither state mutation nor committed event is visible |
| AT-17 | `changes_since` paginates across commit boundary | Every permitted event appears once in deterministic sequence |
| AT-18 | A tries to publish credentials/private session data | Content is rejected or redacted under explicit policy; no leak in search/events |
| AT-19 | Client sends forged `agent_id` claiming to be B | Server records authenticated A or rejects; never impersonates B |
| AT-20 | Context references a path outside project root | Access rejected; no traversal or symbolic-link escape |
| AT-21 | A reads accepted decision after it has been superseded | Current version returned; older record remains auditable and labeled superseded |
| AT-22 | Message is acknowledged | Receipt persists; UI does not claim recipient performed requested work |
| AT-23 | MCP adapter lacks resume/push capability | Capability is reported unsupported, not silently simulated |
| AT-24 | Two isolated worktrees edit related areas | Workspace shows task owners and evidence; merging is not automatic |

## End-to-end pilot walkthrough

1. Operator creates Project P, registers A/B/R and creates a task with acceptance criteria.
2. A claims task and receives a bounded lease with fencing token.
3. A sends B a question; B is offline; message persists.
4. A publishes an interim context snapshot and proposed observation.
5. B reconnects, retrieves inbox and relevant snapshot, and acknowledges the message.
6. B is unable to approve A's observation as authoritative without reviewer capability.
7. A publishes evidence and moves task to Review.
8. R examines refs and approves; task moves to Done.
9. Restart the core; verify all task, message, snapshot, review and audit records.
10. Attempt stale/unauthorized actions; verify state remains valid.

## Performance and token evaluation protocol

Repeat an equivalent development handoff with and without the workspace using the same repository snapshot, task brief and agent models. Measure: (a) tokens passed for initial context and handoff, (b) repeated file/discovery calls, (c) wall-clock handoff latency, (d) incorrect/stale context retrievals, and (e) operator interventions. Do not assume improvement; present medians, spreads and examples of negative cases. Performance may be optimized **only after** correctness tests pass.

## Exit conditions

No correctness/security invariant failure in the test suite; a documented result for each AT scenario; reproducible restart/retry results; unsupported provider capabilities clearly identified. Passing unit tests alone is insufficient if the end-to-end handoff fails.
