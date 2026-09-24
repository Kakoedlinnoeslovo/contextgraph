"""Hybrid retrieval: BM25 + embeddings -> seeds -> graph expansion -> weighted ranking.

score = 0.55 * relevance + 0.25 * graph proximity + 0.10 * pagerank + 0.10 * memory boost
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from contextgraph.config import Config
from contextgraph.embeddings import SemanticIndex, get_embedder
from contextgraph.models import Memory, Node
from contextgraph.store import Store
from contextgraph.util import fts_match_expr, is_auxiliary_path, is_test_path

WEIGHTS = {"relevance": 0.55, "graph": 0.25, "importance": 0.10, "memory": 0.10}
RELATION_WEIGHTS = {
    "calls": 1.0, "inherits": 0.9, "memory_about": 1.0, "tested_by": 0.7, "documented_by": 0.6,
    "contains": 0.6, "references": 0.6, "imports": 0.4,
}
TYPE_FACTOR = {"test": 0.75, "file": 0.6, "module": 0.9}
RRF_K = 20
SEEDS = 15  # top search hits the graph expansion starts from
EXPAND_DEPTH = 2
EXPAND_MAX_NODES = 50
HOP_DECAY = 0.5
MAX_NEIGHBOURS = 20
MAX_RANKED = 60

Via = tuple[str, int | None]  # (how a node was reached, id of the node it was reached from)


@dataclass
class Scored:
    node: Node
    score: float
    relevance: float = 0.0
    via: str | None = None


@dataclass
class Retrieval:
    query: str
    ranked: list[Scored]
    memories: list[Memory]
    mode: str  # "hybrid" or "lexical"


def lexical_search(store: Store, query: str, limit: int, memories: bool = False) -> list[tuple[int, float]]:
    match = fts_match_expr(query)
    return store.fts_search(match, limit, memories) if match else []


def _rrf(lists: list[list[int]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for lst in lists:
        for rank, nid in enumerate(lst):
            out[nid] = out.get(nid, 0.0) + 1.0 / (RRF_K + rank + 1)
    if not out:
        return out
    top = max(out.values())
    return {k: v / top for k, v in out.items()}


def expand(store: Store, seeds: dict[int, float], importance: dict[int, float]) -> dict[int, tuple[float, Via | None]]:
    """Bounded best-first graph expansion with relation weights and hop decay.

    Returns node_id -> (proximity score, how it was reached) for the seeds plus their neighbourhood.
    """
    best: dict[int, tuple[float, Via | None]] = {nid: (score, None) for nid, score in seeds.items()}
    heap = [(-score, nid, 0) for nid, score in seeds.items()]
    heapq.heapify(heap)
    expanded: set[int] = set()
    cap = EXPAND_MAX_NODES + len(seeds)
    while heap and len(best) < cap:
        neg, nid, hops = heapq.heappop(heap)
        if nid in expanded or hops >= EXPAND_DEPTH:
            continue
        expanded.add(nid)
        nbrs = [(t, rel, "→") for t, rel, _ in store.out_edges(nid)]
        nbrs += [(s, rel, "←") for s, rel, _ in store.in_edges(nid)]
        nbrs.sort(key=lambda x: (-RELATION_WEIGHTS.get(x[1], 0.3), -importance.get(x[0], 0.0)))
        for other, rel, arrow in nbrs[:MAX_NEIGHBOURS]:
            score = -neg * RELATION_WEIGHTS.get(rel, 0.3) * (HOP_DECAY if hops >= 1 else 1.0)
            if score <= 0:
                continue
            prev = best.get(other)
            if prev is None or score > prev[0]:
                best[other] = (score, (f"{rel} {arrow}", nid))
                heapq.heappush(heap, (-score, other, hops + 1))
            if len(best) >= cap:
                break
    return best


def retrieve(store: Store, cfg: Config, query: str) -> Retrieval:
    index = store.node_index()
    key_ids = {key: nid for nid, key, _, _ in index}
    id_types = {nid: typ for nid, _, typ, _ in index}
    importance = {nid: imp for nid, _, _, imp in index}
    memory_uid = {nid: key.split(":", 1)[1] for nid, key, typ, _ in index if typ == "memory"}
    lex = [nid for nid, _ in lexical_search(store, query, 40)]

    sem_ids: list[int] = []
    sem_index = None
    qvec = None
    embedder = get_embedder(cfg.embedding_model) if store.has_embeddings() else None
    if embedder is not None:
        sem_index = SemanticIndex(store, embedder.model)
        if len(sem_index):
            qvec = embedder.embed_query(query)
            sem_ids = [key_ids[k] for k, _ in sem_index.search(qvec, 40) if k in key_ids]
        else:
            sem_index = None
    mode = "hybrid" if sem_index is not None else "lexical"

    relevance = _rrf([lex, sem_ids] if sem_ids else [lex])
    code_rel = {nid: r for nid, r in relevance.items() if id_types.get(nid) != "memory"}

    # Memories: semantic hits among memory nodes + FTS over memory text.
    mem_rows = {m.uid: m for m in store.all_memories()}
    mem_targets = {
        m.uid: [t for t, rel, _ in store.out_edges(key_ids[f"memory:{m.uid}"]) if rel == "memory_about"]
        for m in mem_rows.values() if f"memory:{m.uid}" in key_ids
    }
    mem_scores: dict[str, float] = {}
    for nid, r in relevance.items():
        if nid in memory_uid:
            uid = memory_uid[nid]
            mem_scores[uid] = max(mem_scores.get(uid, 0.0), r)
    mem_by_rowid = {m.id: m for m in mem_rows.values()}
    fts_mem = lexical_search(store, query, 10, memories=True)
    if fts_mem:
        top = max(s for _, s in fts_mem) or 1.0
        for rowid, s in fts_mem:
            m = mem_by_rowid.get(rowid)
            if m:
                mem_scores[m.uid] = max(mem_scores.get(m.uid, 0.0), 0.8 * s / top)

    seeds = dict(sorted(code_rel.items(), key=lambda kv: -kv[1])[:SEEDS])
    prox = expand(store, seeds, importance)

    # Memory relevance flows to the nodes a memory is about.
    memory_boost: dict[int, float] = {}
    for uid, s in mem_scores.items():
        for t in mem_targets.get(uid, []):
            memory_boost[t] = max(memory_boost.get(t, 0.0), s)
            if t not in prox:
                prox[t] = (0.5 * s, (f"memory_about ← memory {uid}", None))

    candidates = [nid for nid in prox if id_types.get(nid) not in (None, "memory")]
    nodes = store.get_nodes(candidates)

    sem_sim: dict[int, float] = {}
    if sem_index is not None and qvec is not None:
        sims = sem_index.similarities(qvec, [nodes[n].key for n in candidates if n in nodes])
        if sims:
            lo, hi = min(sims.values()), max(sims.values())
            span = (hi - lo) or 1.0
            sem_sim = {key_ids[k]: (v - lo) / span for k, v in sims.items()}

    ranked: list[Scored] = []
    for nid in candidates:
        node = nodes.get(nid)
        if node is None:
            continue
        rel = code_rel.get(nid, 0.0)
        if sem_sim:
            rel = 0.6 * rel + 0.4 * sem_sim.get(nid, 0.0)
        p, via = prox[nid]
        score = (WEIGHTS["relevance"] * rel + WEIGHTS["graph"] * p + WEIGHTS["importance"] * node.importance
                 + WEIGHTS["memory"] * memory_boost.get(nid, 0.0))
        score *= TYPE_FACTOR.get(node.type, 1.0)
        if node.type not in ("test", "documentation") and node.file_path:
            if is_test_path(node.file_path):
                score *= 0.7  # fixtures / helpers inside test trees
            elif is_auxiliary_path(node.file_path):
                score *= 0.8
        ranked.append(Scored(node, score, rel, _describe(via, nodes)))
    ranked.sort(key=lambda s: -s.score)
    ranked = ranked[:MAX_RANKED]

    # Also surface memories attached to top-ranked nodes even when their text didn't match.
    top_ids = {s.node.id for s in ranked[:12]}
    for uid, targets in mem_targets.items():
        if uid not in mem_scores and top_ids & set(targets):
            mem_scores[uid] = 0.3
    memories = [mem_rows[uid] for uid, _ in sorted(mem_scores.items(), key=lambda kv: -kv[1]) if uid in mem_rows]
    return Retrieval(query, ranked, memories, mode)


def _describe(via: Via | None, nodes: dict[int, Node]) -> str | None:
    if via is None:
        return None
    label, src = via
    if src is None:
        return label
    return f"{label} {nodes[src].name if src in nodes else src}"
