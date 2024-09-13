import random
from locust import TaskSet
from LocustBackend import LocustBackend
import nnsight

class CommonTasks(TaskSet):
    def on_start(self):
        # Setup common configurations, API key, and model
        self.backend = LocustBackend(
            client=self.client,
            api_key=random.choice(self.user.api_keys),
            base_url=self.user.config['base_url']
        )  
        for key, val in self.user.config.items():
            setattr(self, key, val)
        
        self.model = nnsight.LanguageModel(self.model_key)
        self.api_key = random.choice(self.user.api_keys)

        # TODO: Allow for IP address to be uniquely assigned