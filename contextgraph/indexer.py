"""Incremental indexing: hash diff -> parse changed -> relink -> rank -> summarize -> embed."""

from __future__ import annotations

from array import array
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from contextgraph import memory as memory_store
from contextgraph.config import Config, RepoPaths
from contextgraph.crawl import crawl, language_for
from contextgraph.embeddings import embedding_text, get_embedder, should_embed
from contextgraph.linker import Linker
from contextgraph.models import FileResult
from contextgraph.parse_markdown import MarkdownParser
from contextgraph.parse_python import PythonParser
from contextgraph.store import Store
from contextgraph.summaries import summarize
from contextgraph.util import identifier_tokens, sha1

# Bump when parser/linker/summary output changes so existing indexes rebuild.
PIPELINE_VERSION = "3"

GITIGNORE = "# The DB is a rebuildable cache; memories.jsonl is the source of truth.\n*.db\n*.db-*\n"


@dataclass
class IndexReport:
    files_seen: int = 0
    parsed: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    embedded: int = 0
    embedding_model: str | None = None
    relinked: bool = False
    counts: dict = field(default_factory=dict)


def index_repo(root: Path, *, embed: bool = True, force: bool = False,
               log: Callable[[str], None] = lambda _: None) -> IndexReport:
    root = root.resolve()
    cfg = Config.load(root)
    paths = RepoPaths(root)
    paths.index_dir.mkdir(exist_ok=True)
    gi = paths.index_dir / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE)
    store = Store(paths.db)
    if store.get_meta("pipeline_version") != PIPELINE_VERSION:
        force = True
    report = IndexReport()

    files = crawl(root, cfg.ignore, cfg.max_file_bytes)
    report.files_seen = len(files)
    known = store.file_hashes()
    parsers = {"python": PythonParser(), "markdown": MarkdownParser()}
    changed: list[FileResult] = []
    skipped: dict[str, int] = defaultdict(int)
    for rel in files:
        try:
            raw = (root / rel).read_bytes()
        except OSError:
            skipped["unreadable"] += 1
            continue
        h = sha1(raw)
        if not force and known.get(rel) == h:
            report.unchanged += 1
            continue
        text = raw.decode("utf-8", errors="replace")
        result = parsers[language_for(rel)].parse(rel, text)
        result.content_hash = h
        changed.append(result)
    present = set(files)
    removed = [p for p in known if p not in present]
    report.parsed = len(changed)
    report.deleted = len(removed)
    report.skipped = dict(skipped)

    mem_hash = sha1(paths.memories.read_bytes()) if paths.memories.exists() else ""
    memories_changed = store.get_meta("memories_hash") != mem_hash
    needs_relink = bool(changed or removed or memories_changed or force or store.get_meta("linked") != "1")
    now = memory_store.now_iso()
    with store.transaction():
        for p in removed:
            store.delete_file(p)
        for result in changed:
            store.write_file_result(result)
        if needs_relink:
            store.set_meta("linked", "0")
    if needs_relink:
        report.relinked = True
        with store.transaction():
            linker = Linker(store)
            edges = linker.link()
            edges += memory_store.sync(store, paths.memories, linker)
            store.replace_edges(edges)
            store.set_meta("memories_hash", mem_hash)
        with store.transaction():
            store.set_importance(pagerank([n.id for n in store.all_nodes()], store.all_edges()))
        with store.transaction():
            _write_summaries_and_fts(store)
            store.set_meta("linked", "1")

    if embed:
        report.embedded, report.embedding_model = _embed(store, cfg, log)

    store.set_meta("indexed_at", now)
    store.set_meta("pipeline_version", PIPELINE_VERSION)
    store.conn.commit()
    report.counts = store.counts()
    store.close()
    return report


def _write_summaries_and_fts(store: Store) -> None:
    nodes = {n.id: n for n in store.all_nodes()}
    children: dict[int, list] = defaultdict(list)
    callees: dict[int, list] = defaultdict(list)
    callers: dict[int, list] = defaultdict(list)
    for s, t, rel, _ in store.all_edges():
        if s not in nodes or t not in nodes:
            continue
        if rel == "contains":
            children[s].append(nodes[t])
        elif rel == "calls":
            callees[s].append(nodes[t])
            callers[t].append(nodes[s])
    rows, fts = [], []
    for nid, n in nodes.items():
        if n.type == "memory":
            summary = n.docstring or ""
        else:
            summary = summarize(n, children[nid], callees[nid], callers[nid])
        rows.append((summary, nid))
        if n.type != "memory":
            fts.append((nid, n.name, identifier_tokens(n.name, n.qualname), n.file_path or "",
                        summary, (n.docstring or "")[:1000]))
    store.set_summaries(rows)
    store.rebuild_fts(fts)


def _embed(store: Store, cfg: Config, log: Callable[[str], None]) -> tuple[int, str | None]:
    embedder = get_embedder(cfg.embedding_model)
    if embedder is None:
        return 0, None
    existing = store.embedding_hashes(embedder.model)
    todo: list[tuple[str, str, str]] = []
    keep: set[str] = set()
    for n in store.all_nodes():
        if not should_embed(n):
            continue
        text = embedding_text(n)
        h = sha1(text)
        keep.add(n.key)
        if existing.get(n.key) != h:
            todo.append((n.key, h, text))
    store.delete_embeddings_except(keep)
    todo.sort(key=lambda item: len(item[2]))  # similar lengths per batch -> far less padding
    if todo:
        log(f"Embedding {len(todo)} nodes with {embedder.model}…")
    batch = 256
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        vectors = embedder.embed_documents([c[2] for c in chunk])
        store.upsert_embeddings([
            (key, h, embedder.model, len(vec), array("f", vec).tobytes())
            for (key, h, _), vec in zip(chunk, vectors)
        ])
        store.conn.commit()
    return len(todo), embedder.model


# PageRank over the dependency graph (calls/imports/inherits/references). Importance flows
# from caller to callee, so widely used symbols rank high (the signal Aider's repo map uses).
FLOW_RELATIONS = {"calls": 1.0, "inherits": 1.0, "references": 0.5, "imports": 0.3}
DAMPING = 0.85
ITERATIONS = 40


def pagerank(node_ids: list[int], edges: list[tuple[int, int, str, str | None]]) -> dict[int, float]:
    n = len(node_ids)
    if n == 0:
        return {}
    index = {nid: i for i, nid in enumerate(node_ids)}
    out: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for s, t, rel, _ in edges:
        w = FLOW_RELATIONS.get(rel)
        if w and s in index and t in index:  # the store never keeps self-edges
            out[index[s]].append((index[t], w))
    out_weight = {i: sum(w for _, w in targets) for i, targets in out.items()}
    rank = [1.0 / n] * n
    base = (1.0 - DAMPING) / n
    for _ in range(ITERATIONS):
        new = [base] * n
        dangling = sum(rank[i] for i in range(n) if i not in out_weight)
        spread = DAMPING * dangling / n
        for i, targets in out.items():
            share = DAMPING * rank[i] / out_weight[i]
            for j, w in targets:
                new[j] += share * w
        rank = [r + spread for r in new]
    top = max(rank)
    return {nid: rank[i] / top for nid, i in index.items()}
