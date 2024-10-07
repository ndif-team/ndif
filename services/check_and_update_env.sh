#!/bin/bash

set -e

check_and_update_environment() {
    echo "Checking for environment updates..."
    source activate service
    
    # Compare current environment with environment.yml
    if conda env export --name service | grep -v "^prefix: " > /tmp/current_env.yml && \
       diff -q /tmp/current_env.yml environment.yml > /dev/null 2>&1; then
        echo "Environment is up-to-date."
    else
        echo "Differences detected in environment. Updating..."
        conda env update --file environment.yml --prune
        echo "Environment updated successfully."
    fi
    
    rm -f /tmp/current_env.yml
}

# Check and update the environment
check_and_update_environment

# Start the service
echo "Starting the service..."
source activate service && bash ./start.sh