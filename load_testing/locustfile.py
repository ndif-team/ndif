import logging
import os
import time
import random
import yaml
from dotenv import load_dotenv
from locust import HttpUser, TaskSet, task, between, events

import nnsight
from util import gen_rand_str

logger = logging.getLogger('locust')
logger.setLevel(logging.INFO)


# Load configuration
config_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config', 'locust_config.yml')
with open(config_file, 'r') as f:
  config = yaml.safe_load(f)

  
# Load API keys
load_dotenv() 
api_keys = os.getenv("API_KEYS", "").split(",") 

class NNSightTasks(TaskSet):
  def on_start(self):
    # Set attributes directly from the config
    for key, val in self.user.config.items():
      setattr(self, key, val)
    # TODO: Improve the config so that it has nested categories for each task

    # Randomly choose an API key
    # TODO: Have keys partitioned uniformally across users
    self.api_key = random.choice(api_keys)
    nnsight.CONFIG.set_default_api_key(self.api_key)
    logger.info(f'Set API key to: {self.api_key}')

    # TODO: Be able to randomly choose a model
    self.model = nnsight.LanguageModel("openai-community/gpt2")
    #self.model = nnsight.LanguageModel("meta-llama/Meta-Llama-3.1-405B-Instruct")
    #self.n_layers = self.model.config.n_layer
    self.n_layers = self.model.config.num_hidden_layers

    # TODO: Give this user a unique IP address

  def _record_success(self, start_time: float, request_type: str, name: str, response_length: int = 0) -> None:
    '''Records a successful request to Locust (needs to be manually done as Locust cannot obtain this information unless it directly makes requests to the endpoint).'''
    response_time = int((time.time() - start_time)) * 1000 # Convert to MS
    logger.info(f'Successful event! Request Type: {request_type}, Name: {name}, Response Time {response_time}, Response Length {response_length}')
    try:
      events.request.fire(
          request_type=request_type,
          name=name,
          response_time=response_time,
          response_length=response_length
      )
    except:
      logger.info("Failure")
    

  def _record_failure(self, start_time: float, request_type: str, name: str, e: Exception) -> None:
    '''Records a failed request to Locust.'''
    response_time = int((time.time() - start_time)) # Convert to MS
    logger.info(f'Failed event! Request Type: {request_type}, Name: {name}, Response Time {response_time}, Exception {e}')
    events.request.fire(
        request_type=request_type,
        name=name,  # Name of the task or operation
        response_time=response_time,
        exception=e
    )

  @task(100)
  def next_token(self):
    '''Models the basic usecase of using NNSight for next token prediction.'''
    word = 'ayy' # TODO: Consider sampling from natural language
    start_time = time.time()

    try:
      if self.deterministic:
        query = word # TODO: If using natural language, have char length configurable
      else:
        query = word * random.randint(1,20) # TODO: Make char length bounds configurable

      # TODO: Be able to configure how many tokens are generated
      # TODO: Have option for `generate`
      with self.model.trace(query, remote=True):
        output = self.model.output.save()
      self._record_success(start_time, 'next_token', 'name', len(output))

    except Exception as e:
      #self._record_failure(start_time, 'next_token', 'name', e)
      pass
      

  @task(0)
  def layer_selector(self):
    '''Models the selection of the activations of an intermediate layer.'''
    start_time = time.time()

    try:
      if self.deterministic:
        layer = -1 # TODO: Configurable
        query = 'hello world'
      else:
        layer = random.randint(0,self.n_layers-1)
        n_char = random.randint(1,1000) # TODO: Config
        query = gen_rand_str(n_char)

      with self.model.trace(query, remote=True):
        output = self.model.transformer.h[layer].output.save()
      self._record_success(start_time, 'layer_selector', 'name', len(output))
    except Exception as e:
      #self._record_failure(start_time, 'layer_selector', 'name', e)
      pass

  @task(0)
  def memory_user(self):
    '''Simulates a high memory usecase, where the user runs a large number of invokes and saves a lot of outputs'''
    # TODO: The design of this needs a lot of work.
    start_time = time.time()
    outputs = []
    try:
      if self.deterministic:
        n_invokes = 10
        queries = ['hello world'] * 10
      else:
        n_invokes = random.randint(1,20)
        queries = [gen_rand_str(random.randint(1,1000)) for _ in range(n_invokes)]

      with self.model.trace(remote=True) as tracer:
        for i, query in enumerate(queries):
          with tracer.invoke(query):
            layer = -1 if self.deterministic else random.randint(0,self.n_layers-1)
            outputs.append(self.model.transformer.h[layer].output.save())
      self._record_success(start_time, 'memory_user', 'name', len(outputs))

    except Exception as e:
      #self._record_failure(start_time, 'memory_user', 'name', e)
      pass

class NNSightUser(HttpUser):
  tasks = [NNSightTasks]
  wait_time = between(1,5) # TODO: Config
  host = config['base_url']
  config = config
