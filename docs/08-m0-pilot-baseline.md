# M0 Gate — Pilot Engineering Baseline

**Status:** Provisional implementation baseline, **not** operator sign-off for production.  
**Date:** 2026-11-01  
**Purpose:** Allow a bounded, local M1 domain-core prototype while keeping unresolved governance decisions explicit.

## M1-blocking questions and pilot defaults

| Question | Engineering baseline | Boundaries |
|---|---|---|
| Q-01 authentication | One-time local bootstrap creates a 256-bit-class random operator credential; human operator enrolls agents and issues separate random credentials; each connection opens a bounded 12-hour session | Opaque tokens stored only as SHA-256 hashes, not derived from provider names; local filesystem compromise is outside security guarantee |
| Q-02 approvals | Human operator is the sole future reviewer for the first pilot | `Done` is **unavailable** until the evidence/approval flow is implemented and verified |
| Q-03 other sessions | Never read foreign native conversations or hidden reasoning | Explicit structured context snapshots start in M2 |
| Q-04 completion evidence | Verified references and independent approval are mandatory, not self-reported completion | The evidence schema and verification adapter remain pending; M1.1 rejects all completion requests |
| Q-05 environment | One project, one host, two simulated agent sessions | Test harness is not a real Codex/Claude Code integration |
| Q-06 messages | One sender and one recipient | Broadcast and agent wake-up are deferred |
| Q-13 language | Python 3.11+ standard library + SQLite for core correctness prototype | Technology selection for production and MCP remains revisitable |
| Q-14 limits | Messages up to 16 KiB UTF-8; names up to 120 bytes; task criteria up to 4 KiB; leases 10–300 seconds; inbox 100/page | Retention/quota policy **not** finalized; do not use for sensitive or unlimited data |

## Architecture guardrails

- One local authoritative SQLite store; a single core module owns transactions and invariants.
- No autonomous model runner, task scheduler, queue broker, vector index, graph parser, native session access, or network service.
- Credentials must be stored outside the repository; do not publish environment files, database files, messages, or bearer tokens.
- Agent clients must never be granted a raw SQL connection in the final MCP deployment. The current Python API is a local domain test harness.
- Agent-generated knowledge cannot be promoted to project authority by task status or message content.
- The operator must explicitly review and approve the final security/governance gate before production exposure or real coding-agent adoption.

## Review findings / carry-forward decisions

1. All core mutations must couple the authoritative state and audit event in one transaction.
2. Task ownership uses **session-bound** leases with monotonic fencing tokens.
3. Idempotency keys are scoped to **logical principal + project**, not to an ephemeral session; a reconnect must not duplicate an already-committed send.
4. Inbox reads do not silently acknowledge messages. Explicit acknowledgment implies that a message was read, but never implies execution.
5. Task Done is deliberately fail-closed until evidence and independent review are implemented.
6. Test-only fault injection is private to the local core and must not be part of a transport adapter.

## Unresolved before public release

- Operator credential rotation/recovery, agent disable/revocation and session token rotation.
- Evidence schema and independent-review authority checks.
- Secret screening and robust source-reference path containment for M2.
- Retention/quotas, local backups and journal handling.
- MCP transport authentication, client-specific capability detection and actual two-provider validation.

**Gate decision:** Permit only the local M1.1 proof-of-correctness implementation. Formal M0 approval and full M1 exit remain pending.
