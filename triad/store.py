from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .util import now, uid


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "state.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        # 16 MiB with SQLite's default 4096-byte pages; fail instead of unbounded growth.
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        self.db.execute(f"PRAGMA max_page_count={16 * 1024 * 1024 // page_size}")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sessions(
            role TEXT PRIMARY KEY, generation INTEGER NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            created REAL NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS messages(
            id TEXT PRIMARY KEY, recipient TEXT NOT NULL, generation INTEGER NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS requests(
            id TEXT PRIMARY KEY, identity TEXT NOT NULL, action TEXT NOT NULL,
            body TEXT NOT NULL, result TEXT NOT NULL);
        """)

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    def get(self, table, key):
        assert table in {"sessions", "tasks", "runs"}
        column = "role" if table == "sessions" else "id"
        row = self.db.execute(f"SELECT data FROM {table} WHERE {column}=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, table):
        assert table in {"sessions", "tasks", "runs"}
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT data FROM {table} ORDER BY rowid")]

    def put(self, table, value):
        assert table in {"sessions", "tasks", "runs"}
        if table == "sessions":
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?)",
                            (value["role"], value["generation"], json.dumps(value)))
        else:
            self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?)", (value["id"], json.dumps(value)))

    def event(self, kind, data):
        cursor = self.db.execute("INSERT INTO events(id,created,type,data) VALUES(?,?,?,?)",
                                 (uid(), now(), kind, json.dumps(data)))
        return cursor.lastrowid

    def events(self, after=0):
        return [dict(row) | {"data": json.loads(row["data"])} for row in self.db.execute(
            "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 100", (after,))]

    def enqueue(self, recipient, generation, kind, body):
        msg = {"v": 1, "id": uid(), "to": recipient, "generation": generation,
               "type": kind, "body": body}
        msg["seq"] = self.event("message_queued", msg)
        self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                        (msg["id"], recipient, generation, "queued", now(), json.dumps(msg)))
        return msg

    def close(self):
        self.db.close()
