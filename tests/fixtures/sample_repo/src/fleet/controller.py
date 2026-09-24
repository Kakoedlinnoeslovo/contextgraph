from fleet import Autoscaler
from fleet.config import EndpointConfig
from fleet.workers.manager import WorkerManager


class EndpointController:
    def __init__(self, config: EndpointConfig):
        self.scaler = Autoscaler(WorkerManager(), config)

    def reconcile(self) -> None:
        self.scaler.reconcile()


def run_once(config: EndpointConfig) -> None:
    controller = EndpointController(config)
    controller.reconcile()
