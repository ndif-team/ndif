import ray
from ray import serve

ray.init()

serve.get_app_handle("Controller").redeploy.remote()
