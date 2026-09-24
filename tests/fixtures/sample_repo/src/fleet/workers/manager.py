"""Worker lifecycle management."""

from __future__ import annotations


class Worker:
    def __init__(self, wid: int):
        self.wid = wid
        self.ready = False

    def mark_ready(self) -> None:
        """Called once model initialization finishes."""
        self.ready = True


class WorkerManager:
    """Creates, terminates and tracks workers."""

    def __init__(self):
        self.workers: list[Worker] = []

    def ready_count(self) -> int:
        return sum(1 for w in self.workers if w.ready)

    def scale(self, desired: int) -> None:
        """Converge the number of workers to `desired`."""
        while len(self.workers) < desired:
            self._spawn()
        while len(self.workers) > desired:
            self.terminate(self.workers[-1])

    def terminate(self, worker: Worker) -> None:
        self.workers.remove(worker)

    def _spawn(self) -> Worker:
        w = Worker(len(self.workers))
        self.workers.append(w)
        return w
