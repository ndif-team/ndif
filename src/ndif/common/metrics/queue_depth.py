from . import Metric


class QueueDepthMetric(Metric):
    """Tracks the number of requests waiting in each model's queue.

    This metric provides visibility into queue buildup, which is a leading
    indicator of capacity issues before users experience timeouts.
    """
    name: str = "queue_depth"

    @classmethod
    def update(cls, model_key: str, depth: int):
        """Record the current queue depth for a model.

        Args:
            model_key: The model key identifier.
            depth: Number of requests currently in the queue.
        """
        super().update(
            depth,
            model_key=str(model_key),
        )
