# Locust Load Testing Framework

This project provides a framework for performing load testing and benchmarking of API requests on NDIF using [Locust](https://locust.io/). The framework allows flexibility through configuration files and supports tasks like token generation and model layer selection.

## Directory Structure

```
benchmark_tasks.py      # Task set for benchmarking
load_testing_tasks.py    # Task set for load testing
util.py                 # Utility functions
common_tasks.py         # Shared functionality between task sets
LocustBackend.py        # Backend logic
requirements.txt        # Dependencies
config/
  locust_config.yml     # Custom configuration settings
locust.conf             # General Locust settings
start.sh                # Bash script for starting load tests
locustfile.py           # Main entry point for running Locust tests
```

## Configuration

### locust.conf

Defines Locust's settings, such as the number of users and spawn rate:

```ini
users = 10
spawn-rate = 2
run-time = 1m
headless = true
stop-timeout = 300
loglevel = info
```

### locust_config.yml

Contains custom task settings such as API key management, fake IP generation, and task-specific configurations:

```yaml
custom:
  generate_fake_ip: true
  base_url: "http://localhost:5001"
  api_keys:
    random: true
  model_keys:
    random: true
    keys:
      - "openai-community/gpt2"

load_testing:
  max_sequence_length: 20

benchmarking:
  min_num_tokens: 1
  max_num_tokens: 1000000
  increment_strategy: "multiply"
  increment_value: 10
```

## Tasks

### Load Testing Tasks

Simulates basic remote execution scenarios such as token generation and model layer selection:

### Benchmarking Tasks

Benchmarks system performance by incrementally increasing token size:

## Running Tests

You can run the load tests using the provided `start.sh` script:

```bash
./start.sh load_testing
./start.sh benchmarking
```

## Dependencies

Install the required dependencies:

```bash
pip install -r requirements.txt
```