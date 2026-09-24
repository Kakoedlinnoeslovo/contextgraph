from contextgraph.indexer import index_repo
from contextgraph.store import Store


def test_unchanged_repo_is_skipped(repo):
    rep = index_repo(repo)
    assert rep.parsed == 0 and not rep.relinked


def test_edit_add_delete(repo, edge_set):
    mgr = repo / "src/fleet/workers/manager.py"
    mgr.write_text(mgr.read_text().replace("def terminate(", "def drain(").replace("self.terminate(", "self.drain("))
    (repo / "src/fleet/extra.py").write_text("from fleet.workers.manager import WorkerManager\n\n"
                                             "def boot():\n    WorkerManager().scale(1)\n")
    (repo / "src/fleet/metrics.py").unlink()
    rep = index_repo(repo)
    assert rep.parsed == 2 and rep.deleted == 1 and rep.relinked
    s = Store(repo / ".contextgraph" / "contextgraph.db")
    names = {n.qualname for n in s.all_nodes()}
    assert "fleet.workers.manager.WorkerManager.drain" in names
    assert "fleet.workers.manager.WorkerManager.terminate" not in names
    assert "fleet.metrics.QueueMetrics" not in names
    edges = edge_set(s)
    # incoming edges from unchanged files survive re-parsing the target file
    assert ("fleet.scaler.autoscaler.Autoscaler.reconcile", "calls",
            "fleet.workers.manager.WorkerManager.scale") in edges
    assert ("fleet.extra.boot", "calls", "fleet.workers.manager.WorkerManager.scale") in edges
    s.close()
