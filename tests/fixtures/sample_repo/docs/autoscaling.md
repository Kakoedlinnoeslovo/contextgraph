# Autoscaling

The `Autoscaler` computes the desired worker count from `QueueMetrics` and
asks the `WorkerManager` to converge. Bounds come from `EndpointConfig`.

## Readiness

A worker only counts as ready after `Worker.mark_ready` runs, which happens
after model initialization. See src/fleet/workers/manager.py.
