"""Persistent agent memories.

`.contextgraph/memories.jsonl` is the source of truth (append-only, safe to
commit); the SQLite DB holds a derived copy linked into the graph. Each record
stores *anchors* — the content hashes of the nodes it was about when it was
written — so later edits to those nodes mark the memory as possibly stale.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contextgraph.models import Memory, Node
from contextgraph.store import Store


def load_jsonl(path: Path) -> list[tuple[Memory, dict[str, str]]]:
    """Fold the append-only log into current memories (+ their anchors)."""
    if not path.exists():
        return []
    records: dict[str, dict] = {}  # dicts keep insertion order: an update keeps the memory's position
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        uid = rec.get("uid")
        if not uid:
            continue
        if rec.get("deleted"):
            records.pop(uid, None)
            continue
        records[uid] = rec
    return [
        (
            Memory(uid=uid, topic=rec.get("topic"), content=rec["text"], created_at=rec.get("created_at", ""),
                   files=rec.get("files") or [], symbols=rec.get("symbols") or []),
            rec.get("anchors") or {},
        )
        for uid, rec in records.items()
    ]


def append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def new_uid() -> str:
    return uuid.uuid4().hex[:8]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_targets(store: Store, linker, memory: Memory) -> list[Node]:
    """Graph nodes a memory is about: listed symbols, listed files, and symbols
    inside those files whose names appear in the memory text."""
    targets: dict[int, Node] = {}
    for sym in memory.symbols:
        hit = linker.lookup(sym, None)
        if hit is None and "." not in sym:
            ids = linker.by_name.get(sym, [])
            hit = (ids[0], "low") if len(ids) == 1 else None
        if hit:
            targets[hit[0]] = linker.nodes[hit[0]]
    text = memory.content + " " + (memory.topic or "")
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text))
    for f in memory.files:
        mod_id = linker.resolve_path(f)
        if mod_id is None:
            continue
        file_path = linker.nodes[mod_id].file_path
        mentioned = [n for n in store.find_nodes(file_path=file_path)
                     if n.type not in ("module", "file") and n.name in words]
        if mentioned:
            for n in mentioned:
                targets[n.id] = n
        else:
            targets[mod_id] = linker.nodes[mod_id]
    return list(targets.values())


def sync(store: Store, memories_path: Path, linker) -> list[tuple[int, int, str, dict | None]]:
    """Rebuild memory rows + memory nodes from the jsonl log. Returns memory_about edges."""
    loaded = load_jsonl(memories_path)
    store.delete_nodes_of_type("memory")
    for mem, anchors in loaded:
        # Stale when any node the memory was written about has changed or disappeared.
        mem.stale = any((node := store.get_node_by_key(key)) is None or node.content_hash != h
                        for key, h in anchors.items())
    store.replace_memories([m for m, _ in loaded])
    edges = []
    for mem, _ in loaded:
        node_id = store.insert_node(Node(
            key=f"memory:{mem.uid}", type="memory", name=mem.topic or mem.content[:40],
            qualname=f"memory.{mem.uid}", docstring=mem.content,
        ))
        targets = resolve_targets(store, linker, mem)
        for t in targets:
            edges.append((node_id, t.id, "memory_about", None))
    return edges


def anchors_for(targets: list[Node]) -> dict[str, str]:
    return {n.key: n.content_hash for n in targets if n.content_hash}
