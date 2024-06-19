from ray.dashboard.modules.serve.sdk import ServeSubmissionClient
from ray.serve.schema import ServeDeploySchema


class RayState:

    def __init__(self, ray_config_path: str, ray_dashboard_address: str) -> None:

        self.config = ServeDeploySchema.parse_file(ray_config_path)
        
        self.ray_dashboard_address = ray_dashboard_address

    def apply(self):

        ServeSubmissionClient(self.ray_dashboard_address).deploy_applications(
            self.config.dict(exclude_unset=True),
        )
