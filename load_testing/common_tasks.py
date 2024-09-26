import uuid
from locust import TaskSet

import nnsight
from LocustBackend import LocustBackend
from util import gen_rand_ip, get_api_key, get_model_key

class CommonTasks(TaskSet):
    def on_start(self):
        config = self.user.config

        self.model_key = get_model_key(config)
        self.model = nnsight.LanguageModel(self.model_key)
        self.api_key = get_api_key(config)
        self.logger = self.user.logger

        # Give user a unique id (for debugging)
        self.user_id = str(uuid.uuid4())
        self.logger.debug(f'User {self.user_id} initialized with API key: {self.api_key}')
        self.logger.debug(f'User {self.user_id} initialized with model key: {self.model_key}')
        
        # Configue testing endpoint
        self.base_url = config['custom']['base_url'] 
        self.ws_url = config['custom']['ws_url'] 
        nnsight.CONFIG.API.HOST = self.base_url
        nnsight.CONFIG.API.SSL = False if 'localhost' in self.base_url else True # TODO: Make this more robust
        self.logger.debug(f'Endpoint: {self.base_url}')
        self.logger.debug(f'SSL: {nnsight.CONFIG.API.SSL}')

        # Generate and assign a unique IP
        self.fake_ip = gen_rand_ip()
        self.logger.debug(f'User {self.user_id} assigned IP address: {self.fake_ip}')

        self.deterministic = config['custom']['deterministic']

        # Set up LocustBackend (used to obtain RequestModel objects without automatically sending)
        self.backend = LocustBackend(
            client=self.client, # Locust client
            api_key=self.api_key,
            base_url=self.base_url,
            ws_address=self.ws_url,
            ip_addr=self.fake_ip,
            logger=self.logger
        )