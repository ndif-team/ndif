import logging
import os
import random
import torch
import tqdm
import yaml
from dotenv import load_dotenv
from locust import HttpUser, TaskSet, task, between
from typing import Any

import nnsight
from nnsight.schema.Request import RequestModel
from nnsight.schema.Response import ResponseModel
from util import gen_rand_str
from LocustBackend import LocustBackend

logger = logging.getLogger('load_testing')
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
    self.n_layers = self.model.config.n_layer

    self.backend = LocustBackend()
    # TODO: Give this user a unique IP base_url

  def ndif_request(self, request: RequestModel, name: str) -> Any:
    """
    Performs the remote backend request flow using Locust's HTTP client to capture network stats.

    Args:
        request (RequestModel): The request model to send.

    Returns:
        Any: The processed result from the remote backend.
    """
    # Serialize request model to JSON
    request_json = request.model_dump(exclude=["id","received"])
    self.job_id = request.id

    # Define headers
    headers = {"ndif-api-key": self.api_key}

    #Define URLs
    request_url = f"{self.base_url}/request"
    response_url = f"{self.base_url}/response/{self.job_id}" if self.job_id else None
    result_url_template = f"{self.base_url}/result/{{job_id}}"

    # Submit the request
    with self.client.post("/request", json=request_json, headers=headers, catch_response=True, name=name) as response:
      if response.status_code != 200:
        response.failure(f"Failed to submit request: {response.text}")
        raise Exception(f"Request submission failed: {response.text}")
      
      # Parse the response
      try:
        response_data = response.json()
        response_model = ResponseModel(**response_data)
      except Exception as e:
        response.failure(f"Failed to parse response: {e}")
        raise

      if response_model.status == ResponseModel.JobStatus.ERROR:
        response.failure(f"Server returned error: {response_model}")
        raise Exception(str(response_model))
    
    # If the job is blocking and completed, download the result
    if response_model.status == ResponseModel.JobStatus.COMPLETED:
      result_url = result_url_template.format(job_id=response_model.id)
      with self.client.get(f"/result/{response_model.id}", stream=True, headers=headers, catch_response=True, name=name) as result_response:
        if result_response.status_code != 200:
          result_response.failure(f"Failed to download result: {result_response.text}")
          raise Exception(f"Result download failed: {result_response.text}")

        # Initialize BytesIO to store the downloaded bytes
        result_bytes = io.BytesIO()

        # Get total size for progress bar
        total_size = float(result_response.headers.get("Content-length", 0))

        # Use tqdm for progress tracking (optional in load tests)
        with tqdm(total=total_size, unit="B", unit_scale=True, desc="Downloading result") as progress_bar:
          for chunk in result_response.iter_content(chunk_size=8192):
            if chunk:
              progress_bar.update(len(chunk))
              result_bytes.write(chunk)

          # Move cursor to the beginning
          result_bytes.seek(0)

          # Load the result using torch
          try:
              loaded_result = torch.load(result_bytes, map_location="cpu", weights_only=False)
              result_model = ResultModel(**loaded_result)
          except Exception as e:
              raise Exception(f"Failed to load result: {e}")

          # Handle the result
          self.handle_result(result_model.value)

          # Close the BytesIO stream
          result_bytes.close()

    return response_model

  @task
  def next_token(self):
    '''Models the basic usecase of using NNSight for next token prediction.'''
    word = 'ayy' # TODO: Consider sampling from natural language
    if self.deterministic:
      query = word # TODO: If using natural language, have char length configurable
    else:
      query = word * random.randint(1,20) # TODO: Make char length bounds configurable

    # TODO: Be able to configure how many tokens are generated
    # TODO: Have option for `generate`
    with self.model.trace(query, remote=True, backend=self.backend):
      output = self.model.output.save()

    # Make request
    self.ndif_request(self.backend.request_model, name='next_token')

  @task
  def layer_selector(self):
    '''Models the selection of the activations of an intermediate layer.'''
    if self.deterministic:
      layer = -1 # TODO: Configurable
      query = 'hello world'
    else:
      layer = random.randint(0,self.n_layers-1)
      n_char = random.randint(1,1000) # TODO: Config
      query = gen_rand_str(n_char)

    # Grab RequestModel
    with self.model.trace(query, remote=True, backend=self.backend):
      output = self.model.transformer.h[layer].output.save()

    # Make request
    self.ndif_request(self.backend.request_model, name='layer_selector')

  @task
  def memory_user(self):
    '''Simulates a high memory usecase, where the user runs a large number of invokes and saves a lot of outputs'''
    # TODO: The design of this needs a lot of work.
    outputs = []
    if self.deterministic:
      n_invokes = 10
      queries = ['hello world'] * 10
    else:
      n_invokes = random.randint(1,20)
      queries = [gen_rand_str(random.randint(1,1000)) for _ in range(n_invokes)]

    with self.model.trace(remote=True, backend=self.backend) as tracer:
      for i, query in enumerate(queries):
        with tracer.invoke(query):
          layer = -1 if self.deterministic else random.randint(0,self.n_layers-1)
          outputs.append(self.model.transformer.h[layer].output.save())

    # Make request
    self.ndif_request(self.backend.request_model, name='memory_user')

class NNSightUser(HttpUser):
  tasks = [NNSightTasks]
  wait_time = between(1,5) # TODO: Config
  host = config['base_url']
  config = config
