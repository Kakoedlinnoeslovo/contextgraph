from contextgraph.config import Config
from contextgraph.retrieval import retrieve


def top(store, query, k=3):
    return [s.node.qualname for s in retrieve(store, Config(), query).ranked[:k]]


def test_lexical_finds_identifiers(store):
    assert top(store, "WorkerManager scale")[0].startswith("fleet.workers.manager.WorkerManager")


def test_graph_expansion_pulls_in_neighbours(store):
    names = [s.node.qualname for s in retrieve(store, Config(), "EndpointController reconcile").ranked[:10]]
    assert "fleet.scaler.autoscaler.Autoscaler.reconcile" in names


def test_readiness_question(store):
    names = top(store, "when is a worker ready", 5)
    assert "fleet.workers.manager.Worker.mark_ready" in names
