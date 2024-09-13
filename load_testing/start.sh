#!/bin/bash

# Check if the task argument is provided
if [ -z "$1" ]; then
  echo "Usage: ./run_load_test.sh <task>"
  echo "Tasks:"
  echo "  load_testing   - Runs the load testing tasks"
  echo "  benchmarking   - Runs the benchmarking tasks"
  exit 1
fi

# Assign task type based on the argument
if [ "$1" == "load_testing" ]; then
  USER_CLASS="LoadTestingUser"
elif [ "$1" == "benchmarking" ]; then
  USER_CLASS="BenchmarkUser"
else
  echo "Invalid task. Please use 'load_testing' or 'benchmarking'."
  exit 1
fi

# Define other optional parameters for the load test (you can adjust these as needed)
NUM_USERS=${2:-10}    # Default to 10 users if not provided
SPAWN_RATE=${3:-1}    # Default to 1 user per second if not provided
RUN_TIME=${4:-"5m"}   # Default to 5 minutes if not provided
LOCUST_FILE="locustfile.py"  # Locust file to use

# Run the load test with Locust
locust -f $LOCUST_FILE $USER_CLASS --headless -u $NUM_USERS -r $SPAWN_RATE --run-time $RUN_TIME
