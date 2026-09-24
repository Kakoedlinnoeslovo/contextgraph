from fleet.config import EndpointConfig
from fleet.scaler.autoscaler import Autoscaler
from fleet.workers.manager import WorkerManager


def test_scales_up():
    mgr = WorkerManager()
    scaler = Autoscaler(mgr, EndpointConfig(workers_max=3))
    scaler.metrics.record(10)
    scaler.reconcile()
    assert len(mgr.workers) == 3


def test_respects_minimum():
    mgr = WorkerManager()
    scaler = Autoscaler(mgr, EndpointConfig(workers_min=1))
    scaler.reconcile()
    assert len(mgr.workers) == 1
