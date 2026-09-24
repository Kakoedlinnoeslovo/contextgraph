def test_core_edges(store, edge_set):
    e = edge_set(store)
    A = "fleet.scaler.autoscaler.Autoscaler"
    # self.manager is typed from the annotated __init__ parameter
    assert (f"{A}.reconcile", "calls", "fleet.workers.manager.WorkerManager.scale") in e
    assert (f"{A}.reconcile", "calls", f"{A}.desired") in e
    # inherited method resolved through the base class
    assert (f"{A}.desired", "calls", "fleet.scaler.base.BaseScaler.clamp") in e
    assert (A, "inherits", "fleet.scaler.base.BaseScaler") in e
    # `from fleet import Autoscaler` is resolved through the package __init__ re-export
    assert ("fleet.controller.EndpointController.__init__", "calls", A) in e
    assert ("fleet.controller.EndpointController.reconcile", "calls", f"{A}.reconcile") in e
    # local variable typed by constructor call
    assert ("fleet.controller.run_once", "calls", "fleet.controller.EndpointController.reconcile") in e
    assert ("fleet.scaler.autoscaler", "imports", "fleet.workers.manager.WorkerManager") in e
    assert ("fleet.workers.manager", "contains", "fleet.workers.manager.WorkerManager") in e


def test_tested_by_and_documented_by(store, edge_set):
    e = edge_set(store)
    A = "fleet.scaler.autoscaler.Autoscaler"
    assert (A, "tested_by", "tests.test_autoscaler.test_scales_up") in e
    assert (f"{A}.reconcile", "tested_by", "tests.test_autoscaler.test_scales_up") in e
    assert ("fleet.scaler.autoscaler", "tested_by", "tests.test_autoscaler") in e
    assert (A, "documented_by", "docs/autoscaling.md#autoscaling") in e
    assert ("fleet.workers.manager.Worker.mark_ready", "documented_by", "docs/autoscaling.md#readiness") in e
    assert ("fleet.workers.manager", "documented_by", "docs/autoscaling.md#readiness") in e


def test_no_self_or_builtin_edges(store, edge_set):
    for s, rel, t in edge_set(store):
        assert s != t
        assert not t.endswith(".len") and not t.endswith(".max")


def test_importance_ranks_widely_used_symbols(store):
    imp = {n.qualname: n.importance for n in store.all_nodes()}
    assert imp["fleet.workers.manager.WorkerManager.scale"] > imp["fleet.controller.run_once"]
    assert max(imp.values()) == 1.0
