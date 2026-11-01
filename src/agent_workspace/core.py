"""Local coordination domain API.

This module is NOT a network server. It is intentionally dependency-free and
exposes no filesystem-reading tool, provider session access, or agent runner.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from urllib.parse import urlsplit


class WorkspaceError(Exception):
    """Stable machine-readable domain error without leaking sensitive values."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(detail or code)


def _uuid() -> str:
    return str(uuid.uuid4())


def _secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _text(value: Any, field: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError("VALIDATION_FAILED", f"{field} must be non-empty text")
    if len(value.encode("utf-8")) > max_bytes:
        raise WorkspaceError("PAYLOAD_REJECTED", f"{field} exceeds size limit")
    return value


def _key(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isascii():
        raise WorkspaceError("VALIDATION_FAILED", "invalid idempotency key")
    return value


def _version(value: Any) -> None:
    if type(value) is not int or value < 0:
        raise WorkspaceError("VALIDATION_FAILED", "invalid expected version")


def _ttl(value: Any) -> None:
    if type(value) is not int or not 10 <= value <= 300:
        raise WorkspaceError("VALIDATION_FAILED", "lease must be between 10 and 300 seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_reference TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS principals (
    principal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL,
    provider_kind TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('agent','operator')),
    credential_hash TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    title TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('Ready','InProgress','Blocked','Review','Done','Cancelled')),
    version INTEGER NOT NULL DEFAULT 0,
    review_required INTEGER NOT NULL DEFAULT 1,
    blocked_reason TEXT,
    created_by TEXT NOT NULL REFERENCES principals(principal_id),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_leases (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    lease_id TEXT NOT NULL,
    holder_session_id TEXT REFERENCES sessions(session_id),
    expires_at REAL NOT NULL,
    fence_token INTEGER NOT NULL CHECK(fence_token > 0)
);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    sender_id TEXT NOT NULL REFERENCES principals(principal_id),
    recipient_id TEXT NOT NULL REFERENCES principals(principal_id),
    body TEXT NOT NULL,
    reply_to TEXT REFERENCES messages(message_id),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_id, seq);
CREATE TABLE IF NOT EXISTS receipts (
    message_id TEXT PRIMARY KEY REFERENCES messages(message_id),
    recipient_id TEXT NOT NULL REFERENCES principals(principal_id),
    read_at REAL,
    read_event_id INTEGER,
    acknowledged_at REAL,
    ack_event_id INTEGER
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    actor_id TEXT NOT NULL REFERENCES principals(principal_id),
    action TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    occurred_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_events(project_id, event_id);
CREATE TABLE IF NOT EXISTS review_submissions (
    submission_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    author_id TEXT NOT NULL REFERENCES principals(principal_id),
    author_session_id TEXT NOT NULL REFERENCES sessions(session_id),
    handoff_summary TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','changes_requested')),
    submitted_at REAL NOT NULL,
    decided_by TEXT REFERENCES principals(principal_id),
    decision_reason TEXT,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_review_task ON review_submissions(task_id, submitted_at);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    author_id TEXT NOT NULL REFERENCES principals(principal_id),
    lease_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('test_report','commit','diff','manual_check')),
    reference TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('passed','failed','not_applicable')),
    note TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'submitted'
      CHECK(verification_status IN ('submitted','verified','rejected')),
    verifier_id TEXT REFERENCES principals(principal_id),
    verification_method TEXT,
    verification_note TEXT,
    verified_at REAL,
    submission_id TEXT REFERENCES review_submissions(submission_id),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_submission ON evidence(submission_id);
