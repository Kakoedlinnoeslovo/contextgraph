class QueueMetrics:
    """Reads queue depth for an endpoint."""

    def __init__(self):
        self._depth = 0

    def depth(self) -> int:
        return self._depth

    def record(self, n: int) -> None:
        self._depth = n
