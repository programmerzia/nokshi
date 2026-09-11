"""SQLite persistence.

The schema mirrors the roadmap's core model (repository_files, file_symbols,
file_dependencies, project_memories, token_usage, context runs) so that a later
move to PostgreSQL is a driver change, not a redesign. SQLite is the right
choice for a per-developer daily tool: zero setup, one file, fast enough for
repositories with tens of thousands of files.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    language    TEXT NOT NULL,
    module      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    loc         INTEGER NOT NULL,
    tokens      INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    mtime       REAL NOT NULL,
    is_test     INTEGER NOT NULL DEFAULT 0,
    is_config   INTEGER NOT NULL DEFAULT 0,
    namespace   TEXT,
    summary     TEXT,
    summary_sha TEXT,
    indexed_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_module ON files(module);

CREATE TABLE IF NOT EXISTS symbols (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    signature  TEXT NOT NULL,
    parent     TEXT,
    line_start INTEGER NOT NULL,
    line_end   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);

CREATE TABLE IF NOT EXISTS imports (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    raw       TEXT NOT NULL,
    kind      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);

-- content terms per file (top identifiers/words) for lexical retrieval
CREATE TABLE IF NOT EXISTS terms (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    term    TEXT NOT NULL,
    count   INTEGER NOT NULL,
    PRIMARY KEY (file_id, term)
);

-- file_dependencies: src depends on dst. kind = import | reference
CREATE TABLE IF NOT EXISTS deps (
    src_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    dst_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind   TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (src_id, dst_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_deps_dst ON deps(dst_id);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,          -- decision | note | bug | pattern
    body       TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS context_runs (
    id              INTEGER PRIMARY KEY,
    ts              REAL NOT NULL,
    task            TEXT NOT NULL,
    role            TEXT NOT NULL,
    budget          INTEGER NOT NULL,
    considered      INTEGER NOT NULL,
    selected        INTEGER NOT NULL,
    tokens          INTEGER NOT NULL,
    baseline_tokens INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS token_usage (
    id            INTEGER PRIMARY KEY,
    ts            REAL NOT NULL,
    task          TEXT NOT NULL,
    role          TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read    INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(files)")}
        if "summary_sha" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN summary_sha TEXT")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- meta -------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # ---- files ------------------------------------------------------------
    def file_hashes(self) -> dict[str, str]:
        return {r["path"]: r["sha256"] for r in self.conn.execute("SELECT path, sha256 FROM files")}

    def upsert_file(self, c: sqlite3.Connection, **f) -> int:
        c.execute(
            """INSERT INTO files(path, language, module, size, loc, tokens, sha256, mtime,
                                 is_test, is_config, namespace, summary, indexed_at)
               VALUES (:path, :language, :module, :size, :loc, :tokens, :sha256, :mtime,
                       :is_test, :is_config, :namespace, :summary, :indexed_at)
               ON CONFLICT(path) DO UPDATE SET
                 language=excluded.language, module=excluded.module, size=excluded.size,
                 loc=excluded.loc, tokens=excluded.tokens, sha256=excluded.sha256,
                 mtime=excluded.mtime, is_test=excluded.is_test, is_config=excluded.is_config,
                 namespace=excluded.namespace, indexed_at=excluded.indexed_at""",
            {"summary": None, **f, "indexed_at": time.time()},
        )
        return c.execute("SELECT id FROM files WHERE path = ?", (f["path"],)).fetchone()["id"]

    def delete_files(self, paths: Iterable[str]) -> None:
        with self.tx() as c:
            c.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in paths])

    def replace_symbols(self, c: sqlite3.Connection, file_id: int, symbols, imports) -> None:
        c.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        c.execute("DELETE FROM imports WHERE file_id = ?", (file_id,))
        c.executemany(
            "INSERT INTO symbols(file_id, name, kind, signature, parent, line_start, line_end)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(file_id, s.name, s.kind, s.signature, s.parent, s.line_start, s.line_end) for s in symbols],
        )
        c.executemany(
            "INSERT INTO imports(file_id, raw, kind) VALUES (?, ?, ?)",
            [(file_id, i.raw, i.kind) for i in imports],
        )

    def replace_terms(self, c: sqlite3.Connection, file_id: int, counts: dict[str, int]) -> None:
        c.execute("DELETE FROM terms WHERE file_id = ?", (file_id,))
        c.executemany("INSERT INTO terms(file_id, term, count) VALUES (?, ?, ?)",
                      [(file_id, t, n) for t, n in counts.items()])

    def all_terms(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT file_id, term, count FROM terms").fetchall()

    def all_imports(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT file_id, raw FROM imports").fetchall()

    def set_summary(self, file_id: int, summary: str, sha: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE files SET summary = ?, summary_sha = ? WHERE id = ?", (summary, sha, file_id))

    def files_needing_summary(self, min_tokens: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM files WHERE tokens >= ? AND is_config = 0
               AND language IN ('php','python','typescript','javascript','vue','csharp')
               AND (summary IS NULL OR summary_sha IS NULL OR summary_sha != sha256)
               ORDER BY is_test ASC, tokens DESC""", (min_tokens,)).fetchall()

    def all_files(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM files ORDER BY path").fetchall()

    def file_by_path(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()

    def symbols_for(self, file_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM symbols WHERE file_id = ? ORDER BY line_start", (file_id,)
        ).fetchall()

    def all_symbols(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.*, f.path FROM symbols s JOIN files f ON f.id = s.file_id"
        ).fetchall()

    def imports_for(self, file_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM imports WHERE file_id = ?", (file_id,)).fetchall()

    # ---- dependencies -----------------------------------------------------
    def replace_deps(self, edges: Iterable[tuple[int, int, str, float]]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM deps")
            c.executemany(
                "INSERT OR REPLACE INTO deps(src_id, dst_id, kind, weight) VALUES (?, ?, ?, ?)", list(edges)
            )

    def all_deps(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM deps").fetchall()

    # ---- memory -----------------------------------------------------------
    def add_memory(self, kind: str, body: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO memories(kind, body, created_at) VALUES (?, ?, ?)", (kind, body, time.time()))

    def memories(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM memories ORDER BY created_at").fetchall()

    def delete_memory(self, memory_id: int) -> int:
        with self.tx() as c:
            return c.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount

    # ---- analytics --------------------------------------------------------
    def log_context_run(self, **row) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO context_runs(ts, task, role, budget, considered, selected, tokens, baseline_tokens)
                   VALUES (:ts, :task, :role, :budget, :considered, :selected, :tokens, :baseline_tokens)""",
                {"ts": time.time(), **row},
            )

    def log_usage(self, **row) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO token_usage(ts, task, role, provider, model, input_tokens, output_tokens,
                                           cache_read, cost_usd)
                   VALUES (:ts, :task, :role, :provider, :model, :input_tokens, :output_tokens,
                           :cache_read, :cost_usd)""",
                {"ts": time.time(), "cache_read": 0, **row},
            )

    def usage_summary(self, since: float) -> dict:
        totals = self.conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,
                      COALESCE(SUM(cost_usd),0) cost FROM token_usage WHERE ts >= ?""",
            (since,),
        ).fetchone()
        by_role = self.conn.execute(
            """SELECT role, model, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cost_usd) cost
               FROM token_usage WHERE ts >= ? GROUP BY role, model ORDER BY cost DESC""",
            (since,),
        ).fetchall()
        ctx = self.conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(tokens),0) t, COALESCE(SUM(baseline_tokens),0) b,
                      COALESCE(AVG(selected),0) sel, COALESCE(AVG(considered),0) cons
               FROM context_runs WHERE ts >= ?""",
            (since,),
        ).fetchone()
        return {"totals": totals, "by_role": by_role, "context": ctx}