CREATE INDEX IF NOT EXISTS idx_evidence_lease ON evidence(task_id,lease_id,submission_id);
CREATE TABLE IF NOT EXISTS idempotency (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    request_key TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    PRIMARY KEY(project_id, principal_id, request_key)
);
"""


_KNOWLEDGE_SCHEMA = """
CREATE TABLE knowledge_decisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    statement TEXT NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected','superseded')),
    version INTEGER NOT NULL DEFAULT 0,
    proposed_by TEXT NOT NULL REFERENCES principals(principal_id),
    proposed_at REAL NOT NULL,
    decided_by TEXT REFERENCES principals(principal_id),
    decided_at REAL,
    decision_note TEXT,
    supersedes_decision_id TEXT REFERENCES knowledge_decisions(decision_id),
    superseded_by TEXT REFERENCES knowledge_decisions(decision_id),
    superseded_event_id INTEGER,
    proposed_event_id INTEGER,
    decision_event_id INTEGER
);
CREATE INDEX idx_decisions_project_status_seq ON knowledge_decisions(project_id,status,seq);
CREATE TABLE knowledge_sources (
    decision_id TEXT NOT NULL REFERENCES knowledge_decisions(decision_id),
    ordinal INTEGER NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('task','message','evidence','decision','external')),
    source_ref TEXT NOT NULL,
    PRIMARY KEY(decision_id, ordinal),
    UNIQUE(decision_id, source_kind, source_ref)
);
"""


# Memory is agent-reported evidence, NEVER approved brain knowledge.
# The FTS virtual table stores searchable text locally; no embeddings or external calls.
_MEMORY_SCHEMA = """
CREATE TABLE memory_entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    author_id TEXT NOT NULL REFERENCES principals(principal_id),
    task_id TEXT REFERENCES tasks(task_id),
    kind TEXT NOT NULL CHECK(kind IN ('observation','finding','failed_attempt','lesson')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','superseded','retracted')),
    created_at REAL NOT NULL,
    expires_at REAL,
    supersedes_memory_id TEXT REFERENCES memory_entries(memory_id),
    superseded_by TEXT REFERENCES memory_entries(memory_id),
    invalidated_by TEXT REFERENCES principals(principal_id),
    invalidated_at REAL,
    invalidation_reason TEXT,
    recorded_event_id INTEGER,
    invalidation_event_id INTEGER
);
CREATE INDEX idx_memory_project_seq ON memory_entries(project_id,status,seq);
CREATE TABLE memory_sources (
    memory_id TEXT NOT NULL REFERENCES memory_entries(memory_id),
    ordinal INTEGER NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('task','message','evidence','decision','external')),
    source_ref TEXT NOT NULL,
    PRIMARY KEY(memory_id, ordinal),
    UNIQUE(memory_id, source_kind, source_ref)
);
CREATE VIRTUAL TABLE memory_fts USING fts5(
    title, content, content='memory_entries', content_rowid='seq',
    tokenize='unicode61 remove_diacritics 2'
);
"""

# M2.3 stores structured, task-scoped, immutable snapshots. A handoff grants
# read permission only; it never transfers task ownership or resumes a provider.
_CONTEXT_SCHEMA = """
CREATE TABLE context_snapshots (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    author_id TEXT NOT NULL REFERENCES principals(principal_id),
    author_session_id TEXT NOT NULL REFERENCES sessions(session_id),
    lease_id TEXT NOT NULL,
    fence_token INTEGER NOT NULL,
    summary TEXT NOT NULL,
    next_steps TEXT NOT NULL,
    blockers TEXT,
    changed_paths_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    created_event_id INTEGER
);
CREATE INDEX idx_context_task_seq ON context_snapshots(project_id,task_id,seq);
CREATE TABLE context_sources (
    snapshot_id TEXT NOT NULL REFERENCES context_snapshots(snapshot_id),
    ordinal INTEGER NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN
        ('task','message','evidence','decision','memory','external')),
    source_ref TEXT NOT NULL,
    PRIMARY KEY(snapshot_id,ordinal),
    UNIQUE(snapshot_id,source_kind,source_ref)
);
CREATE TABLE context_handoffs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id TEXT NOT NULL UNIQUE,
    snapshot_id TEXT NOT NULL REFERENCES context_snapshots(snapshot_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    sender_id TEXT NOT NULL REFERENCES principals(principal_id),
    recipient_id TEXT NOT NULL REFERENCES principals(principal_id),
    action_required TEXT NOT NULL,
    created_at REAL NOT NULL,
    created_event_id INTEGER,
    UNIQUE(snapshot_id,recipient_id)
);
CREATE INDEX idx_context_handoffs_inbox ON context_handoffs(recipient_id,seq);
"""


def _context_paths(paths: list | None) -> list[str]:
    if paths is None:
        return []
    if not isinstance(paths,list):
        raise WorkspaceError('VALIDATION_FAILED', 'paths must be a list')
    if len(paths)>16:
        raise WorkspaceError('PAYLOAD_REJECTED','more than 16 paths')
    seen=set()
    for value in paths:
        _text(value,'changed path',256)
        if (value in seen or value.startswith(('/', '~')) or '\\' in value
                or ':' in value or any(ord(char)<32 for char in value)
                or any(part in ('.','..','') for part in value.split('/'))):
            raise WorkspaceError('VALIDATION_FAILED','invalid relative path metadata')
        seen.add(value)
    return paths


_MEMORY_KINDS = frozenset(('observation', 'finding', 'failed_attempt', 'lesson'))


def _memory_preview(value: str, limit: int) -> tuple[str, bool]:
    raw = value.encode('utf-8')
    if len(raw) <= limit:
        return value, False
    return raw[:limit].decode('utf-8', errors='ignore'), True


def _memory_match_query(query: str) -> str:
    _text(query, 'search query', 512)
    tokens = re.findall(r'[^\W_]+', query, flags=re.UNICODE)
    if not tokens or len(tokens) > 12:
        raise WorkspaceError('VALIDATION_FAILED', 'search query requires 1-12 words')
    # Wrap every term as an FTS phrase to stop user-supplied syntax/operators.
    return ' AND '.join('"' + token.replace('"', '') + '"' for token in tokens)


class Workspace:
    SESSION_TTL_SECONDS = 12 * 60 * 60
    SCHEMA_VERSION = 5

    # A version-zero M1.2 database has these tables and columns, but no
    # user_version marker. Validate before marking it migrated; never repair
    # partial/unknown files by quietly adding tables.
    _LEGACY_COLUMNS = {
        "projects": ("project_id", "name", "root_reference", "created_at"),
        "principals": ("principal_id", "project_id", "role", "credential_hash", "enabled"),
        "sessions": ("session_id", "principal_id", "token_hash", "revoked", "expires_at"),
        "tasks": ("task_id", "project_id", "status", "version"),
        "task_leases": ("task_id", "holder_session_id", "expires_at", "fence_token"),
        "messages": ("message_id", "sender_id", "recipient_id", "body"),
        "receipts": ("message_id", "recipient_id"),
        "audit_events": ("event_id", "project_id", "actor_id"),
        "review_submissions": ("submission_id", "task_id", "status"),
        "evidence": ("evidence_id", "task_id", "verification_status"),
        "idempotency": ("project_id", "principal_id", "request_key"),
    }

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.db_path = Path(db_path).expanduser().absolute()
        self.clock = clock
        self._failpoint: str | None = None
        parent_existed = self.db_path.parent.exists()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not parent_existed and os.name == "posix":
            self.db_path.parent.chmod(0o700)
        if self.db_path.is_symlink():
            raise WorkspaceError("UNSAFE_STORAGE", "database path must not be a symlink")
        try:
            # Create the DB with private permissions *before* SQLite opens it.
            fd = os.open(self.db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        if not self.db_path.is_file() or self.db_path.is_symlink():
            raise WorkspaceError("UNSAFE_STORAGE", "database path must be a regular file")
        if os.name == "posix":
            self.db_path.chmod(0o600)
        self._initialize_schema()

    @classmethod
    def _validate_legacy_tables(cls, db):
        for table, required in cls._LEGACY_COLUMNS.items():
            # Table names are hardcoded, not passed by an external caller.
            rows = db.execute(f'PRAGMA table_info("{table}")').fetchall()
            if not rows or not set(required).issubset({row[1] for row in rows}):
                raise WorkspaceError("UNSUPPORTED_SCHEMA", f"missing/incompatible table: {table}")

    def _initialize_schema(self) -> None:
        # Serialize startup migrations with all other writers.
        with self._transaction() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION or version < 0:
                raise WorkspaceError("UNSUPPORTED_SCHEMA", "database is from an unknown version")
            names = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if version == 0 and not names:
                # Execute statements individually: executescript() implicitly
                # commits an active transaction and would break migration atomicity.
                for statement in _SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
            else:
                self._validate_legacy_tables(db)
            if version == 2:
                required = db.execute("""SELECT version FROM schema_migrations
                    WHERE version IN (1,2) ORDER BY version""").fetchall() if "schema_migrations" in names else []
                if [r[0] for r in required] != [1,2]:
                    raise WorkspaceError("UNSUPPORTED_SCHEMA", "incomplete M1.3 migration history")
            if version < 3:
                # A partially migrated schema must never be silently repaired.
                known = {r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'knowledge_%'")}
                if known:
                    raise WorkspaceError("UNSUPPORTED_SCHEMA", "partial knowledge migration")
                for statement in _KNOWLEDGE_SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
            else:
                for table, required in {
                    "knowledge_decisions": ("decision_id", "project_id", "status", "version",
                                            "supersedes_decision_id", "decision_event_id", "superseded_event_id"),
                    "knowledge_sources": ("decision_id", "ordinal", "source_kind", "source_ref")
                }.items():
                    columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
                    if not set(required).issubset(columns):
                        raise WorkspaceError("UNSUPPORTED_SCHEMA", f"missing knowledge table: {table}")
            # Version 3 is the only supported direct M2.1 -> M2.2 upgrade.
            if version == 3:
                required = db.execute("""SELECT version FROM schema_migrations
                    WHERE version IN (1,2,3) ORDER BY version""").fetchall() if 'schema_migrations' in names else []
                if [r[0] for r in required] != [1,2,3]:
                    raise WorkspaceError('UNSUPPORTED_SCHEMA', 'incomplete M2.1 migration history')
            if version < 4:
                existing = {r[0] for r in db.execute("""SELECT name FROM sqlite_master
                    WHERE name LIKE 'memory_%'""")}
                if existing:
                    raise WorkspaceError('UNSUPPORTED_SCHEMA', 'partial memory migration')
                for statement in _MEMORY_SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
            else:
                for table, required in {
                    'memory_entries': ('memory_id', 'project_id', 'author_id', 'status', 'seq',
                                       'expires_at', 'recorded_event_id', 'superseded_by'),
                    'memory_sources': ('memory_id', 'ordinal', 'source_kind', 'source_ref'),
                    'memory_fts': ('title', 'content')
                }.items():
                    columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
                    if not set(required).issubset(columns):
                        raise WorkspaceError('UNSUPPORTED_SCHEMA', f'missing memory table: {table}')
            # A real v4 database carries all four migration records.
            if version == 4:
                history = db.execute("SELECT version FROM schema_migrations WHERE version <= 4 ORDER BY version").fetchall() if 'schema_migrations' in names else []
                if [r[0] for r in history] != [1,2,3,4]:
                    raise WorkspaceError('UNSUPPORTED_SCHEMA', 'incomplete M2.2 migration history')
            if version < 5:
                existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'context_%'")}
                if existing:
                    raise WorkspaceError('UNSUPPORTED_SCHEMA','partial context migration')
                for statement in _CONTEXT_SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
            else:
                for table, required in {
                    'context_snapshots': ('snapshot_id','project_id','task_id','lease_id','created_event_id'),
                    'context_sources': ('snapshot_id','source_kind','source_ref'),
                    'context_handoffs': ('handoff_id','snapshot_id','recipient_id','created_event_id')
                }.items():
                    columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
                    if not set(required).issubset(columns):
                        raise WorkspaceError('UNSUPPORTED_SCHEMA',f'missing context table: {table}')
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise WorkspaceError("CORRUPT_DATABASE")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise WorkspaceError("CORRUPT_DATABASE", "foreign key violations")
            if version < self.SCHEMA_VERSION:
                db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at REAL NOT NULL)""")
                now = self.clock()
                db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1,?,?)",
                           ("M1.2 baseline inferred for legacy DB; new DB baseline", now))
                db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (2,?,?)",
                           ("M1.3 schema tracking and recovery contract", now))
                db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (3,?,?)",
                           ("M2.1 governed knowledge decisions and provenance", now))
                db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (4,?,?)",
                           ("M2.2 sourced memory and bounded FTS5 retrieval", now))
                db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (5,?,?)",
                           ("M2.3 permissioned snapshots and handoff metadata",now))
                db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            elif "schema_migrations" not in names or not db.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (self.SCHEMA_VERSION,)).fetchone():
                raise WorkspaceError("UNSUPPORTED_SCHEMA", "missing migration record")

    def schema_version(self) -> int:
        with self._connect() as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def inject_failpoint(self, name: str | None) -> None:
        """Test-only fault injector; never expose through a client adapter."""
        self._failpoint = name

    def _actor(self, db: sqlite3.Connection, session_token: str):
        if not isinstance(session_token, str):
            raise WorkspaceError("UNAUTHENTICATED")
        row = db.execute("""
            SELECT s.session_id, s.project_id, s.principal_id, p.role, p.name
            FROM sessions s JOIN principals p ON p.principal_id=s.principal_id
            WHERE s.token_hash=? AND s.revoked=0 AND p.enabled=1 AND s.expires_at>?
        """, (_hash(session_token), self.clock())).fetchone()
        if row is None:
            raise WorkspaceError("UNAUTHENTICATED")
        return row

    @staticmethod
    def _operator(actor) -> None:
        if actor["role"] != "operator":
            raise WorkspaceError("FORBIDDEN")

    @staticmethod
    def _event(db, actor, action, kind, subject, payload, now) -> int:
        cursor = db.execute("""
            INSERT INTO audit_events(project_id,actor_id,action,subject_type,subject_id,payload_digest,occurred_at)
            VALUES (?,?,?,?,?,?,?)
        """, (actor["project_id"], actor["principal_id"], action, kind, subject,
              _hash(_canonical(payload)), now))
        return int(cursor.lastrowid)

    def _mutate(self, session_token, action: str, request_key: str, payload: dict, fn):
        _key(request_key)
        digest = _hash(_canonical({"action": action, "payload": payload}))
        with self._transaction() as db:
            actor = self._actor(db, session_token)
            previous = db.execute("""
                SELECT payload_digest,response_json FROM idempotency
                WHERE project_id=? AND principal_id=? AND request_key=?
            """, (actor["project_id"], actor["principal_id"], request_key)).fetchone()
            if previous:
                if previous["payload_digest"] != digest:
                    raise WorkspaceError("IDEMPOTENCY_CONFLICT")
                return json.loads(previous["response_json"])
            result = fn(db, actor, self.clock())
            db.execute("""
                INSERT INTO idempotency(project_id,principal_id,request_key,payload_digest,response_json)
                VALUES (?,?,?,?,?)
            """, (actor["project_id"], actor["principal_id"], request_key, digest, _canonical(result)))
            return result

    def bootstrap(self, name: str, root_reference: str | Path) -> dict:
        """One-time local operator action. Never expose through MCP or agent tools."""
        _text(name, "project name", 120)
        root = Path(root_reference).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise WorkspaceError("VALIDATION_FAILED", "project root is not a directory")
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
                raise WorkspaceError("ALREADY_INITIALIZED")
            now = self.clock()
            project_id, operator_id = _uuid(), _uuid()
            credential = _secret("awc_")
            db.execute("INSERT INTO projects VALUES (?,?,?,?)", (project_id, name, str(root), now))
            db.execute("INSERT INTO principals VALUES (?,?,?,?,?,?,1)",
                       (operator_id, project_id, "local-operator", "human", "operator", _hash(credential)))
            operator = {"project_id": project_id, "principal_id": operator_id}
            self._event(db, operator, "project.bootstrapped", "project", project_id, {"name": name}, now)
            return {"project_id": project_id, "operator_id": operator_id, "operator_credential": credential}

    def open_session(self, credential: str) -> dict:
        if not isinstance(credential, str):
            raise WorkspaceError("UNAUTHENTICATED")
        with self._transaction() as db:
            principal = db.execute("""SELECT * FROM principals
                WHERE credential_hash=? AND enabled=1""", (_hash(credential),)).fetchone()
            if principal is None:
                raise WorkspaceError("UNAUTHENTICATED")
            token, sid, now = _secret("aws_"), _uuid(), self.clock()
            expires = now + self.SESSION_TTL_SECONDS
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,0)",
                       (sid, principal["project_id"], principal["principal_id"], _hash(token), now, expires))
            actor = {"project_id": principal["project_id"], "principal_id": principal["principal_id"]}
            self._event(db, actor, "session.opened", "session", sid, {"session_id": sid}, now)
            return {"session_id": sid, "agent_id": principal["principal_id"], "role": principal["role"],
                    "session_token": token, "expires_at": expires}

    def session_info(self, session_token: str) -> dict:
        """Return only the currently authenticated session's metadata."""
        with self._connect() as db:
            actor = self._actor(db, session_token)
            row = db.execute("SELECT created_at,expires_at FROM sessions WHERE session_id=?",
                             (actor["session_id"],)).fetchone()
            return {"session_id": actor["session_id"], "agent_id": actor["principal_id"],
                    "role": actor["role"], "created_at": row["created_at"],
                    "expires_at": row["expires_at"]}

    def _retire_sessions(self, db, actor, session_ids: list[str], now: float) -> int:
        """Revoke selected sessions and atomically release their leased tasks."""
        released = 0
        for sid in session_ids:
            for row in db.execute("""SELECT t.task_id FROM tasks t
                JOIN task_leases l ON t.task_id=l.task_id
                WHERE t.project_id=? AND t.status='InProgress' AND l.holder_session_id=?""",
                (actor["project_id"], sid)).fetchall():
                tid = row["task_id"]
                db.execute("UPDATE task_leases SET holder_session_id=NULL,expires_at=0 WHERE task_id=?", (tid,))
                db.execute("UPDATE tasks SET status='Ready',version=version+1 WHERE task_id=?", (tid,))
                self._event(db, actor, "task.lease_revoked", "task", tid,
                            {"session_id": sid}, now)
                released += 1
            db.execute("UPDATE sessions SET revoked=1 WHERE session_id=?", (sid,))
        if self._failpoint == "after_session_retirement_write":
            raise RuntimeError("Injected failure after session and lease writes")
        return released

    def revoke_session(self, operator_token: str, session_id: str,
                       request_key: str, reason: str) -> dict:
        _text(session_id, "session id", 128)
        _text(reason, "revocation reason", 2048)
        payload = {"session_id": session_id, "reason": reason}

        def run(db, actor, now):
            self._operator(actor)
            row = db.execute("SELECT * FROM sessions WHERE session_id=? AND project_id=?",
                             (session_id, actor["project_id"])).fetchone()
            if row is None:
                raise WorkspaceError("NOT_FOUND")
            if row["revoked"]:
                raise WorkspaceError("INVALID_TRANSITION")
            released = self._retire_sessions(db, actor, [session_id], now)
            event = self._event(db, actor, "session.revoked", "session", session_id,
                                {"reason": reason, "released_task_count": released}, now)
            return {"session_id": session_id, "released_task_count": released, "event_id": event}

        return self._mutate(operator_token, "session.revoke", request_key, payload, run)

    def close_session(self, session_token: str) -> dict:
        """Self-logout. Not idempotently replayable after the token is revoked."""
        with self._transaction() as db:
            actor = self._actor(db, session_token)
            now = self.clock()
            released = self._retire_sessions(db, actor, [actor["session_id"]], now)
            event = self._event(db, actor, "session.closed", "session", actor["session_id"],
                                {"released_task_count": released}, now)
            return {"session_id": actor["session_id"], "released_task_count": released, "event_id": event}

    def disable_agent(self, operator_token: str, agent_id: str,
                      request_key: str, reason: str) -> dict:
        _text(agent_id, "agent id", 128)
        _text(reason, "disable reason", 2048)
        payload = {"agent_id": agent_id, "reason": reason}

        def run(db, actor, now):
            self._operator(actor)
            principal = db.execute("SELECT * FROM principals WHERE principal_id=? AND project_id=?",
                                   (agent_id, actor["project_id"])).fetchone()
            if principal is None:
                raise WorkspaceError("NOT_FOUND")
            if principal["role"] != "agent":
                raise WorkspaceError("FORBIDDEN")
            if not principal["enabled"]:
                raise WorkspaceError("INVALID_TRANSITION")
            sessions = [r["session_id"] for r in db.execute(
                "SELECT session_id FROM sessions WHERE principal_id=? AND revoked=0", (agent_id,))]
            released = self._retire_sessions(db, actor, sessions, now)
            db.execute("UPDATE principals SET enabled=0 WHERE principal_id=?", (agent_id,))
            event = self._event(db, actor, "agent.disabled", "agent", agent_id,
                                {"reason": reason, "revoked_session_count": len(sessions)}, now)
            return {"agent_id": agent_id, "revoked_session_count": len(sessions),
                    "released_task_count": released, "event_id": event}

        return self._mutate(operator_token, "agent.disable", request_key, payload, run)

    def rotate_agent_credential(self, operator_token: str, agent_id: str) -> dict:
        """One-shot secret delivery, deliberately NOT cached in idempotency storage.

        The operator must persist the returned credential securely; retries
        generate a NEW credential and revoke prior sessions. No secret or its
        plaintext echo is stored in the audit/idempotency tables.
        """
        _text(agent_id, "agent id", 128)
        with self._transaction() as db:
            actor = self._actor(db, operator_token)
            self._operator(actor)
            principal = db.execute("SELECT * FROM principals WHERE principal_id=? AND project_id=?",
                                   (agent_id, actor["project_id"])).fetchone()
            if principal is None:
                raise WorkspaceError("NOT_FOUND")
            if principal["role"] != "agent" or not principal["enabled"]:
                raise WorkspaceError("FORBIDDEN")
            new_secret = _secret("awc_")
            sessions = [r["session_id"] for r in db.execute(
                "SELECT session_id FROM sessions WHERE principal_id=? AND revoked=0", (agent_id,))]
            released = self._retire_sessions(db, actor, sessions, self.clock())
            db.execute("UPDATE principals SET credential_hash=? WHERE principal_id=?",
                       (_hash(new_secret), agent_id))
            event = self._event(db, actor, "agent.credential_rotated", "agent", agent_id,
                                {"revoked_session_count": len(sessions)}, self.clock())
            return {"agent_id": agent_id, "credential": new_secret,
                    "revoked_session_count": len(sessions),
                    "released_task_count": released, "event_id": event}

    def recover_abandoned_tasks(self, operator_token: str, request_key: str) -> dict:
        """Explicit, audited recovery; never force-release healthy leases."""
        payload = {"operation": "recover-abandoned", "policy_version": 1}

        def run(db, actor, now):
            self._operator(actor)
            rows = db.execute("""SELECT t.task_id FROM tasks t
                LEFT JOIN task_leases l ON l.task_id=t.task_id
                LEFT JOIN sessions s ON s.session_id=l.holder_session_id
                LEFT JOIN principals p ON p.principal_id=s.principal_id
                WHERE t.project_id=? AND t.status='InProgress' AND (
                  l.task_id IS NULL OR l.holder_session_id IS NULL OR
                  l.expires_at<=? OR s.session_id IS NULL OR
                  s.revoked=1 OR s.expires_at<=? OR p.enabled=0)
                ORDER BY t.task_id LIMIT 100""",
                (actor["project_id"], now, now)).fetchall()
            ids = [r["task_id"] for r in rows]
            for tid in ids:
                db.execute("UPDATE task_leases SET holder_session_id=NULL,expires_at=0 WHERE task_id=?", (tid,))
                db.execute("UPDATE tasks SET status='Ready',version=version+1 WHERE task_id=?", (tid,))
                self._event(db, actor, "task.recovered", "task", tid, {"reason": "abandoned"}, now)
            if self._failpoint == "after_recovery_write":
                raise RuntimeError("Injected failure after recovery writes")
            event = self._event(db, actor, "workspace.recovery_completed", "project",
                                actor["project_id"], {"count": len(ids)}, now)
            return {"recovered_task_ids": ids, "recovered_task_count": len(ids),
                    "batch_limit": 100, "event_id": event}

        return self._mutate(operator_token, "workspace.recover_abandoned", request_key, payload, run)

    def enroll_agent(self, operator_token: str, name: str, provider_kind: str) -> dict:
        _text(name, "agent name", 120)
        _text(provider_kind, "provider kind", 80)
        with self._transaction() as db:
            actor = self._actor(db, operator_token)
            self._operator(actor)
            agent_id, credential = _uuid(), _secret("awc_")
            try:
                db.execute("INSERT INTO principals VALUES (?,?,?,?,?,?,1)",
                           (agent_id, actor["project_id"], name, provider_kind, "agent", _hash(credential)))
            except sqlite3.IntegrityError:
                raise WorkspaceError("VALIDATION_FAILED", "agent name already exists") from None
            event = self._event(db, actor, "agent.enrolled", "agent", agent_id,
                                {"agent_id": agent_id, "name": name}, self.clock())
            return {"agent_id": agent_id, "credential": credential, "event_id": event}

    @staticmethod
    def _load_task(db, project_id, task_id):
        task = db.execute("""SELECT t.*, l.lease_id, l.holder_session_id, l.expires_at,
            l.fence_token, s.principal_id AS owner_agent_id
            FROM tasks t LEFT JOIN task_leases l ON t.task_id=l.task_id
            LEFT JOIN sessions s ON l.holder_session_id=s.session_id
            WHERE t.task_id=? AND t.project_id=?""", (task_id, project_id)).fetchone()
        if task is None:
            raise WorkspaceError("NOT_FOUND")
        return task

    @staticmethod
    def _task_view(t):
        return {"task_id": t["task_id"], "title": t["title"], "acceptance_criteria": t["acceptance_criteria"],
                "status": t["status"], "version": t["version"], "review_required": bool(t["review_required"]),
                "owner_agent_id": t["owner_agent_id"], "blocked_reason": t["blocked_reason"]}

    def create_task(self, session_token: str, title: str, acceptance_criteria: str, request_key: str) -> dict:
        _text(title, "title", 256)
        _text(acceptance_criteria, "acceptance criteria", 4096)
        payload = {"title": title, "acceptance_criteria": acceptance_criteria}

        def run(db, actor, now):
            self._operator(actor)
            task_id = _uuid()
            db.execute("""INSERT INTO tasks(task_id,project_id,title,acceptance_criteria,status,created_by,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (task_id, actor["project_id"], title, acceptance_criteria, "Ready", actor["principal_id"], now))
            event = self._event(db, actor, "task.created", "task", task_id, payload, now)
            return {"task_id": task_id, "status": "Ready", "version": 0, "event_id": event}

        return self._mutate(session_token, "task.create", request_key, payload, run)

    def get_task(self, session_token: str, task_id: str) -> dict:
        with self._connect() as db:
            actor = self._actor(db, session_token)
            return self._task_view(self._load_task(db, actor["project_id"], task_id))

    def list_tasks(self, session_token: str, limit: int = 100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError("VALIDATION_FAILED")
        with self._connect() as db:
            actor = self._actor(db, session_token)
            ids = db.execute("SELECT task_id FROM tasks WHERE project_id=? ORDER BY created_at,task_id LIMIT ?",
                             (actor["project_id"], limit)).fetchall()
            return [self._task_view(self._load_task(db, actor["project_id"], r["task_id"])) for r in ids]

    @staticmethod
    def _check_version(t, expected_version):
        if t["version"] != expected_version:
            raise WorkspaceError("VERSION_CONFLICT")

    @staticmethod
    def _check_lease(t, actor, fence_token, now):
        if type(fence_token) is not int or t["fence_token"] != fence_token or t["holder_session_id"] != actor["session_id"]:
            raise WorkspaceError("STALE_FENCE")
        if t["expires_at"] <= now:
            raise WorkspaceError("LEASE_EXPIRED")

    def claim_task(self, session_token: str, task_id: str, expected_version: int,
                   request_key: str, lease_seconds: int = 60) -> dict:
        _version(expected_version)
        _ttl(lease_seconds)
        payload = {"task_id": task_id, "expected_version": expected_version, "lease_seconds": lease_seconds}

        def run(db, actor, now):
            if actor["role"] != "agent":
                raise WorkspaceError("FORBIDDEN", "only agent principals can claim tasks")
            t = self._load_task(db, actor["project_id"], task_id)
            self._check_version(t, expected_version)
            if t["status"] not in ("Ready", "InProgress"):
                raise WorkspaceError("INVALID_TRANSITION")
            if t["holder_session_id"] is not None and t["expires_at"] > now:
                raise WorkspaceError("LEASE_HELD")
            if t["status"] == "InProgress" and t["holder_session_id"] is None:
                raise WorkspaceError("INVALID_TRANSITION")
            fence = (t["fence_token"] or 0) + 1
            lease_id, expires = _uuid(), now + lease_seconds
            db.execute("UPDATE tasks SET status='InProgress',version=version+1 WHERE task_id=?", (task_id,))
            db.execute("""INSERT INTO task_leases(task_id,lease_id,holder_session_id,expires_at,fence_token)
                VALUES (?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET lease_id=excluded.lease_id,
                holder_session_id=excluded.holder_session_id,expires_at=excluded.expires_at,
                fence_token=excluded.fence_token""", (task_id, lease_id, actor["session_id"], expires, fence))
            if self._failpoint == "after_claim_write":
                raise RuntimeError("Injected failure after task and lease changes")
            event = self._event(db, actor, "task.claimed", "task", task_id,
                                {"lease_id": lease_id, "fence_token": fence}, now)
            return {"task_id": task_id, "lease_id": lease_id, "fence_token": fence,
                    "expires_at": expires, "version": expected_version + 1, "event_id": event}

        return self._mutate(session_token, "task.claim", request_key, payload, run)

    def renew_task(self, session_token: str, task_id: str, expected_version: int,
                   fence_token: int, request_key: str, lease_seconds: int = 60) -> dict:
        _version(expected_version)
        _ttl(lease_seconds)
        payload = {"task_id": task_id, "expected_version": expected_version,
                   "fence_token": fence_token, "lease_seconds": lease_seconds}

        def run(db, actor, now):
            t = self._load_task(db, actor["project_id"], task_id)
            self._check_lease(t, actor, fence_token, now)
            self._check_version(t, expected_version)
            if t["status"] != "InProgress":
                raise WorkspaceError("INVALID_TRANSITION")
            # Renewal extends the existing expiration, so even a prompt renewal is useful.
            expires = max(now, t["expires_at"]) + lease_seconds
            db.execute("UPDATE task_leases SET expires_at=? WHERE task_id=?", (expires, task_id))
            db.execute("UPDATE tasks SET version=version+1 WHERE task_id=?", (task_id,))
            event = self._event(db, actor, "task.lease_renewed", "task", task_id, payload, now)
            return {"task_id": task_id, "fence_token": fence_token, "expires_at": expires,
                    "version": expected_version + 1, "event_id": event}

        return self._mutate(session_token, "task.renew", request_key, payload, run)

    def _end_lease(self, session_token, task_id, expected_version, fence_token,
                   request_key, action, target_status, reason=None):
        _version(expected_version)
        if reason is not None:
            _text(reason, "reason", 2048)
        payload = {"task_id": task_id, "expected_version": expected_version,
                   "fence_token": fence_token, "reason": reason}

        def run(db, actor, now):
            t = self._load_task(db, actor["project_id"], task_id)
            self._check_lease(t, actor, fence_token, now)
            self._check_version(t, expected_version)
            if t["status"] != "InProgress":
                raise WorkspaceError("INVALID_TRANSITION")
            db.execute("UPDATE task_leases SET holder_session_id=NULL,expires_at=0 WHERE task_id=?", (task_id,))
            db.execute("UPDATE tasks SET status=?,blocked_reason=?,version=version+1 WHERE task_id=?",
                       (target_status, reason, task_id))
            event = self._event(db, actor, action, "task", task_id, payload, now)
            return {"task_id": task_id, "status": target_status, "version": expected_version + 1,
                    "event_id": event}

        return self._mutate(session_token, action, request_key, payload, run)

    def release_task(self, session_token: str, task_id: str, expected_version: int,
                     fence_token: int, request_key: str) -> dict:
        return self._end_lease(session_token, task_id, expected_version, fence_token,
                               request_key, "task.released", "Ready")

    def block_task(self, session_token: str, task_id: str, expected_version: int,
                   fence_token: int, request_key: str, reason: str) -> dict:
        return self._end_lease(session_token, task_id, expected_version, fence_token,
                               request_key, "task.blocked", "Blocked", reason)

    def requeue_task(self, session_token: str, task_id: str, expected_version: int, request_key: str) -> dict:
        _version(expected_version)
        payload = {"task_id": task_id, "expected_version": expected_version}

        def run(db, actor, now):
            self._operator(actor)
            t = self._load_task(db, actor["project_id"], task_id)
            self._check_version(t, expected_version)
            if t["status"] != "Blocked":
                raise WorkspaceError("INVALID_TRANSITION")
            db.execute("UPDATE tasks SET status='Ready',blocked_reason=NULL,version=version+1 WHERE task_id=?", (task_id,))
            event = self._event(db, actor, "task.requeued", "task", task_id, payload, now)
            return {"task_id": task_id, "status": "Ready", "version": expected_version + 1, "event_id": event}

        return self._mutate(session_token, "task.requeue", request_key, payload, run)

    def add_evidence(self, session_token: str, task_id: str, expected_version: int,
                     fence_token: int, request_key: str, kind: str, reference: str,
                     result: str, note: str) -> dict:
        """Record a reference, NOT proof the referenced test or artifact is genuine."""
        _version(expected_version)
        if kind not in ("test_report", "commit", "diff", "manual_check"):
            raise WorkspaceError("VALIDATION_FAILED", "unknown evidence kind")
        if (kind == "test_report" and result not in ("passed", "failed")) or (kind != "test_report" and result != "not_applicable"):
            raise WorkspaceError("VALIDATION_FAILED", "invalid reported result for evidence kind")
        _text(reference, "evidence reference", 1024)
        _text(note, "evidence note", 2048)
        payload = {"task_id": task_id, "expected_version": expected_version,
                   "fence_token": fence_token, "kind": kind, "reference": reference,
                   "result": result, "note": note}

        def run(db, actor, now):
            t = self._load_task(db, actor["project_id"], task_id)
            self._check_lease(t, actor, fence_token, now)
            self._check_version(t, expected_version)
            if t["status"] != "InProgress":
                raise WorkspaceError("INVALID_TRANSITION")
            evidence_id = _uuid()
            db.execute("""INSERT INTO evidence
                (evidence_id,task_id,author_id,lease_id,kind,reference,result,note,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (evidence_id, task_id, actor["principal_id"], t["lease_id"],
                 kind, reference, result, note, now))
            db.execute("UPDATE tasks SET version=version+1 WHERE task_id=?", (task_id,))
            event = self._event(db, actor, "evidence.added", "evidence", evidence_id,
                                {"task_id": task_id, "kind": kind, "result": result}, now)
            return {"evidence_id": evidence_id, "task_id": task_id, "verification_status": "submitted",
                    "version": expected_version + 1, "event_id": event}

        return self._mutate(session_token, "evidence.add", request_key, payload, run)

    def submit_for_review(self, session_token: str, task_id: str, expected_version: int,
                          fence_token: int, request_key: str, handoff_summary: str) -> dict:
        """Transfer a leased task into Review; handoff is a text summary, not an M2 snapshot."""
        _version(expected_version)
        _text(handoff_summary, "handoff summary", 4096)
        payload = {"task_id": task_id, "expected_version": expected_version,
                   "fence_token": fence_token, "handoff_summary": handoff_summary}

        def run(db, actor, now):
            t = self._load_task(db, actor["project_id"], task_id)
            self._check_lease(t, actor, fence_token, now)
            self._check_version(t, expected_version)
            if t["status"] != "InProgress":
                raise WorkspaceError("INVALID_TRANSITION")
            records = db.execute("""SELECT evidence_id FROM evidence
                WHERE task_id=? AND lease_id=? AND author_id=? AND submission_id IS NULL""",
                (task_id, t["lease_id"], actor["principal_id"])).fetchall()
            if not records:
                raise WorkspaceError("EVIDENCE_REQUIRED")
            submission_id = _uuid()
            db.execute("""INSERT INTO review_submissions
                (submission_id,task_id,author_id,author_session_id,handoff_summary,status,submitted_at)
                VALUES (?,?,?,?,?,'pending',?)""",
                (submission_id, task_id, actor["principal_id"], actor["session_id"], handoff_summary, now))
            db.execute("""UPDATE evidence SET submission_id=?
                WHERE task_id=? AND lease_id=? AND author_id=? AND submission_id IS NULL""",
                (submission_id, task_id, t["lease_id"], actor["principal_id"]))
            # Reviews have no lease owner: rejected reviews return to Ready for a fresh claim.
            db.execute("UPDATE task_leases SET holder_session_id=NULL,expires_at=0 WHERE task_id=?", (task_id,))
            db.execute("UPDATE tasks SET status='Review',version=version+1 WHERE task_id=?", (task_id,))
            event = self._event(db, actor, "task.review_submitted", "task", task_id,
                                {"submission_id": submission_id, "evidence_count": len(records)}, now)
            return {"task_id": task_id, "submission_id": submission_id, "status": "Review",
                    "evidence_count": len(records), "version": expected_version + 1, "event_id": event}

        return self._mutate(session_token, "task.submit_review", request_key, payload, run)

    @staticmethod
    def _last_review(db, task_id):
        return db.execute("""SELECT * FROM review_submissions WHERE task_id=?
            ORDER BY rowid DESC LIMIT 1""", (task_id,)).fetchone()

    def get_review(self, session_token: str, task_id: str) -> dict:
        """Project-scoped current review and its attached evidence records."""
        with self._connect() as db:
            actor = self._actor(db, session_token)
            self._load_task(db, actor["project_id"], task_id)
            review = self._last_review(db, task_id)
            if review is None:
                raise WorkspaceError("NOT_FOUND")
            records = db.execute("""SELECT evidence_id,kind,reference,result,note,
                author_id,verification_status,verifier_id,verification_method,verification_note,
                verified_at,created_at FROM evidence WHERE submission_id=? ORDER BY rowid""",
                (review["submission_id"],)).fetchall()
            return {"task_id": task_id, "submission_id": review["submission_id"],
                    "submitted_by": review["author_id"], "status": review["status"],
                    "handoff_summary": review["handoff_summary"], "submitted_at": review["submitted_at"],
                    "decided_by": review["decided_by"], "decision_reason": review["decision_reason"],
                    "evidence": [dict(row) for row in records]}

    def verify_evidence(self, session_token: str, task_id: str, evidence_id: str,
                        expected_version: int, request_key: str, verdict: str, note: str) -> dict:
        """Operator attests a reference was checked; the core does not run tests."""
        _version(expected_version)
        if verdict not in ("verified", "rejected"):
            raise WorkspaceError("VALIDATION_FAILED")
        _text(note, "verification note", 2048)
        payload = {"task_id": task_id, "evidence_id": evidence_id,
                   "expected_version": expected_version, "verdict": verdict, "note": note}

        def run(db, actor, now):
            self._operator(actor)
            t = self._load_task(db, actor["project_id"], task_id)
            self._check_version(t, expected_version)
            if t["status"] != "Review":
                raise WorkspaceError("INVALID_TRANSITION")
            review = self._last_review(db, task_id)
            if review is None or review["status"] != "pending":
                raise WorkspaceError("INVALID_TRANSITION")
            evidence = db.execute("""SELECT * FROM evidence
                WHERE evidence_id=? AND task_id=? AND submission_id=?""",
                (evidence_id, task_id, review["submission_id"])).fetchone()
            if evidence is None:
                raise WorkspaceError("NOT_FOUND")
            if evidence["author_id"] == actor["principal_id"]:
                raise WorkspaceError("FORBIDDEN")
            if evidence["verification_status"] != "submitted":
                raise WorkspaceError("INVALID_TRANSITION")
            db.execute("""UPDATE evidence SET verification_status=?,verifier_id=?,
                verification_method='operator_attestation',verification_note=?,verified_at=? WHERE evidence_id=?""",
                (verdict, actor["principal_id"], note, now, evidence_id))
            db.execute("UPDATE tasks SET version=version+1 WHERE task_id=?", (task_id,))
            action = "evidence.verified" if verdict == "verified" else "evidence.rejected"
            event = self._event(db, actor, action, "evidence", evidence_id,
                                {"task_id": task_id, "verdict": verdict}, now)
            return {"task_id": task_id, "evidence_id": evidence_id,
                    "verification_status": verdict, "verification_method": "operator_attestation",
                    "version": expected_version + 1, "event_id": event}

        return self._mutate(session_token, "evidence.verify", request_key, payload, run)

    def approve_review(self, session_token: str, task_id: str, expected_version: int,
                       request_key: str, rationale: str) -> dict:
        _version(expected_version)
        _text(rationale, "approval rationale", 2048)
        payload = {"task_id": task_id, "expected_version": expected_version, "rationale": rationale}

        def run(db, actor, now):
            self._operator(actor)
            t = self._load_task(db, actor["project_id"], task_id)
            self._check_version(t, expected_version)
            if t["status"] != "Review":
                raise WorkspaceError("INVALID_TRANSITION")
            review = self._last_review(db, task_id)
            if review is None or review["status"] != "pending":
                raise WorkspaceError("INVALID_TRANSITION")
            if review["author_id"] == actor["principal_id"]:
                raise WorkspaceError("FORBIDDEN")
            records = db.execute("SELECT kind,result,verification_status,verifier_id FROM evidence WHERE submission_id=?",
                                 (review["submission_id"],)).fetchall()
            if not records or any(e["verification_status"] != "verified" for e in records):
                raise WorkspaceError("EVIDENCE_UNVERIFIED")
            if any(e["verifier_id"] == review["author_id"] for e in records):
                raise WorkspaceError("FORBIDDEN")
            # A verified report may truthfully describe a failed suite; that is
            # useful review evidence but never sufficient for completion.
            test_reports = [e for e in records if e["kind"] == "test_report"]
            if not test_reports or any(e["result"] != "passed" for e in test_reports):
                raise WorkspaceError("PASSING_TEST_REQUIRED")
            db.execute("UPDATE tasks SET status='Done',version=version+1 WHERE task_id=?", (task_id,))
            db.execute("""UPDATE review_submissions SET status='approved',decided_by=?,
                decision_reason=?,decided_at=? WHERE submission_id=?""",
                (actor["principal_id"], rationale, now, review["submission_id"]))
            if self._failpoint == "before_review_approval_event":
                raise RuntimeError("Injected failure after approval writes")
            event = self._event(db, actor, "task.review_approved", "task", task_id,
                                {"submission_id": review["submission_id"]}, now)
            return {"task_id": task_id, "status": "Done", "version": expected_version + 1,
                    "submission_id": review["submission_id"], "event_id": event}

        return self._mutate(session_token, "task.approve_review", request_key, payload, run)

    def request_changes(self, session_token: str, task_id: str, expected_version: int,
                        request_key: str, reason: str) -> dict:
        """Return rejected work to Ready: new owner must claim a fresh fenced lease."""
        _version(expected_version)
        _text(reason, "change request reason", 2048)
        payload = {"task_id": task_id, "expected_version": expected_version, "reason": reason}

        def run(db, actor, now):
            self._operator(actor)
            t = self._load_task(db, actor["project_id"], task_id)
            self._check_version(t, expected_version)
            if t["status"] != "Review":
                raise WorkspaceError("INVALID_TRANSITION")
            review = self._last_review(db, task_id)
            if review is None or review["status"] != "pending":
                raise WorkspaceError("INVALID_TRANSITION")
            if review["author_id"] == actor["principal_id"]:
                raise WorkspaceError("FORBIDDEN")
            db.execute("""UPDATE review_submissions SET status='changes_requested',decided_by=?,
                decision_reason=?,decided_at=? WHERE submission_id=?""",
                (actor["principal_id"], reason, now, review["submission_id"]))
            db.execute("UPDATE tasks SET status='Ready',version=version+1 WHERE task_id=?", (task_id,))
            event = self._event(db, actor, "task.review_changes_requested", "task", task_id,
                                {"submission_id": review["submission_id"]}, now)
            return {"task_id": task_id, "status": "Ready", "version": expected_version + 1,
                    "event_id": event}

        return self._mutate(session_token, "task.request_changes", request_key, payload, run)

    def finish_task(self, session_token: str, task_id: str, expected_version: int, request_key: str) -> dict:
        """Legacy direct completion stays closed; use approve_review with verified evidence."""
        with self._connect() as db:
            actor = self._actor(db, session_token)
            self._load_task(db, actor["project_id"], task_id)
        raise WorkspaceError("REVIEW_REQUIRED", "M1 does not support completion approvals")

    def send_message(self, session_token: str, recipient_agent_id: str, body: str,
                     request_key: str, *, reply_to: str | None = None,
                     claimed_sender_id: str | None = None) -> dict:
        _text(body, "body", 16384)
        payload = {"recipient_agent_id": recipient_agent_id, "body": body,
                   "reply_to": reply_to, "claimed_sender_id": claimed_sender_id}

        def run(db, actor, now):
            if claimed_sender_id is not None and claimed_sender_id != actor["principal_id"]:
                raise WorkspaceError("FORBIDDEN")
            recipient = db.execute("SELECT principal_id FROM principals WHERE principal_id=? AND project_id=? AND enabled=1",
                                   (recipient_agent_id, actor["project_id"])).fetchone()
            if recipient is None:
                raise WorkspaceError("NOT_FOUND")
            if reply_to is not None:
                referenced = db.execute("SELECT * FROM messages WHERE message_id=? AND project_id=?",
                                        (reply_to, actor["project_id"])).fetchone()
                if referenced is None or actor["principal_id"] not in (referenced["sender_id"], referenced["recipient_id"]):
                    raise WorkspaceError("FORBIDDEN")
            message_id = _uuid()
            row = db.execute("""INSERT INTO messages(message_id,project_id,sender_id,recipient_id,body,reply_to,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (message_id, actor["project_id"], actor["principal_id"], recipient_agent_id, body, reply_to, now))
            db.execute("INSERT INTO receipts(message_id,recipient_id) VALUES (?,?)", (message_id, recipient_agent_id))
            event = self._event(db, actor, "message.sent", "message", message_id,
                                {"message_id": message_id, "recipient_id": recipient_agent_id}, now)
            return {"message_id": message_id, "seq": row.lastrowid, "sender_agent_id": actor["principal_id"],
                    "recipient_agent_id": recipient_agent_id, "delivery_state": "stored", "event_id": event}

        return self._mutate(session_token, "message.send", request_key, payload, run)

    @staticmethod
    def _message_view(m):
        return {"message_id": m["message_id"], "seq": m["seq"], "sender_agent_id": m["sender_id"],
                "recipient_agent_id": m["recipient_id"], "body": m["body"], "reply_to": m["reply_to"],
                "created_at": m["created_at"], "read_at": m["read_at"], "acknowledged_at": m["acknowledged_at"]}

    def inbox(self, session_token: str, *, after_seq: int = 0, limit: int = 100,
              include_acked: bool = False) -> dict:
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError("VALIDATION_FAILED")
        with self._connect() as db:
            actor = self._actor(db, session_token)
            rows = db.execute("""SELECT m.*,r.read_at,r.acknowledged_at FROM messages m
                JOIN receipts r ON m.message_id=r.message_id
                WHERE m.project_id=? AND m.recipient_id=? AND m.seq>?
                    AND (?=1 OR r.acknowledged_at IS NULL)
                ORDER BY m.seq ASC LIMIT ?""",
                (actor["project_id"], actor["principal_id"], after_seq, int(include_acked), limit)).fetchall()
            return {"messages": [self._message_view(m) for m in rows],
                    "next_cursor": rows[-1]["seq"] if rows else after_seq}

    def get_message(self, session_token: str, message_id: str) -> dict:
        with self._connect() as db:
            actor = self._actor(db, session_token)
            m = db.execute("""SELECT m.*,r.read_at,r.acknowledged_at FROM messages m
                JOIN receipts r ON m.message_id=r.message_id
                WHERE m.message_id=? AND m.project_id=?""", (message_id, actor["project_id"])).fetchone()
            if m is None:
                raise WorkspaceError("NOT_FOUND")
            if m["recipient_id"] != actor["principal_id"]:
                raise WorkspaceError("FORBIDDEN")
            return self._message_view(m)

    def _receipt_change(self, session_token: str, message_id: str, request_key: str, change: str) -> dict:
        field, event_column, action = ("acknowledged_at", "ack_event_id", "message.acknowledged") if change == "ack" else ("read_at", "read_event_id", "message.read")
        payload = {"message_id": message_id}

        def run(db, actor, now):
            m = db.execute("""SELECT m.message_id,m.project_id,m.recipient_id,r.* FROM messages m
                JOIN receipts r ON r.message_id=m.message_id WHERE m.message_id=? AND m.project_id=?""",
                (message_id, actor["project_id"])).fetchone()
            if m is None:
                raise WorkspaceError("NOT_FOUND")
            if m["recipient_id"] != actor["principal_id"]:
                raise WorkspaceError("FORBIDDEN")
            if m[field] is not None:
                return {"message_id": message_id, field: m[field], "event_id": m[event_column]}
            event = self._event(db, actor, action, "message", message_id, payload, now)
            if change == "ack":
                # Ack is an explicit act and also implies the message was read.
                db.execute("""UPDATE receipts SET acknowledged_at=?,ack_event_id=?,
                    read_at=COALESCE(read_at,?) WHERE message_id=?""", (now, event, now, message_id))
            else:
                db.execute(f"UPDATE receipts SET {field}=?,{event_column}=? WHERE message_id=?", (now, event, message_id))
            return {"message_id": message_id, field: now, "event_id": event}

        return self._mutate(session_token, action, request_key, payload, run)

    def mark_read(self, session_token: str, message_id: str, request_key: str) -> dict:
        return self._receipt_change(session_token, message_id, request_key, "read")

    def ack_message(self, session_token: str, message_id: str, request_key: str) -> dict:
        return self._receipt_change(session_token, message_id, request_key, "ack")

    def audit_events(self, session_token: str, after_event_id: int = 0, limit: int = 500) -> dict:
        if type(after_event_id) is not int or after_event_id < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise WorkspaceError("VALIDATION_FAILED")
        with self._connect() as db:
            actor = self._actor(db, session_token)
            self._operator(actor)
            rows = db.execute("""SELECT * FROM audit_events WHERE project_id=? AND event_id>?
                ORDER BY event_id LIMIT ?""", (actor["project_id"], after_event_id, limit)).fetchall()
            events = [dict(row) for row in rows]
            return {"events": events, "next_cursor": rows[-1]["event_id"] if rows else after_event_id}

    # M2.1: Decisions are *governed knowledge*, never raw agent memory.
    # No provider transcript or file content is ingested by these methods.

    @staticmethod
    def _knowledge_sources(db, project_id: str, actor_id: str, actor_role: str, source_refs: list) -> list[dict]:
        if not isinstance(source_refs, list) or not 1 <= len(source_refs) <= 8:
            raise WorkspaceError("VALIDATION_FAILED", "1-8 source references required")
        result = []
        seen = set()
        for source in source_refs:
            if not isinstance(source, dict) or set(source) != {"kind", "ref"}:
                raise WorkspaceError("VALIDATION_FAILED", "invalid source reference")
            kind = source["kind"]
            if kind not in ("task", "message", "evidence", "decision", "external"):
                raise WorkspaceError("VALIDATION_FAILED", "unknown source kind")
            ref = _text(source["ref"], "source ref", 2048 if kind == "external" else 128)
            if (kind, ref) in seen:
                raise WorkspaceError("VALIDATION_FAILED", "duplicate source reference")
            seen.add((kind, ref))
            if kind == "external":
                try:
                    parsed = urlsplit(ref)
                    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                            or parsed.password is not None or parsed.fragment or any(c.isspace() or ord(c) < 32 for c in ref)):
                        raise ValueError("not a safe reference")
                    _ = parsed.port  # invalid port must be rejected
                except ValueError:
                    raise WorkspaceError("VALIDATION_FAILED", "external source must be a safe HTTPS URL") from None
            else:
                if kind == "task":
                    query = "SELECT 1 FROM tasks WHERE task_id=? AND project_id=?"
                elif kind == "message":
                    query = "SELECT 1 FROM messages WHERE message_id=? AND project_id=?"
                elif kind == "evidence":
                    query = "SELECT 1 FROM evidence e JOIN tasks t ON t.task_id=e.task_id WHERE e.evidence_id=? AND t.project_id=?"
                else:
                    query = "SELECT 1 FROM knowledge_decisions WHERE decision_id=? AND project_id=? AND status IN ('accepted','superseded')"
                if db.execute(query, (ref, project_id)).fetchone() is None:
                    raise WorkspaceError("NOT_FOUND", "source reference not in project")
                if kind == "message" and actor_role != "operator":
                    visible = db.execute("""SELECT 1 FROM messages
                        WHERE message_id=? AND project_id=? AND (sender_id=? OR recipient_id=?)""",
                        (ref, project_id, actor_id, actor_id)).fetchone()
                    if visible is None:
                        raise WorkspaceError("NOT_FOUND", "source reference not in project")
            result.append({"kind": kind, "ref": ref})
        return result

    @staticmethod
    def _decision_view(db, row) -> dict:
        sources = db.execute("""SELECT source_kind,source_ref FROM knowledge_sources
            WHERE decision_id=? ORDER BY ordinal""", (row["decision_id"],)).fetchall()
        refs = [{"kind": r["source_kind"], "ref": r["source_ref"]} for r in sources]
        return {
            "decision_id": row["decision_id"], "seq": row["seq"],
            "statement": row["statement"], "rationale": row["rationale"],
            "status": row["status"], "version": row["version"],
            "proposed_by": row["proposed_by"], "proposed_at": row["proposed_at"],
            "decided_by": row["decided_by"], "decided_at": row["decided_at"],
            "decision_note": row["decision_note"],
            "supersedes_decision_id": row["supersedes_decision_id"],
            "superseded_by": row["superseded_by"],
            "superseded_event_id": row["superseded_event_id"],
            "source_refs": refs, "proposed_event_id": row["proposed_event_id"],
            "decision_event_id": row["decision_event_id"],
            "external_sources_verified": False if any(r["kind"] == "external" for r in refs) else None,
        }

    def propose_decision(self, session_token: str, statement: str, rationale: str,
                         source_refs: list, request_key: str,
                         *, supersedes_decision_id: str | None = None) -> dict:
        _text(statement, "decision statement", 4096)
        _text(rationale, "decision rationale", 8192)
        if supersedes_decision_id is not None:
            _text(supersedes_decision_id, "supersedes decision id", 128)
        # Validate JSON type before canonicalizing; malformed payloads are domain errors.
        try:
            _canonical(source_refs)
        except (TypeError, ValueError):
            raise WorkspaceError("VALIDATION_FAILED", "invalid source serialization") from None
        # Canonical payload for idempotency.
        payload = {"statement": statement, "rationale": rationale, "source_refs": source_refs,
                   "supersedes_decision_id": supersedes_decision_id}

        def run(db, actor, now):
            refs = self._knowledge_sources(db, actor["project_id"], actor["principal_id"], actor["role"], source_refs)
            if supersedes_decision_id is not None:
                target = db.execute("""SELECT status FROM knowledge_decisions
                    WHERE decision_id=? AND project_id=?""",
                    (supersedes_decision_id, actor["project_id"])).fetchone()
                if target is None:
                    raise WorkspaceError("NOT_FOUND")
                if target["status"] != "accepted":
                    raise WorkspaceError("INVALID_TRANSITION", "superseded decision must be active")
            did = _uuid()
            db.execute("""INSERT INTO knowledge_decisions
                (decision_id,project_id,statement,rationale,status,proposed_by,proposed_at,supersedes_decision_id)
                VALUES (?,?,?,?,'proposed',?,?,?)""",
                (did, actor["project_id"], statement, rationale, actor["principal_id"],
                 now, supersedes_decision_id))
            for idx, ref in enumerate(refs):
                db.execute("INSERT INTO knowledge_sources VALUES (?,?,?,?)",
                           (did, idx, ref["kind"], ref["ref"]))
            event = self._event(db, actor, "knowledge.proposed", "decision", did,
                                {"sources": refs, "supersedes": supersedes_decision_id}, now)
            db.execute("UPDATE knowledge_decisions SET proposed_event_id=? WHERE decision_id=?",
                       (event, did))
            return {"decision_id": did, "status": "proposed", "version": 0, "event_id": event}

        return self._mutate(session_token, "knowledge.propose", request_key, payload, run)

    def decide_decision(self, session_token: str, decision_id: str, expected_version: int,
                        outcome: str, reason: str, request_key: str) -> dict:
        _version(expected_version)
        _text(decision_id, "decision id", 128)
        _text(reason, "decision reason", 4096)
        if outcome not in ("accepted", "rejected"):
            raise WorkspaceError("VALIDATION_FAILED", "invalid decision outcome")
        payload = {"decision_id": decision_id, "expected_version": expected_version,
                   "outcome": outcome, "reason": reason}

        def run(db, actor, now):
            self._operator(actor)
            row = db.execute("SELECT * FROM knowledge_decisions WHERE decision_id=? AND project_id=?",
                             (decision_id, actor["project_id"])).fetchone()
            if row is None:
                raise WorkspaceError("NOT_FOUND")
            if row["version"] != expected_version:
                raise WorkspaceError("VERSION_CONFLICT")
            if row["status"] != "proposed":
                raise WorkspaceError("INVALID_TRANSITION")
            predecessor = row["supersedes_decision_id"]
            if outcome == "accepted" and predecessor:
                old = db.execute("""SELECT * FROM knowledge_decisions
                    WHERE decision_id=? AND project_id=?""",
                    (predecessor, actor["project_id"])).fetchone()
                if old is None or old["status"] != "accepted":
                    raise WorkspaceError("INVALID_TRANSITION", "predecessor is no longer active")
                supersession_event = self._event(db, actor, "knowledge.superseded", "decision", predecessor,
                                                 {"superseded_by": decision_id}, now)
                db.execute("""UPDATE knowledge_decisions SET status='superseded',version=version+1,
                    superseded_by=?,superseded_event_id=? WHERE decision_id=?""",
                    (decision_id, supersession_event, predecessor))
            db.execute("""UPDATE knowledge_decisions SET status=?,version=version+1,
                decided_by=?,decided_at=?,decision_note=? WHERE decision_id=?""",
                (outcome, actor["principal_id"], now, reason, decision_id))
            if self._failpoint == "before_knowledge_approval_event":
                raise RuntimeError("Injected failure after knowledge approval writes")
            event = self._event(db, actor, "knowledge." + outcome, "decision", decision_id,
                                {"outcome": outcome, "supersedes": predecessor}, now)
            db.execute("UPDATE knowledge_decisions SET decision_event_id=? WHERE decision_id=?",
                       (event, decision_id))
            return {"decision_id": decision_id, "status": outcome,
                    "version": expected_version + 1, "event_id": event}

        return self._mutate(session_token, "knowledge.decide", request_key, payload, run)

    def get_decision(self, session_token: str, decision_id: str) -> dict:
        _text(decision_id, "decision id", 128)
        with self._connect() as db:
            actor = self._actor(db, session_token)
            row = db.execute("SELECT * FROM knowledge_decisions WHERE decision_id=? AND project_id=?",
                             (decision_id, actor["project_id"])).fetchone()
            if row is None or (row["status"] in ("proposed", "rejected")
                               and actor["role"] != "operator" and row["proposed_by"] != actor["principal_id"]):
                raise WorkspaceError("NOT_FOUND")
            return self._decision_view(db, row)

    def list_decisions(self, session_token: str, *, status: str = "accepted",
                       after_seq: int = 0, limit: int = 100) -> dict:
        if status not in ("accepted", "superseded", "proposed", "rejected", "all"):
            raise WorkspaceError("VALIDATION_FAILED", "invalid decision status filter")
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError("VALIDATION_FAILED", "invalid pagination")
        with self._connect() as db:
            actor = self._actor(db, session_token)
            if status not in ("accepted", "superseded"):
                self._operator(actor)
            conditions = "" if status == "all" else "AND status=?"
            params = [actor["project_id"], after_seq]
            if status != "all":
                params.append(status)
            params.append(limit + 1)
            rows = db.execute(f"""SELECT * FROM knowledge_decisions WHERE project_id=?
                AND seq>? {conditions} ORDER BY seq LIMIT ?""", params).fetchall()
            return {"decisions": [self._decision_view(db, row) for row in rows[:limit]],
                    "next_cursor": rows[min(len(rows), limit)-1]["seq"] if rows else after_seq,
                    "has_more": len(rows) > limit}

    def knowledge_brief(self, session_token: str, *, limit: int = 20) -> dict:
        """Only active accepted decisions; never drafts or other agents' private sessions."""
        # Default compact summary, with explicit truncation signal.
        if type(limit) is not int or not 1 <= limit <= 50:
            raise WorkspaceError("VALIDATION_FAILED", "invalid brief limit")
        with self._connect() as db:
            actor = self._actor(db, session_token)
            rows = db.execute("""SELECT * FROM knowledge_decisions
                WHERE project_id=? AND status='accepted' ORDER BY seq DESC LIMIT ?""",
                (actor["project_id"], limit + 1)).fetchall()
            previews = []
            for row in rows[:limit]:
                # A brief is a navigation index, not a replacement for the
                # complete decision. Never silently treat truncated text as a rule.
                statement = row["statement"]
                utf8 = statement.encode("utf-8")
                truncated = len(utf8) > 320
                preview = utf8[:320].decode("utf-8", errors="ignore") if truncated else statement
                previews.append({"decision_id": row["decision_id"],
                                 "statement_preview": preview,
                                 "preview_truncated": truncated,
                                 "version": row["version"],
                                 "status": row["status"],
                                 "decided_at": row["decided_at"]})
            return {"decisions": previews, "has_more": len(rows) > limit}


    # M2.2: Memory is separately sourced and agent-reported, never an accepted decision.
    # No native provider transcripts, file crawling, or external source fetching.
    @staticmethod
    def _memory_view(db, row, actor, now) -> dict:
        refs = db.execute("""SELECT source_kind,source_ref FROM memory_sources
            WHERE memory_id=? ORDER BY ordinal""", (row['memory_id'],)).fetchall()
        source_refs = []
        hidden = 0
        for r in refs:
            if r['source_kind'] == 'message' and actor['role'] != 'operator':
                permitted = db.execute("""SELECT 1 FROM messages WHERE
                    message_id=? AND project_id=? AND (sender_id=? OR recipient_id=?)""",
                    (r['source_ref'], actor['project_id'], actor['principal_id'], actor['principal_id'])).fetchone()
                if permitted is None:
                    hidden += 1
                    continue
            source_refs.append({'kind': r['source_kind'], 'ref': r['source_ref']})
        is_stale = row['status'] != 'active' or (row['expires_at'] is not None and row['expires_at'] <= now)
        effective = 'expired' if row['status'] == 'active' and is_stale else row['status']
        return {
            'memory_id': row['memory_id'], 'seq': row['seq'], 'kind': row['kind'],
            'title': row['title'], 'content': row['content'],
            'status': row['status'], 'effective_status': effective,
            'is_stale': is_stale, 'verification': 'agent_reported',
            'project_id': row['project_id'], 'author_id': row['author_id'],
            'task_id': row['task_id'], 'created_at': row['created_at'],
            'expires_at': row['expires_at'], 'supersedes_memory_id': row['supersedes_memory_id'],
            'superseded_by': row['superseded_by'],
            'invalidated_by': row['invalidated_by'], 'invalidated_at': row['invalidated_at'],
            'invalidation_reason': row['invalidation_reason'],
            'recorded_event_id': row['recorded_event_id'],
            'invalidation_event_id': row['invalidation_event_id'],
            'source_refs': source_refs, 'hidden_source_count': hidden,
            'external_sources_verified': False if any(r['kind'] == 'external' for r in source_refs) else None
        }

    def record_memory(self, session_token: str, kind: str, title: str, content: str,
                      source_refs: list, request_key: str, *, task_id: str | None = None,
                      supersedes_memory_id: str | None = None, ttl_seconds: int | None = None) -> dict:
        if kind not in _MEMORY_KINDS:
            raise WorkspaceError('VALIDATION_FAILED', 'unknown memory kind')
        _text(title, 'memory title', 256)
        _text(content, 'memory content', 16384)
        if task_id is not None:
            _text(task_id, 'task id', 128)
        if supersedes_memory_id is not None:
            _text(supersedes_memory_id, 'predecessor memory id', 128)
        if ttl_seconds is not None and (type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 31_536_000):
            raise WorkspaceError('VALIDATION_FAILED', 'ttl_seconds must be 60-31536000')
        try:
            _canonical(source_refs)
        except (TypeError, ValueError):
            raise WorkspaceError('VALIDATION_FAILED', 'invalid source serialization') from None
        payload = {'kind': kind, 'title': title, 'content': content,
                   'sources': source_refs, 'task_id': task_id,
                   'supersedes': supersedes_memory_id, 'ttl_seconds': ttl_seconds}

        def run(db, actor, now):
            refs = self._knowledge_sources(db, actor['project_id'], actor['principal_id'], actor['role'], source_refs)
            if task_id is not None and db.execute(
                    'SELECT 1 FROM tasks WHERE task_id=? AND project_id=?',
                    (task_id, actor['project_id'])).fetchone() is None:
                raise WorkspaceError('NOT_FOUND', 'task not in project')
            if supersedes_memory_id:
                prior = db.execute('SELECT * FROM memory_entries WHERE memory_id=? AND project_id=?',
                                   (supersedes_memory_id, actor['project_id'])).fetchone()
                if prior is None:
                    raise WorkspaceError('NOT_FOUND')
                if prior['author_id'] != actor['principal_id'] and actor['role'] != 'operator':
                    raise WorkspaceError('FORBIDDEN')
                if prior['status'] != 'active':
                    raise WorkspaceError('INVALID_TRANSITION')
            mid = _uuid()
            expires = now + ttl_seconds if ttl_seconds is not None else None
            cursor = db.execute("""INSERT INTO memory_entries (
                memory_id,project_id,author_id,task_id,kind,title,content,status,
                created_at,expires_at,supersedes_memory_id)
                VALUES (?,?,?,?,?,?,?,'active',?,?,?)""",
                (mid,actor['project_id'],actor['principal_id'],task_id,kind,title,content,
                 now,expires,supersedes_memory_id))
            db.execute('INSERT INTO memory_fts(rowid,title,content) VALUES (?,?,?)',
                       (cursor.lastrowid, title, content))
            for ordinal, ref in enumerate(refs):
                db.execute('INSERT INTO memory_sources VALUES (?,?,?,?)',
                           (mid,ordinal,ref['kind'],ref['ref']))
            if supersedes_memory_id:
                event = self._event(db, actor, 'memory.superseded', 'memory', supersedes_memory_id,
                                    {'superseded_by': mid}, now)
                db.execute("""UPDATE memory_entries SET status='superseded',superseded_by=?,
                    invalidated_by=?,invalidated_at=?,invalidation_reason='superseded',
                    invalidation_event_id=? WHERE memory_id=?""",
                    (mid,actor['principal_id'],now,event,supersedes_memory_id))
            if self._failpoint == 'before_memory_commit_event':
                raise RuntimeError('Injected memory failure before event')
            event = self._event(db, actor, 'memory.recorded', 'memory', mid,
                                {'kind': kind, 'task_id': task_id, 'supersedes': supersedes_memory_id}, now)
            db.execute('UPDATE memory_entries SET recorded_event_id=? WHERE memory_id=?', (event,mid))
            return {'memory_id': mid, 'status': 'active', 'seq': cursor.lastrowid,
                    'event_id': event}

        return self._mutate(session_token, 'memory.record', request_key, payload, run)

    def retract_memory(self, session_token: str, memory_id: str, reason: str,
                       request_key: str) -> dict:
        _text(memory_id, 'memory id', 128)
        _text(reason, 'retraction reason', 4096)
        payload = {'memory_id': memory_id, 'reason': reason}

        def run(db, actor, now):
            row = db.execute('SELECT * FROM memory_entries WHERE memory_id=? AND project_id=?',
                             (memory_id, actor['project_id'])).fetchone()
            if row is None:
                raise WorkspaceError('NOT_FOUND')
            if row['author_id'] != actor['principal_id'] and actor['role'] != 'operator':
                raise WorkspaceError('FORBIDDEN')
            if row['status'] != 'active':
                raise WorkspaceError('INVALID_TRANSITION')
            event = self._event(db, actor, 'memory.retracted', 'memory', memory_id,
                                {'reason': reason}, now)
            db.execute("""UPDATE memory_entries SET status='retracted',invalidated_by=?,
                invalidated_at=?,invalidation_reason=?,invalidation_event_id=? WHERE memory_id=?""",
                (actor['principal_id'],now,reason,event,memory_id))
            return {'memory_id': memory_id, 'status': 'retracted', 'event_id': event}

        return self._mutate(session_token, 'memory.retract', request_key, payload, run)

    def get_memory(self, session_token: str, memory_id: str) -> dict:
        _text(memory_id, 'memory id', 128)
        with self._connect() as db:
            actor = self._actor(db, session_token)
            row = db.execute('SELECT * FROM memory_entries WHERE memory_id=? AND project_id=?',
                             (memory_id,actor['project_id'])).fetchone()
            if row is None:
                raise WorkspaceError('NOT_FOUND')
            now = self.clock()
            if (row['status'] == 'retracted' or (row['expires_at'] is not None and row['expires_at'] <= now)) \
                    and actor['role'] != 'operator' and actor['principal_id'] != row['author_id']:
                raise WorkspaceError('NOT_FOUND')
            return self._memory_view(db,row,actor,now)

    def search_memories(self, session_token: str, query: str | None = None,
                        *, kind: str | None = None, before_seq: int | None = None,
                        limit: int = 20) -> dict:
        if kind is not None and kind not in _MEMORY_KINDS:
            raise WorkspaceError('VALIDATION_FAILED', 'unknown memory kind')
        if before_seq is not None and (type(before_seq) is not int or before_seq < 1):
            raise WorkspaceError('VALIDATION_FAILED', 'invalid search cursor')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError('VALIDATION_FAILED', 'invalid search limit')
        match = _memory_match_query(query) if query is not None else None
        with self._connect() as db:
            actor = self._actor(db, session_token)
            params = []
            join = ''
            if match is not None:
                join = 'JOIN memory_fts ON memory_fts.rowid=m.seq'
            where = ['m.project_id=?', "m.status='active'", '(m.expires_at IS NULL OR m.expires_at>?)']
            params.extend([actor['project_id'], self.clock()])
            if match is not None:
                where.append('memory_fts MATCH ?')
                params.append(match)
            if kind is not None:
                where.append('m.kind=?')
                params.append(kind)
            if before_seq is not None:
                where.append('m.seq<?')
                params.append(before_seq)
            params.append(limit+1)
            rows = db.execute(f"""SELECT m.* FROM memory_entries AS m {join}
                WHERE {' AND '.join(where)} ORDER BY m.seq DESC LIMIT ?""", params).fetchall()
            out = []
            for row in rows[:limit]:
                title, t_trunc = _memory_preview(row['title'],160)
                content, c_trunc = _memory_preview(row['content'],320)
                out.append({'memory_id':row['memory_id'], 'seq':row['seq'], 'kind':row['kind'],
                            'title_preview':title, 'content_preview':content,
                            'preview_truncated':t_trunc or c_trunc,
                            'created_at':row['created_at'], 'expires_at':row['expires_at'],
                            'author_id':row['author_id'], 'status':'active','verification':'agent_reported'})
            return {'memories':out,
                    'next_cursor':rows[min(len(rows),limit)-1]['seq'] if rows else before_seq,
                    'has_more':len(rows)>limit}

    def memory_changes(self, session_token: str, *, after_event_id: int = 0,
                       limit: int = 100) -> dict:
        if type(after_event_id) is not int or after_event_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError('VALIDATION_FAILED', 'invalid change cursor/limit')
        with self._connect() as db:
            actor = self._actor(db,session_token)
            rows = db.execute("""SELECT event_id,action,subject_id AS memory_id,occurred_at
                FROM audit_events WHERE project_id=? AND event_id>? AND subject_type='memory'
                AND action IN ('memory.recorded','memory.superseded','memory.retracted')
                ORDER BY event_id ASC LIMIT ?""",
                (actor['project_id'],after_event_id,limit+1)).fetchall()
            return {'changes':[dict(row) for row in rows[:limit]],
                    'next_cursor':rows[min(len(rows),limit)-1]['event_id'] if rows else after_event_id,
                    'has_more':len(rows)>limit}


    # M2.3 Context Gateway: structured, sourced and explicit task handoffs.
    # Text is agent-reported and untrusted. No provider transcript, file read,
    # CLI execution, external fetching, automatic notification or lease transfer.

    @staticmethod
    def _context_sources(db, actor, task_id: str, source_refs: list, now: float) -> list[dict]:
        if not isinstance(source_refs, list) or not 1 <= len(source_refs) <= 8:
            raise WorkspaceError('VALIDATION_FAILED','1-8 source refs required')
        refs=[]
        seen=set()
        for item in source_refs:
            if not isinstance(item,dict) or set(item)!= {'kind','ref'}:
                raise WorkspaceError('VALIDATION_FAILED','invalid source')
            kind,ref=item['kind'],item['ref']
            if kind=='memory':
                _text(ref,'source ref',128)
                memory=db.execute('''SELECT * FROM memory_entries WHERE memory_id=?
                    AND project_id=? AND status='active' AND (expires_at IS NULL OR expires_at>?)
                    AND (task_id IS NULL OR task_id=?)''',
                    (ref,actor['project_id'],now,task_id)).fetchone()
                if memory is None:
                    raise WorkspaceError('NOT_FOUND','inactive or out-of-scope memory')
                validated={'kind':kind,'ref':ref}
            else:
                validated=Workspace._knowledge_sources(
                    db,actor['project_id'],actor['principal_id'],actor['role'],[item])[0]
                if kind=='task' and ref!=task_id:
                    raise WorkspaceError('NOT_FOUND','task source outside snapshot')
                if kind=='evidence':
                    evidence=db.execute('SELECT task_id FROM evidence WHERE evidence_id=?',(ref,)).fetchone()
                    if evidence is None or evidence['task_id']!=task_id:
                        raise WorkspaceError('NOT_FOUND','evidence outside snapshot task')
                if kind=='decision':
                    row=db.execute('SELECT status FROM knowledge_decisions WHERE decision_id=?',(ref,)).fetchone()
                    if row is None or row['status']!='accepted':
                        raise WorkspaceError('NOT_FOUND','decision not currently accepted')
            if (kind,ref) in seen:
                raise WorkspaceError('VALIDATION_FAILED','duplicate source')
            seen.add((kind,ref))
            refs.append(validated)
        return refs

    @staticmethod
    def _visible_context(db,actor,row,now):
        if actor['role']=='operator' or row['author_id']==actor['principal_id']:
            return True
        if db.execute('SELECT 1 FROM context_handoffs WHERE snapshot_id=? AND recipient_id=?',
                      (row['snapshot_id'],actor['principal_id'])).fetchone():
            return True
        lease=db.execute('''SELECT 1 FROM task_leases l JOIN sessions s
            ON s.session_id=l.holder_session_id JOIN principals p ON p.principal_id=s.principal_id
            WHERE l.task_id=? AND s.principal_id=? AND s.session_id=? AND l.expires_at>?
            AND s.revoked=0 AND p.enabled=1''',
            (row['task_id'],actor['principal_id'],actor['session_id'],now)).fetchone()
        return lease is not None

    @staticmethod
    def _context_visibility_clause(actor,now):
        if actor['role']=='operator':
            return '1=1',[]
        return ('''(c.author_id=? OR EXISTS (SELECT 1 FROM context_handoffs h
                WHERE h.snapshot_id=c.snapshot_id AND h.recipient_id=?)
                OR EXISTS (SELECT 1 FROM task_leases l JOIN sessions s ON s.session_id=l.holder_session_id
                JOIN principals p ON p.principal_id=s.principal_id
                WHERE l.task_id=c.task_id AND s.principal_id=? AND s.session_id=?
                AND l.expires_at>? AND s.revoked=0 AND p.enabled=1))''',
               [actor['principal_id'],actor['principal_id'],actor['principal_id'],actor['session_id'],now])

    @staticmethod
    def _context_view(db,row,actor,now):
        sources=db.execute('SELECT source_kind,source_ref FROM context_sources WHERE snapshot_id=? ORDER BY ordinal',
                           (row['snapshot_id'],)).fetchall()
        exposed=[]
        hidden=0
        stale=0
        for source in sources:
            kind,ref=source['source_kind'],source['source_ref']
            if kind=='message' and actor['role']!='operator':
                m=db.execute('SELECT 1 FROM messages WHERE message_id=? AND project_id=? AND (sender_id=? OR recipient_id=?)',
                             (ref,actor['project_id'],actor['principal_id'],actor['principal_id'])).fetchone()
                if m is None:
                    hidden+=1
                    continue
            if kind=='memory':
                valid=db.execute('''SELECT 1 FROM memory_entries WHERE memory_id=? AND project_id=?
                    AND status='active' AND (expires_at IS NULL OR expires_at>?)''',
                    (ref,actor['project_id'],now)).fetchone()
                if valid is None:
                    stale+=1
                    continue
            if kind=='decision':
                valid=db.execute("SELECT 1 FROM knowledge_decisions WHERE decision_id=? AND project_id=? AND status='accepted'",
                                 (ref,actor['project_id'])).fetchone()
                if valid is None:
                    stale+=1
                    continue
            exposed.append({'kind':kind,'ref':ref})
        return {'snapshot_id':row['snapshot_id'],'seq':row['seq'],'project_id':row['project_id'],
                'task_id':row['task_id'],'author_id':row['author_id'],
                'author_session_id':row['author_session_id'],'lease_id':row['lease_id'],
                'fence_token':row['fence_token'],'summary':row['summary'],
                'next_steps':row['next_steps'],'blockers':row['blockers'],
                'changed_paths':json.loads(row['changed_paths_json']),
                'created_at':row['created_at'],'created_event_id':row['created_event_id'],
                'source_refs':exposed,'hidden_source_count':hidden,'stale_source_count':stale,
                'verification':'agent_reported','external_sources_verified':False if any(
                    x['kind']=='external' for x in exposed) else None}

    def publish_context(self,session_token:str,task_id:str,expected_version:int,
                        fence_token:int,summary:str,next_steps:str,source_refs:list,
                        request_key:str,*,changed_paths:list|None=None,blockers:str|None=None)->dict:
        _text(task_id,'task id',128)
        _version(expected_version)
        _text(summary,'summary',4096)
        _text(next_steps,'next steps',2048)
        if blockers is not None:
            _text(blockers,'blockers',2048)
        paths=_context_paths(changed_paths)
        try:
            _canonical(source_refs)
        except (TypeError,ValueError):
            raise WorkspaceError('VALIDATION_FAILED','invalid source serialization') from None
        payload={'task_id':task_id,'expected_version':expected_version,'fence_token':fence_token,
                 'summary':summary,'next_steps':next_steps,'sources':source_refs,
                 'changed_paths':paths,'blockers':blockers}

        def run(db,actor,now):
            if actor['role']!='agent':
                raise WorkspaceError('FORBIDDEN')
            task=self._load_task(db,actor['project_id'],task_id)
            self._check_lease(task,actor,fence_token,now)
            self._check_version(task,expected_version)
            if task['status']!='InProgress':
                raise WorkspaceError('INVALID_TRANSITION')
            sources=self._context_sources(db,actor,task_id,source_refs,now)
            snapshot_id=_uuid()
            cursor=db.execute('''INSERT INTO context_snapshots
                (snapshot_id,project_id,task_id,author_id,author_session_id,lease_id,
                 fence_token,summary,next_steps,blockers,changed_paths_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (snapshot_id,actor['project_id'],task_id,actor['principal_id'],actor['session_id'],
                 task['lease_id'],fence_token,summary,next_steps,blockers,_canonical(paths),now))
            for ordinal,ref in enumerate(sources):
                db.execute('INSERT INTO context_sources VALUES (?,?,?,?)',
                           (snapshot_id,ordinal,ref['kind'],ref['ref']))
            if self._failpoint=='before_context_commit_event':
                raise RuntimeError('Injected context failure')
            event=self._event(db,actor,'context.published','context',snapshot_id,
                              {'task_id':task_id,'source_count':len(sources)},now)
            db.execute('UPDATE context_snapshots SET created_event_id=? WHERE snapshot_id=?',
                       (event,snapshot_id))
            return {'snapshot_id':snapshot_id,'seq':cursor.lastrowid,
                    'task_id':task_id,'event_id':event}

        return self._mutate(session_token,'context.publish',request_key,payload,run)

    def send_handoff(self,session_token:str,snapshot_id:str,recipient_agent_id:str,
                     action_required:str,request_key:str)->dict:
        _text(snapshot_id,'snapshot id',128)
        _text(recipient_agent_id,'recipient id',128)
        _text(action_required,'action required',2048)
        payload={'snapshot_id':snapshot_id,'recipient_id':recipient_agent_id,
                 'action_required':action_required}

        def run(db,actor,now):
            if actor['role']!='agent':
                raise WorkspaceError('FORBIDDEN')
            snap=db.execute('SELECT * FROM context_snapshots WHERE snapshot_id=? AND project_id=?',
                            (snapshot_id,actor['project_id'])).fetchone()
            if snap is None:
                raise WorkspaceError('NOT_FOUND')
            if snap['author_id']!=actor['principal_id']:
                raise WorkspaceError('FORBIDDEN')
            target=db.execute('''SELECT 1 FROM principals WHERE principal_id=? AND project_id=?
                AND role='agent' AND enabled=1''',
                (recipient_agent_id,actor['project_id'])).fetchone()
            if recipient_agent_id==actor['principal_id']:
                raise WorkspaceError('VALIDATION_FAILED','self handoff not permitted')
            if target is None:
                raise WorkspaceError('NOT_FOUND','recipient not found')
            task=self._load_task(db,actor['project_id'],snap['task_id'])
            if (task['lease_id']!=snap['lease_id'] or task['fence_token']!=snap['fence_token']
                    or task['holder_session_id']!=actor['session_id']):
                raise WorkspaceError('STALE_FENCE')
            self._check_lease(task,actor,snap['fence_token'],now)
            if task['status']!='InProgress':
                raise WorkspaceError('INVALID_TRANSITION')
            if db.execute('SELECT 1 FROM context_handoffs WHERE snapshot_id=? AND recipient_id=?',
                          (snapshot_id,recipient_agent_id)).fetchone():
                raise WorkspaceError('INVALID_TRANSITION','already handed off')
            handoff_id=_uuid()
            cursor=db.execute('''INSERT INTO context_handoffs
                (handoff_id,snapshot_id,project_id,sender_id,recipient_id,action_required,created_at)
                VALUES (?,?,?,?,?,?,?)''',
                (handoff_id,snapshot_id,actor['project_id'],actor['principal_id'],
                 recipient_agent_id,action_required,now))
            if self._failpoint=='before_handoff_commit_event':
                raise RuntimeError('Injected handoff failure')
            event=self._event(db,actor,'context.handoff_sent','context',snapshot_id,
                              {'handoff_id':handoff_id,'recipient_id':recipient_agent_id},now)
            db.execute('UPDATE context_handoffs SET created_event_id=? WHERE handoff_id=?',
                       (event,handoff_id))
            return {'handoff_id':handoff_id,'seq':cursor.lastrowid,'snapshot_id':snapshot_id,
                    'recipient_agent_id':recipient_agent_id,'event_id':event}

        return self._mutate(session_token,'context.handoff',request_key,payload,run)

    def get_context(self,session_token:str,snapshot_id:str)->dict:
        _text(snapshot_id,'snapshot id',128)
        with self._connect() as db:
            actor=self._actor(db,session_token)
            snap=db.execute('SELECT * FROM context_snapshots WHERE snapshot_id=? AND project_id=?',
                            (snapshot_id,actor['project_id'])).fetchone()
            if snap is None or not self._visible_context(db,actor,snap,self.clock()):
                raise WorkspaceError('NOT_FOUND')
            return self._context_view(db,snap,actor,self.clock())

    def list_task_contexts(self,session_token:str,task_id:str,*,before_seq:int|None=None,
                           limit:int=20)->dict:
        _text(task_id,'task id',128)
        if before_seq is not None and (type(before_seq) is not int or before_seq<1):
            raise WorkspaceError('VALIDATION_FAILED','invalid context cursor')
        if type(limit) is not int or not 1<=limit<=100:
            raise WorkspaceError('VALIDATION_FAILED','invalid context limit')
        with self._connect() as db:
            actor=self._actor(db,session_token)
            self._load_task(db,actor['project_id'],task_id)
            clause,params=self._context_visibility_clause(actor,self.clock())
            where=['c.project_id=?','c.task_id=?',clause]
            bindings=[actor['project_id'],task_id]+params
            if before_seq is not None:
                where.append('c.seq<?')
                bindings.append(before_seq)
            bindings.append(limit+1)
            rows=db.execute(f'''SELECT c.* FROM context_snapshots c WHERE {' AND '.join(where)}
                 ORDER BY c.seq DESC LIMIT ?''',bindings).fetchall()
            result=[]
            for snap in rows[:limit]:
                preview,truncated=_memory_preview(snap['summary'],256)
                result.append({'snapshot_id':snap['snapshot_id'],'seq':snap['seq'],
                               'task_id':snap['task_id'],'author_id':snap['author_id'],
                               'summary_preview':preview,'preview_truncated':truncated,
                               'created_at':snap['created_at']})
            return {'snapshots':result,
                    'next_cursor':rows[min(len(rows),limit)-1]['seq'] if rows else before_seq,
                    'has_more':len(rows)>limit}

    def handoff_inbox(self,session_token:str,*,after_seq:int=0,limit:int=20)->dict:
        if type(after_seq) is not int or after_seq<0 or type(limit) is not int or not 1<=limit<=100:
            raise WorkspaceError('VALIDATION_FAILED','invalid handoff cursor/limit')
        with self._connect() as db:
            actor=self._actor(db,session_token)
            rows=db.execute('''SELECT seq,handoff_id,snapshot_id,sender_id,recipient_id,
                    action_required,created_at,created_event_id FROM context_handoffs
                    WHERE project_id=? AND recipient_id=? AND seq>?
                    ORDER BY seq LIMIT ?''',
                    (actor['project_id'],actor['principal_id'],after_seq,limit+1)).fetchall()
            return {'handoffs':[dict(x) for x in rows[:limit]],
                    'next_cursor':rows[min(len(rows),limit)-1]['seq'] if rows else after_seq,
                    'has_more':len(rows)>limit}

    def context_changes(self,session_token:str,*,after_event_id:int=0,limit:int=100)->dict:
        if type(after_event_id) is not int or after_event_id<0 or type(limit) is not int or not 1<=limit<=100:
            raise WorkspaceError('VALIDATION_FAILED','invalid context cursor/limit')
        with self._connect() as db:
            actor=self._actor(db,session_token)
            clause,params=self._context_visibility_clause(actor,self.clock())
            rows=db.execute(f'''SELECT e.event_id,e.action,e.subject_id AS snapshot_id,e.occurred_at
                FROM audit_events e JOIN context_snapshots c ON c.snapshot_id=e.subject_id
                WHERE e.project_id=? AND e.event_id>? AND e.subject_type='context'
                AND e.action IN ('context.published','context.handoff_sent')
                AND {clause} ORDER BY e.event_id ASC LIMIT ?''',
                [actor['project_id'],after_event_id]+params+[limit+1]).fetchall()
            return {'changes':[dict(x) for x in rows[:limit]],
                    'next_cursor':rows[min(len(rows),limit)-1]['event_id'] if rows else after_event_id,
                    'has_more':len(rows)>limit}
