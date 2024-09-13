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
from benchmark_tasks import BenchmarkTasks
from load_testing_tasks import LoadTestingTasks
from util import gen_rand_str

logger = logging.getLogger('load_testing')
logger.setLevel(logging.INFO)


# Load configuration
config_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config', 'locust_config.yml')
with open(config_file, 'r') as f:
  config = yaml.safe_load(f)

# Load API keys
load_dotenv()
api_keys = os.getenv("API_KEYS", "").split(",")

# Define the HttpUser class for load testing tasks
class LoadTestingUser(HttpUser):
  """User class for standard load testing tasks."""
  tasks = [LoadTestingTasks]
  wait_time = between(1,5) # TODO: Config
  host = config['base_url']
  config = config
  api_keys = api_keys

# Define the HttpUser class for benchmarking tasks
class BenchmarkUser(HttpUser):
    """User class for benchmarking tasks with incrementally increasing load."""
    tasks = [BenchmarkTasks]
    wait_time = between(1,5) # TODO: Config
    host = config['base_url']
    config = config
    api_keys = api_keys