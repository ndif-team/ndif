from locust import TaskSet, task
import random
from LocustBackend import LocustBackend
import nnsight

class LoadTestingTasks(TaskSet):
    def on_start(self):
        """Initialize backend and model for standard load testing tasks."""
        # Dynamically initialize the backend
        self.backend = LocustBackend(
            client=self.client,
            api_key=random.choice(self.user.api_keys),
            base_url=self.user.config['base_url']
        )
        for key, val in self.user.config.items():
            setattr(self, key, val)

        self.model = nnsight.LanguageModel(self.model_key)
        self.n_layers = self.model.config.n_layer

        # TODO: Give this user a unique IP address

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
        layer = random.randint(0, self.model.config.n_layer - 1)
        query = 'test query'
        with self.model.trace(query, remote=True, backend=self.backend):
            output = self.model.transformer.h[layer].output.save()

        self.backend.ndif_request(locust_client=self, name='layer_selector')