from locust import task
from common_tasks import CommonTasks
from util import gen_rand_str

class BenchmarkTasks(CommonTasks):
    """TaskSet for benchmarking with incrementally increasing load."""

    def on_start(self):
        """Initialize backend and model for benchmarking."""

        super().on_start()
        
        benchmark_config = self.user.config['benchmarking']
        for key, value in benchmark_config.items():
            # Sets min_num_tokens, max_num_tokens, increment_strategy, increment_value
            setattr(self, key, value)

        # Set starting value
        self.current_token_count = self.min_num_tokens

    @task
    def benchmark_token_size(self):
        """Increment token size and send requests to benchmark system performance."""
        # Increment the batch size for benchmarking
        if self.current_token_count > self.max_num_tokens:
            self.current_token_count = self.min_num_tokens  # Reset to minimum batch size

        # Generate input
        word =  'ayy ' if self.deterministic else gen_rand_str(10)
        sentence = word * self.current_token_count
        
        # Simulate the model trace
        with self.model.trace(sentence, remote=True, backend=self.backend):
            output = self.model.output.save()

        # Send the request
        self.backend.ndif_request(name=f'tokens_{self.current_token_count}')

        # Increase the batch size for the next run
        if self.increment_strategy == 'add':
            self.current_token_count += self.increment_value
        elif self.increment_strategy == 'multiply':
            self.current_token_count *= self.increment_value
        else:
            raise ValueError(f"Incorrectly formatted `increment_stategy` value. Expected: {['add','multiply']} Received: {self.increment_strategy}")