import os

from ray import serve

from ..controller import ControllerDeploymentArgs, _ControllerDeployment
from .scheduler import SchedulingActor


@serve.deployment(ray_actor_options={"num_cpus": 1, "resources": {"head": 1}})
class SchedulingControllerDeployment(_ControllerDeployment):
    def __init__(
        self,
        google_creds_path: str,
        google_calendar_id: str,
        check_interval_s: float,
        **kwargs
    ):

        super().__init__(**kwargs)

        controller_handle = serve.get_app_handle(self.replica_context.app_name)

        self.scheduler = SchedulingActor.options().remote(
            controller_handle=controller_handle,
            google_creds_path=google_creds_path,
            google_calendar_id=google_calendar_id,
            check_interval_s=check_interval_s,
        )

        self.scheduler.start.remote()


class SchedulingControllerDeploymentArgs(ControllerDeploymentArgs):

    google_creds_path: str = os.environ.get("SCHEDULING_GOOGLE_CREDS_PATH", "")
    google_calendar_id: str = os.environ.get("SCHEDULING_GOOGLE_CALENDAR_ID", "")
    check_interval_s: float = float(os.environ.get("SCHEDULING_CHECK_INTERVAL_S", "10"))


def app(args: SchedulingControllerDeploymentArgs):
    return SchedulingControllerDeployment.bind(**args.model_dump())
