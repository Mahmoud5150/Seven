"""One small database layer for SQLite (laptop, tests) and PostgreSQL (server).

All SQL in this project is written once, with `?` placeholders and these
type words, and this module adapts it:

    {id}     auto-increment primary key
    {float}  8-byte float (PostgreSQL's REAL is only 4 bytes: 36.8 would become 36.7999)

Pick the database with a URL (or a plain file path for SQLite):
    seven.db  /  sqlite:///data/seven.db  /  postgresql://user:pass@host:5432/seven
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager

_TYPES = {
    "sqlite": {"id": "INTEGER PRIMARY KEY AUTOINCREMENT", "float": "REAL"},
    "postgres": {"id": "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", "float": "DOUBLE PRECISION"},
}


class Database:
    def __init__(self, url: str = "seven.db") -> None:
        self.url = url
        if url.startswith(("postgres://", "postgresql://")):
            import psycopg                       # only needed on the server
            self.kind = "postgres"
            self.conn = psycopg.connect(url, autocommit=True)   # transactions are explicit below
        else:
            self.kind = "sqlite"
            path = url.removeprefix("sqlite:///")
            self.conn = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit BEGIN below
            self.conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0

    # ---- SQL in, adapted for this database ----
    def _sql(self, sql: str) -> str:
        sql = sql.format(**_TYPES[self.kind]) if "{" in sql else sql
        if self.kind == "postgres":
            sql = sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: tuple | list = ()):
        return self.conn.execute(self._sql(sql), tuple(params))

    def executemany(self, sql: str, rows) -> None:
        rows = list(rows)
        if not rows:
            return
        if self.kind == "postgres":
            with self.conn.cursor() as cur:
                cur.executemany(self._sql(sql), rows)
        else:
            self.conn.executemany(self._sql(sql), rows)

    def one(self, sql: str, params: tuple | list = ()):
        return self.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | list = ()) -> list:
        return self.execute(sql, params).fetchall()

    def script(self, sql: str) -> None:
        """Run several `;`-separated statements (schema setup)."""
        for stmt in [s.strip() for s in self._sql(sql).split(";")]:
            if stmt:
                self.conn.execute(stmt)

    def columns(self, table: str) -> set[str]:
        if self.kind == "postgres":
            return {r[0] for r in self.all(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))}
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    @contextmanager
    def transaction(self):
        """All or nothing. Nested calls join the outer transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        self._depth = 1
        try:
            if self.kind == "postgres":
                with self.conn.transaction():
                    yield self
            else:
                self.conn.execute("BEGIN")
                try:
                    yield self
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise
                self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    def close(self) -> None:
        self.conn.close()


def safe_identifier(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise ValueError(f"bad SQL identifier: {name}")
    return name
