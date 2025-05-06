
from . import Metric


class CriticalErrorMetric(Metric):

    name: str = "critical_error"

    @classmethod
    def update(
        cls,
        error_message: str,
    ) -> None:

        super().update(
            error_message,
        )
