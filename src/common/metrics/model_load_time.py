from . import Metric


class ModelLoadTimeMetric(Metric):

    name: str = "model_load_time"

    @classmethod
    def update(cls, time_s: float, model_key: str, type: str):

        super().update(
            time_s,
            model_key=model_key,
            type=type,
        )
