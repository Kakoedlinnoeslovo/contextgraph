from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from contextgraph.models import FileResult, Memory, Node, RawRef

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    language TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    qualname TEXT NOT NULL,
    file_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    docstring TEXT,
    is_public INTEGER DEFAULT 1,
    content_hash TEXT,
    parent_id INTEGER,
    summary TEXT,
    importance REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS nodes_qualname ON nodes(qualname);

CREATE TABLE IF NOT EXISTS edges (
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    relation TEXT NOT NULL,
    metadata TEXT,
    PRIMARY KEY (source_id, target_id, relation)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS edges_target ON edges(target_id, relation);

CREATE TABLE IF NOT EXISTS raw_refs (
    src_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    target TEXT,
    scope TEXT
);
CREATE INDEX IF NOT EXISTS raw_refs_src ON raw_refs(src_id);

CREATE TABLE IF NOT EXISTS embeddings (
    node_key TEXT PRIMARY KEY,
    text_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    uid TEXT UNIQUE NOT NULL,
    topic TEXT,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    files TEXT,
    symbols TEXT,
    stale INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, idents, file_path, summary, docstring,
    tokenize = 'porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    topic, content, tokenize = 'porter unicode61'
);
"""

NODE_COLUMNS = (
    "id, key, type, name, qualname, file_path, start_line, end_line, "
    "signature, docstring, is_public, content_hash, summary, importance"
)

# bm25 column weights per FTS table.
FTS_WEIGHTS = {"nodes_fts": "6.0, 4.0, 1.5, 2.0, 1.0", "memories_fts": "2.0, 1.0"}


def row_to_node(row: sqlite3.Row) -> Node:
    return Node(
        id=row["id"], key=row["key"], type=row["type"], name=row["name"],
        qualname=row["qualname"], file_path=row["file_path"],
        start_line=row["start_line"], end_line=row["end_line"],
        signature=row["signature"], docstring=row["docstring"], is_public=bool(row["is_public"]),
        content_hash=row["content_hash"], summary=row["summary"],
        importance=row["importance"] or 0.0,
    )


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # -- files --------------------------------------------------------------

    def file_hashes(self) -> dict[str, str]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT path, content_hash FROM files")}

    def delete_file(self, path: str) -> None:
        ids = [r[0] for r in self.conn.execute("SELECT id FROM nodes WHERE file_path=?", (path,))]
        self._delete_nodes(ids)
        self.conn.execute("DELETE FROM files WHERE path=?", (path,))

    def _delete_nodes(self, ids: list[int]) -> None:
        for chunk in _chunks(ids, 500):
            marks = ",".join("?" * len(chunk))
            self.conn.execute(f"DELETE FROM raw_refs WHERE src_id IN ({marks})", chunk)
            self.conn.execute(f"DELETE FROM nodes_fts WHERE rowid IN ({marks})", chunk)
            self.conn.execute(
                f"DELETE FROM edges WHERE source_id IN ({marks}) OR target_id IN ({marks})",
                chunk + chunk,
            )
            self.conn.execute(f"DELETE FROM nodes WHERE id IN ({marks})", chunk)

    def write_file_result(self, result: FileResult) -> None:
        """Replace everything known about one file with a fresh parse result."""
        self.delete_file(result.path)
        key_to_id: dict[str, int] = {}
        for node in result.nodes:
            parent_id = key_to_id.get(node.parent_key) if node.parent_key else None
            if node.key in key_to_id:
                continue  # parsers de-duplicate keys; never let one bad file abort indexing
            key_to_id[node.key] = self.insert_node(node, parent_id)
        self.conn.executemany(
            "INSERT INTO raw_refs(src_id, kind, name, target, scope) VALUES (?,?,?,?,?)",
            [
                (key_to_id[r.src_key], r.kind, r.name, r.target, r.scope)
                for r in result.refs
                if r.src_key in key_to_id
            ],
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO files(path, content_hash, language) VALUES (?,?,?)",
            (result.path, result.content_hash, result.language),
        )

    # -- nodes --------------------------------------------------------------

    def all_nodes(self, include_memory: bool = True) -> list[Node]:
        sql = f"SELECT {NODE_COLUMNS} FROM nodes"
        if not include_memory:
            sql += " WHERE type != 'memory'"
        return [row_to_node(r) for r in self.conn.execute(sql)]

    def node_index(self) -> list[tuple[int, str, str, float]]:
        """(id, key, type, importance) for every node: a cheap index without loading full nodes."""
        return [(r[0], r[1], r[2], r[3] or 0.0)
                for r in self.conn.execute("SELECT id, key, type, importance FROM nodes")]

    def get_node(self, node_id: int) -> Node | None:
        row = self.conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes WHERE id=?", (node_id,)).fetchone()
        return row_to_node(row) if row else None

    def get_nodes(self, ids: Iterable[int]) -> dict[int, Node]:
        out: dict[int, Node] = {}
        for chunk in _chunks(list(ids), 500):
            marks = ",".join("?" * len(chunk))
            for r in self.conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes WHERE id IN ({marks})", chunk):
                out[r["id"]] = row_to_node(r)
        return out

    def get_node_by_key(self, key: str) -> Node | None:
        row = self.conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes WHERE key=?", (key,)).fetchone()
        return row_to_node(row) if row else None

    def find_nodes(self, *, name: str | None = None, qualname: str | None = None,
                   qualname_suffix: str | None = None, file_path: str | None = None) -> list[Node]:
        clauses, params = [], []
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if qualname is not None:
            clauses.append("qualname = ?")
            params.append(qualname)
        if qualname_suffix is not None:
            clauses.append("(qualname = ? OR qualname LIKE ?)")
            params += [qualname_suffix, f"%.{qualname_suffix}"]
        if file_path is not None:
            clauses.append("file_path = ?")
            params.append(file_path)
        where = " AND ".join(clauses) or "1"
        sql = f"SELECT {NODE_COLUMNS} FROM nodes WHERE {where} ORDER BY importance DESC"
        return [row_to_node(r) for r in self.conn.execute(sql, params)]

    def children(self, node_id: int) -> list[Node]:
        rows = self.conn.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE parent_id=? ORDER BY start_line", (node_id,)
        )
        return [row_to_node(r) for r in rows]

    def parent_ids(self) -> dict[int, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT id, parent_id FROM nodes WHERE parent_id IS NOT NULL")}

    def set_importance(self, scores: dict[int, float]) -> None:
        self.conn.executemany("UPDATE nodes SET importance=? WHERE id=?", [(v, k) for k, v in scores.items()])

    def set_summaries(self, rows: list[tuple[str, int]]) -> None:
        """rows: (summary, node_id)"""
        self.conn.executemany("UPDATE nodes SET summary=? WHERE id=?", rows)

    def insert_node(self, node: Node, parent_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO nodes(key, type, name, qualname, file_path, start_line, end_line, "
            "signature, docstring, is_public, content_hash, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (node.key, node.type, node.name, node.qualname, node.file_path, node.start_line,
             node.end_line, node.signature, node.docstring, int(node.is_public), node.content_hash, parent_id),
        )
        return cur.lastrowid

    def delete_nodes_of_type(self, node_type: str) -> None:
        ids = [r[0] for r in self.conn.execute("SELECT id FROM nodes WHERE type=?", (node_type,))]
        self._delete_nodes(ids)

    # -- full-text ----------------------------------------------------------

    def rebuild_fts(self, rows: list[tuple[int, str, str, str, str, str]]) -> None:
        """rows: (node_id, name, idents, file_path, summary, docstring)"""
        self.conn.execute("DELETE FROM nodes_fts")
        self.conn.executemany(
            "INSERT INTO nodes_fts(rowid, name, idents, file_path, summary, docstring) VALUES (?,?,?,?,?,?)",
            rows,
        )

    def fts_search(self, match: str, limit: int, memories: bool = False) -> list[tuple[int, float]]:
        """(rowid, score) with higher = better. Rowids are node ids, or memory ids if `memories`."""
        table = "memories_fts" if memories else "nodes_fts"
        try:
            rows = self.conn.execute(
                f"SELECT rowid, bm25({table}, {FTS_WEIGHTS[table]}) AS s FROM {table} "
                f"WHERE {table} MATCH ? ORDER BY s LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r[0], -r[1]) for r in rows]

    # -- raw refs & edges ---------------------------------------------------

    def raw_refs(self) -> list[tuple[int, RawRef]]:
        rows = self.conn.execute("SELECT src_id, kind, name, target, scope FROM raw_refs")
        return [(r[0], RawRef(src_key="", kind=r[1], name=r[2], target=r[3], scope=r[4])) for r in rows]

    def replace_edges(self, edges: Iterable[tuple[int, int, str, dict | None]]) -> None:
        self.conn.execute("DELETE FROM edges")
        self.conn.executemany(
            "INSERT OR IGNORE INTO edges(source_id, target_id, relation, metadata) VALUES (?,?,?,?)",
            [(s, t, rel, json.dumps(meta) if meta else None) for s, t, rel, meta in edges if s != t],
        )

    def all_edges(self) -> list[tuple[int, int, str, str | None]]:
        return [tuple(r) for r in self.conn.execute("SELECT source_id, target_id, relation, metadata FROM edges")]

    def out_edges(self, node_id: int) -> list[tuple[int, str, str | None]]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT target_id, relation, metadata FROM edges WHERE source_id=?", (node_id,))]

    def in_edges(self, node_id: int) -> list[tuple[int, str, str | None]]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT source_id, relation, metadata FROM edges WHERE target_id=?", (node_id,))]

    # -- embeddings ---------------------------------------------------------

    def has_embeddings(self) -> bool:
        return self.conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone() is not None

    def embedding_hashes(self, model: str) -> dict[str, str]:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT node_key, text_hash FROM embeddings WHERE model=?", (model,))}

    def upsert_embeddings(self, rows: list[tuple[str, str, str, int, bytes]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO embeddings(node_key, text_hash, model, dim, vector) VALUES (?,?,?,?,?)", rows
        )

    def delete_embeddings_except(self, keep_keys: set[str]) -> None:
        existing = [r[0] for r in self.conn.execute("SELECT node_key FROM embeddings")]
        drop = [k for k in existing if k not in keep_keys]
        for chunk in _chunks(drop, 500):
            self.conn.execute(f"DELETE FROM embeddings WHERE node_key IN ({','.join('?' * len(chunk))})", chunk)

    def load_embeddings(self, model: str) -> list[tuple[str, int, bytes]]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT node_key, dim, vector FROM embeddings WHERE model=?", (model,))]

    # -- memories -----------------------------------------------------------

    def replace_memories(self, memories: list[Memory]) -> None:
        self.conn.execute("DELETE FROM memories")
        self.conn.execute("DELETE FROM memories_fts")
        for m in memories:
            cur = self.conn.execute(
                "INSERT INTO memories(uid, topic, content, created_at, files, symbols, stale) VALUES (?,?,?,?,?,?,?)",
                (m.uid, m.topic, m.content, m.created_at, json.dumps(m.files), json.dumps(m.symbols), int(m.stale)),
            )
            self.conn.execute(
                "INSERT INTO memories_fts(rowid, topic, content) VALUES (?,?,?)",
                (cur.lastrowid, m.topic or "", m.content),
            )

    def all_memories(self) -> list[Memory]:
        return [
            Memory(
                id=r["id"], uid=r["uid"], topic=r["topic"], content=r["content"], created_at=r["created_at"],
                files=json.loads(r["files"] or "[]"), symbols=json.loads(r["symbols"] or "[]"),
                stale=bool(r["stale"]),
            )
            for r in self.conn.execute("SELECT * FROM memories ORDER BY id")
        ]

    # -- stats --------------------------------------------------------------

    def counts(self) -> dict:
        c = self.conn
        return {
            "files": c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "nodes_by_type": dict(c.execute("SELECT type, COUNT(*) FROM nodes GROUP BY type").fetchall()),
            "edges_by_relation": dict(c.execute("SELECT relation, COUNT(*) FROM edges GROUP BY relation").fetchall()),
            "languages": dict(c.execute("SELECT language, COUNT(*) FROM files GROUP BY language").fetchall()),
            "embeddings": c.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
            "memories": c.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
            "stale_memories": c.execute("SELECT COUNT(*) FROM memories WHERE stale=1").fetchone()[0],
        }


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
