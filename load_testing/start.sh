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

# Run Locust, allowing it to pick up settings from locust.conf
locust -f locustfile.py $USER_CLASS