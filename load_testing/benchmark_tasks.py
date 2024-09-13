from locust import TaskSet, task
import random
from LocustBackend import LocustBackend
from util import gen_rand_str
import nnsight

class BenchmarkTasks(TaskSet):
    """TaskSet for benchmarking with incrementally increasing load."""
    # TODO: Add to config
    min_num_tokens = 1
    max_num_tokens = 10**6
    increment_size = 10   # Increment size
    current_token_count = min_num_tokens

    def on_start(self):
        """Initialize backend and model for benchmarking."""
        self.backend = LocustBackend(
            client=self.client,
            api_key=random.choice(self.user.api_keys),
            base_url=self.user.config['base_url']
        )

        for key, val in self.user.config.items():
            setattr(self, key, val)

        self.model = nnsight.LanguageModel(self.model_key)

    @task
    def benchmark_token_size(self):
        """Increment token size and send requests to benchmark system performance."""
        # Increment the batch size for benchmarking
        if self.current_token_count > self.max_num_tokens:
            self.current_token_count = self.min_num_tokens  # Reset to minimum batch size

        # Generate input
        word = gen_rand_str(10) if self.deterministic else 'ayy '
        sentence = word * self.current_token_count
        
        # Simulate the model trace
        with self.model.trace(sentence, remote=True, backend=self.backend):
            output = self.model.output.save()

        # Send the request
        self.backend.ndif_request(locust_client=self, name=f'tokens_{self.current_token_count}')

        # Increase the batch size for the next run
        self.current_token_count *= self.increment_size