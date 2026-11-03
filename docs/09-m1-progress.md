# M1.1 — Persistent Coordination Core (Implementation Report)

**Status:** Working prototype; M1 not fully complete.  
**Scope boundary:** No MCP server, UI, raw context sharing, approved Brain, evidence verification, or final task approval.

## Implemented

- One-time local project bootstrap and operator enrollment of logical agents.
- Separate stable agent identities, ephemeral sessions and high-entropy credential/session bearer tokens.
- SQLite durable tasks with optimistic task versions, session-bound leases, monotonic fencing tokens, bounded renewal, release and blocked/requeue transitions.
- Direct immutable agent messages, recipient-filtered inbox, explicit read and acknowledgment receipts.
- Principal-scoped idempotency across session restarts, payload-mismatch rejection. **Important:** Replay does not transfer a lease from an old session to a new session; wait for expiry/reclaim with a new request key.
- Append-only audit event records committed in the same transaction as state changes.
- Local programmatic API, two-session demonstration and reproducible `unittest` suite (including an independent-process claim race).

## Supported scenarios

| M0 acceptance scenario | M1.1 result |
|---|---|
| AT-01 competing claims | Covered: one winner |
| AT-02 stale fence after reclaim | Covered |
| AT-03 retry same request | Covered, including reconnect retry |
| AT-04 key reused with different content | Covered |
| AT-05 durable offline mail | Covered across Workspace restart |
| AT-06 inbox no implicit acknowledgment | Covered |
| AT-14 version conflict | Covered |
| AT-15 state + event persistence | Covered |
| AT-16 rollback on fault | Covered with test-only injection |
| AT-19 forged sender identity | Covered at domain API boundary |
| AT-22 persisted message acknowledgment | Covered; not a work-execution claim |

## Not yet satisfied

- **End-to-end agent interoperability:** Code uses simulated sessions. No Claude Code, Codex or OpenCode connection has been tested.
- **Full task review lifecycle:** `InProgress -> Review -> Done` is not implemented; `finish_task` fails with `REVIEW_REQUIRED`.
- **AT-07–AT-13, AT-17, AT-18, AT-20, AT-21, AT-23, AT-24** are not claimed passed.
- **Long-running cross-process soak testing and crash/power-loss testing** are not complete. Race tests now use both independent SQLite connections in concurrent threads **and two separate Python processes**.
- **Operator enrollment** and session creation are controlled bootstrap operations without replay-idempotency. Handle their credentials carefully; rotation remains future work.
- **Audit record retention and data export** are not implemented. Source code and local database permissions alone do not constitute a security audit.

## Next bounded implementation (M1.2)

1. Define an explicit evidence record with submitted/verified/rejected status, provenance and a minimal operator verification action.
2. Add `InProgress -> Review` using a structured handoff reference, a specific evidence requirement and expected version/fencing token.
3. Implement operator-only, non-self approval and `Review -> Done` with full negative tests.
4. Add subprocess-level concurrency and server-restart fault tests, plus operator revocation and credential recovery strategy.
5. Reassess the M1 gate with test evidence before implementing MCP (M3) or context retrieval (M2).
