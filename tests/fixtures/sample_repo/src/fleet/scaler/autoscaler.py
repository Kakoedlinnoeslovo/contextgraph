"""Queue-driven autoscaling."""

from ..config import EndpointConfig
from ..metrics import QueueMetrics
from ..workers.manager import WorkerManager
from .base import BaseScaler


class Autoscaler(BaseScaler):
    """Controls desired worker count based on queue depth and worker state."""

    def __init__(self, manager: WorkerManager, config: EndpointConfig):
        self.manager = manager
        self.config = config
        self.metrics = QueueMetrics()

    def desired(self) -> int:
        depth = self.metrics.depth()
        want = -(-depth // self.config.target_queue_per_worker)
        return self.clamp(want, self.config.workers_min, self.config.workers_max)

    def reconcile(self) -> None:
        """One scaling step: compute the target and converge."""
        self.manager.scale(self.desired())
