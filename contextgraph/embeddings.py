"""Embedding backends. fastembed (ONNX, CPU) is optional; without it search is BM25-only."""

from __future__ import annotations

import functools
import os

from contextgraph.models import CODE_TYPES, Node
from contextgraph.store import Store

MODEL_CACHE = os.path.expanduser("~/.cache/contextgraph/models")
# More threads is slower on big.LITTLE CPUs (e.g. Apple silicon); 4 is a good default.
THREADS = min(4, os.cpu_count() or 4)


class FastEmbedder:
    def __init__(self, model: str):
        from fastembed import TextEmbedding  # noqa: PLC0415 - optional dependency

        self.model = model
        self._impl = TextEmbedding(model_name=model, cache_dir=MODEL_CACHE, threads=THREADS)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._impl.passage_embed(texts, batch_size=16)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._impl.query_embed(text))).tolist()


def get_embedder(model: str) -> FastEmbedder | None:
    """None when fastembed is not installed or CONTEXTGRAPH_NO_EMBED is set."""
    if os.environ.get("CONTEXTGRAPH_NO_EMBED"):
        return None
    return _load_embedder(model)


@functools.cache
def _load_embedder(model: str) -> FastEmbedder | None:
    try:
        return FastEmbedder(model)
    except ImportError:
        return None


EMBED_TYPES = CODE_TYPES | {"documentation", "memory"}


def should_embed(node: Node) -> bool:
    if node.type not in EMBED_TYPES:
        return False
    if node.name.startswith("__") and node.name.endswith("__") and node.type == "method" and not node.docstring:
        return False
    if node.type in ("function", "method") and not node.is_public and not node.docstring:
        span = (node.end_line or 0) - (node.start_line or 0)
        return span >= 8
    return True


def embedding_text(node: Node) -> str:
    if node.type == "memory":
        return f"memory {node.name}\n{node.docstring or ''}"
    if node.type == "documentation":
        return f"documentation {node.name}\n{node.file_path}\n{node.docstring or ''}"
    doc = (node.docstring or "")[:500]
    return f"{node.type} {node.qualname}\n{node.file_path}\n{node.signature or ''}\n{doc}".strip()


class SemanticIndex:
    """In-process cosine search over embeddings stored in SQLite (no vector DB)."""

    def __init__(self, store: Store, model: str):
        import numpy as np  # noqa: PLC0415 - optional dependency (installed with fastembed)

        self.np = np
        rows = store.load_embeddings(model)
        self.keys = [r[0] for r in rows]
        self.pos = {k: i for i, k in enumerate(self.keys)}
        if rows:
            mat = np.stack([np.frombuffer(r[2], dtype=np.float32, count=r[1]) for r in rows])
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self.matrix = mat / norms
        else:
            self.matrix = np.zeros((0, 1), dtype=np.float32)

    def __len__(self) -> int:
        return len(self.keys)

    def _q(self, qvec: list[float]):
        q = self.np.asarray(qvec, dtype=self.np.float32)
        n = self.np.linalg.norm(q)
        return q / n if n else q

    def search(self, qvec: list[float], k: int) -> list[tuple[str, float]]:
        if not len(self):
            return []
        sims = self.matrix @ self._q(qvec)
        k = min(k, len(sims))
        top = self.np.argpartition(-sims, k - 1)[:k]
        top = top[self.np.argsort(-sims[top])]
        return [(self.keys[i], float(sims[i])) for i in top]

    def similarities(self, qvec: list[float], keys: list[str]) -> dict[str, float]:
        idx = [(k, self.pos[k]) for k in keys if k in self.pos]
        if not idx:
            return {}
        sims = self.matrix[[i for _, i in idx]] @ self._q(qvec)
        return {k: float(s) for (k, _), s in zip(idx, sims)}
