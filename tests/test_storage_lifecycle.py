"""Regression tests for SQLite connection lifetime across operating systems."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import sqlite3

from agent_workspace import Workspace


class ClosingConnection(sqlite3.Connection):
    def close(self):
        self.was_closed = True
        return super().close()


class SQLiteLifecycleTests(TestCase):
    def test_corrupted_database_initialization_closes_connection(self):
        # PRAGMA setup fails on this file. It must not escape _connect's finally.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "corrupt.db"
            path.write_bytes(b"not a sqlite database")
            real_connect = sqlite3.connect
            connections = []

            def tracked_connect(*args, **kwargs):
                db = real_connect(*args, **kwargs, factory=ClosingConnection)
                db.was_closed = False
                connections.append(db)
                return db

            with patch("agent_workspace.core.sqlite3.connect", side_effect=tracked_connect):
                with self.assertRaises(sqlite3.DatabaseError):
                    Workspace(path)
            self.assertTrue(connections)
            self.assertTrue(all(db.was_closed for db in connections),
                            "Every opened connection must close even when PRAGMA setup fails")

    def test_normal_workspace_operations_close_connections(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.db"
            real_connect = sqlite3.connect
            connections = []

            def tracked_connect(*args, **kwargs):
                db = real_connect(*args, **kwargs, factory=ClosingConnection)
                db.was_closed = False
                connections.append(db)
                return db

            with patch("agent_workspace.core.sqlite3.connect", side_effect=tracked_connect):
                workspace = Workspace(path)
                initialized = workspace.bootstrap("test", tmp)
                token = workspace.open_session(initialized["operator_credential"])["session_token"]
                workspace.list_tasks(token)
            self.assertTrue(connections)
            self.assertTrue(all(db.was_closed for db in connections))
