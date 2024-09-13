from locust import task
import random
from common_tasks import CommonTasks
from util import gen_rand_str
import nnsight

class BenchmarkTasks(CommonTasks):
    """TaskSet for benchmarking with incrementally increasing load."""

    def on_start(self):
        """Initialize backend and model for benchmarking."""

        super().on_start()
        # TODO: Add to config
        self.min_num_tokens = 1
        self.max_num_tokens = 10**6
        self.increment_size = 10   # Increment size
        self.current_token_count = self.min_num_tokens

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