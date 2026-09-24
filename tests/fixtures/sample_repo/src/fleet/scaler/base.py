class BaseScaler:
    """Common scaler behaviour."""

    def clamp(self, value: int, low: int, high: int) -> int:
        return max(low, min(high, value))

    def reconcile(self) -> None:
        raise NotImplementedError
