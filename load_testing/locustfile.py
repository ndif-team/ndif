import logging
import os
import yaml
from dotenv import load_dotenv
from locust import between, events, HttpUser
from locust.env import Environment
from locust.stats import stats_history, stats_printer
from typing import Any

from benchmark_tasks import BenchmarkTasks
from load_testing_tasks import LoadTestingTasks
from util import get_log_level

# Load configuration
config_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config', 'locust_config.yml')
with open(config_file, 'r') as f:
  config = yaml.safe_load(f)

logger_name = config['custom']['logging']['name']
log_level = get_log_level(config['custom']['logging']['level'])

logger = logging.getLogger(logger_name)
logger.setLevel(log_level)

# Load API keys
load_dotenv()
api_keys = os.getenv("API_KEYS", "").split(",")
config['custom']['api_keys']['keys'] = api_keys

# Get the wait times
wait_time_min = config['locust']['wait_time_min']
wait_time_max = config['locust']['wait_time_max']

# Define the HttpUser class for load testing tasks
class LoadTestingUser(HttpUser):
  """User class for standard load testing tasks."""
  tasks = [LoadTestingTasks]
  wait_time = between(wait_time_min, wait_time_max)
  host = config['custom']['base_url']
  config = config
  api_keys = api_keys
  logger = logger

# Define the HttpUser class for benchmarking tasks
class BenchmarkUser(HttpUser):
    """User class for benchmarking tasks with incrementally increasing load."""
    tasks = [BenchmarkTasks]
    wait_time = between(wait_time_min, wait_time_max)
    host = config['custom']['base_url']
    config = config
    api_keys = api_keys
    logger = logger