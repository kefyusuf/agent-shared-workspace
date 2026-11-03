# M3.2.4 — Windows SQLite connection lifetime

## Reproduction

On Windows with Python 3.15, `workspace.ps1 test` completed 209 tests with 14 errors and one platform skip. Each error was `PermissionError: [WinError 32]` while `TemporaryDirectory` attempted to remove an open `workspace.db`, `context.db`, or `partial.db`. Python also reported `ResourceWarning: unclosed database`.

These were errors in test fixture cleanup, not failing business assertions. The existing green Linux CI did **not** verify Windows behavior.

## Root cause

`with sqlite3.connect(...) as db:` uses SQLite's **transaction manager**, which commits or rolls back on exit but **does not close the underlying connection**. Python's garbage collection may hide the leaked handle on POSIX, but Windows prohibits unlinking an open SQLite database file. Corrupt schema handling in `Workspace._connect` also performed PRAGMA initialization *before* entering its `try/finally`, which could leak a connection when initialization failed.

## Fix

- Use `contextlib.closing(...)` around every direct test and CLI/broker SQLite connection. Where tests write data, retain SQLite's transaction semantics with `with closing(sqlite3.connect(...)) as db, db:`.
- Move all post-connect PRAGMA operations inside `Workspace._connect`'s `try` so the `finally: db.close()` path runs even on corrupt files.
- Add tracked-connection tests for normal workspace operations and corrupt DB initialization.
- Extend the GitHub test matrix to Windows Python 3.13 and 3.15, keeping Ubuntu Python 3.11/3.13.

## Acceptance

- The user's 14 Windows cleanup errors must be absent on the equivalent CI runner.
- A corrupt SQLite database must close any opened handle before propagating the error.
- All prior 209 tests remain intact, including platform-conditional skip(s).
- The agent/broker design remains unchanged; no schema migration or new dependency is introduced.

## Limits

CI Windows is a reproducible reference platform, but a successful CI run does not directly validate the user's specific Qoder-bundled Python build or local filesystem/antivirus conditions. Re-run `workspace.ps1 test` locally after checking out the fix branch. No credentials or database files should be shared in logs.
