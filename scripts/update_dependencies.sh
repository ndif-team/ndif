#!/bin/bash

# Check if environment argument is provided
if [ $# -eq 0 ]; then
    echo "No environment specified. Usage: $0 <environment>"
    exit 1
fi

ENV=$1
COMPOSE_FILE="compose/$ENV/docker-compose.yml"

# Check if the docker-compose file exists
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "Docker Compose file not found: $COMPOSE_FILE"
    exit 1
fi

# Set environment variables
export HOST_IP=$(hostname -I | awk '{print $1}')
export N_DEVICES=$(nvidia-smi -L | wc -l)

# Array of services to update
services=("api" "ray-head" "ray-worker")

for service in "${services[@]}"; do
    echo "Updating $service..."
    
    echo "Trying to find container for service: $service"
    echo "Using compose file: $COMPOSE_FILE"
    
    # List all containers
    echo "All containers:"
    docker compose -f $COMPOSE_FILE ps
    
    # Try to get the container ID
    container_id=$(docker compose -f $COMPOSE_FILE ps -q $service)
    
    echo "Command used: docker compose -f $COMPOSE_FILE ps -q $service"
    echo "Result (container_id): '$container_id'"
    
    if [ -z "$container_id" ]; then
        echo "Container for $service is not running or not found. Skipping..."
        continue
    fi
        
    # Copy the new environment.yml file to the container
    # Convert dashes to underscores for local path
    service_path=${service//-/_}

    docker cp services/$service_path/environment.yml $container_id:/tmp/environment.yml
    echo "Copied environment.yml to container"

    # Update the conda environment
    docker exec -it $container_id /bin/bash -c \
        "source activate service && conda env update --file /tmp/environment.yml --prune"
    
    echo "$service updated successfully."
done

echo "All services updated. Restarting services..."
docker compose -f $COMPOSE_FILE restart

echo "Update process completed."