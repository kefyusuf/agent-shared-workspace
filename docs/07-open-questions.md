# M0.8 — Open Questions and Decision Queue

**Status:** Items to settle before or during M1; defaults below are proposals, not confirmed user requirements.

## Blocking before M1

| ID | Question | Proposed M1 default | Reason |
|---|---|---|---|
| Q-01 | How are local agent sessions authenticated? | Operator-enrolled local identity with per-client credentials bound by adapter | Prevent self-declared identity spoofing |
| Q-02 | Who may approve accepted decisions and final task review? | Local human operator; no agent self-approval | Clear initial governance boundary |
| Q-03 | Can agents read each other's raw session histories? | No; only explicit permitted handoff snapshots | Provider-independent and safer |
| Q-04 | What constitutes task completion evidence? | Referenced commit/diff plus test or manual verification result; explicit justified exception | Different task types need different proof |
| Q-05 | What is the pilot environment? | One local Git repository, two agents and one human reviewer | Bounded reproducibility |
| Q-06 | Should group messages/broadcasts ship in M1? | No; direct messages only | Keep ACL and receipt rules small |

## Non-blocking, postpone until evidence exists

| ID | Question | Deferred recommendation |
|---|---|---|
| Q-07 | Should we adopt a repository index/graph or vector DB? | Measure retrieval bottleneck first |
| Q-08 | Should we support native ACP session management? | Add only for confirmed capabilities and use cases |
| Q-09 | Do clients need push notification hooks? | Start with explicit inbox polling/checkpoints |
| Q-10 | Should file-level leases be mandatory? | Use Git worktrees; record conflicts before expanding |
| Q-11 | Should the UI permit agent-config editing? | Keep UI informational and task/review-focused first |
| Q-12 | Should task approvals be automated? | No in pilot; revisit by risk tier |
| Q-13 | What is the implementation programming language? | Choose after M0; evaluate low operational complexity, MCP ecosystem, transaction support and maintainability |
| Q-14 | How long do messages and snapshots remain? | Define configurable local caps and explicit operator retention policy before production use |

## M0 decision record

At the M0 exit review, record which proposed defaults are accepted, any revisions to invariants, remaining risks, and whether M1 may begin. Do not treat this draft package as an implicit approval.
