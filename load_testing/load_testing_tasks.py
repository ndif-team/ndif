from locust import task
import random
from common_tasks import CommonTasks
from util import get_num_layers

class LoadTestingTasks(CommonTasks):
    def on_start(self):
        """Initialize backend and model for standard load testing tasks."""
        super().on_start()
        self.n_layers = get_num_layers(self.model.config)

    @task
    def next_token(self):
        """Simulate a next token request."""
        query = 'hello' * random.randint(1, 20)
        with self.model.trace(query, remote=True, backend=self.backend):
            output = self.model.output.save()

        self.backend.ndif_request(locust_client=self, name='next_token')

    @task
    def layer_selector(self):
        """Simulate selecting activations of an intermediate layer."""
        layer = random.randint(0, self.n_layers - 1)
        query = 'test query'
        with self.model.trace(query, remote=True, backend=self.backend):
            output = self.model.transformer.h[layer].output.save()

        self.backend.ndif_request(locust_client=self, name='layer_selector')