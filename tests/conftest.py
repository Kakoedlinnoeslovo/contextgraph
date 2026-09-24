import shutil
from pathlib import Path

import pytest

from contextgraph.indexer import index_repo
from contextgraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CONTEXTGRAPH_NO_EMBED", "1")
    dst = tmp_path / "sample_repo"
    shutil.copytree(FIXTURE, dst, ignore=shutil.ignore_patterns(".contextgraph"))
    index_repo(dst)
    return dst


@pytest.fixture
def store(repo: Path):
    s = Store(repo / ".contextgraph" / "contextgraph.db")
    yield s
    s.close()


@pytest.fixture
def edge_set():
    """store -> {(source qualname, relation, target qualname)}"""
    def edges(store: Store) -> set[tuple[str, str, str]]:
        nodes = {n.id: n for n in store.all_nodes()}
        return {(nodes[s].qualname, rel, nodes[t].qualname) for s, t, rel, _ in store.all_edges()}
    return edges
