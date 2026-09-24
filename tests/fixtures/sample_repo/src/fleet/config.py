from dataclasses import dataclass


@dataclass
class EndpointConfig:
    """Scaling bounds for one endpoint."""

    workers_min: int = 0
    workers_max: int = 10
    target_queue_per_worker: int = 4
