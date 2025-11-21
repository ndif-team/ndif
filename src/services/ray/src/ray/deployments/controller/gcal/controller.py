import os
import time

from ray import ray

from .....providers.mailgun import MailgunProvider
from .....providers.objectstore import ObjectStoreProvider
from .....providers.socketio import SioProvider
from ..cluster.deployment import DeploymentLevel
from ..controller import ControllerDeploymentArgs, _ControllerActor
from .scheduler import SchedulingActor


@ray.remote(num_cpus=1, num_gpus=0, max_restarts=-1, resources={"head": 1})
class SchedulingControllerActor(_ControllerActor):
    def __init__(
        self,
        google_credentials_path: str,
        google_calendar_id: str,
        check_interval_s: float,
        delay_start_s: float,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.google_calendar_id = google_calendar_id

        self.scheduler = SchedulingActor.options().remote(
            google_credentials_path=google_credentials_path,
            google_calendar_id=google_calendar_id,
            check_interval_s=check_interval_s,
        )

        # Allow time for all nodes to sync with the controller
        time.sleep(delay_start_s)

        self.scheduler.start.remote()

    def status(self):
        status = super().status()

        status["calendar_id"] = self.google_calendar_id

        schedule = ray.get(self.scheduler.get_schedule.remote())

        for model_key, schedule in schedule.items():
            application_name = f"ModelActor:{model_key}"

            if application_name in status["deployments"]:
                status["deployments"][application_name]["schedule"] = schedule

            else:
                self.cluster.evaluator(model_key)

                repo_id = self.cluster.evaluator.cache[model_key].config._name_or_path

                if repo_id in status["deployments"]:
                    del status["deployments"][repo_id]

                status["deployments"][application_name] = {
                    "deployment_level": DeploymentLevel.COLD.name,
                    "model_key": model_key,
                    "repo_id": repo_id,
                    "revision": self.cluster.evaluator.cache[model_key].revision,
                    "config": self.cluster.evaluator.cache[
                        model_key
                    ].config.to_json_string(),
                    "schedule": schedule,
                    "n_params": self.cluster.evaluator.cache[model_key].n_params,
                }

        return status


class SchedulingControllerActorArgs(ControllerDeploymentArgs):
    google_credentials_path: str = os.environ.get("SCHEDULING_GOOGLE_CREDS_PATH", "")
    google_calendar_id: str = os.environ.get("SCHEDULING_GOOGLE_CALENDAR_ID", "")
    check_interval_s: float = float(os.environ.get("SCHEDULING_CHECK_INTERVAL_S", "10"))
    delay_start_s: float = float(os.environ.get("SCHEDULING_DELAY_START_S", "15"))


def app(**kwargs):
    args = SchedulingControllerActorArgs(**kwargs)

    actor = SchedulingControllerActor.options(
        name="Controller",
        namespace="NDIF",
        lifetime="detached",
        runtime_env={
            **SioProvider.to_env(),
            **ObjectStoreProvider.to_env(),
            **MailgunProvider.to_env(),
        },
    ).remote(**args.model_dump())
