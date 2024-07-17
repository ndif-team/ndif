
# NDIF Development Guide

This guide explains how to set up a development environment, install dependencies, and get started with contributing to the `NDIF` project.

## Prerequisites

- Python 3.10
- Docker
- Docker Compose


## Setup

### 1. Install Conda

If you haven't already, install Conda by downloading and installing Anaconda or Miniconda from the [official Conda website](https://docs.conda.io/en/latest/miniconda.html).

### 2. Create Conda Environment

Fork or clone the `NDIF` repository to your local machine. Then create a new Conda virtual environment:

```sh
conda create -n ndif-dev python=3.10
conda activate ndif-dev
```

### 3. Install NNsight 

Choose one of the following methods:

a. Via pip (simple)

```
pip install nnsight
```

b. From repository (recommended for specific branches)

```sh
git clone https://github.com/nnsight/nnsight.git
cd nnsight
git checkout <branch-name>  # e.g., 0.3
pip install -e .
```

### 4. Configure Docker Compose

Modify the Docker Compose file for your system

1. Edit the file:

```sh
vi compose/(dev|prod)/docker-compose.yml 
```

2. Update the Hugging Face cache path:

```sh
- /disk/u/[YOUR BAULAB USERNAME]/.cache/huggingface/hub/:/root/.cache/huggingface/hub
```

3. Adjust GPU settings (check your system with `nvidia-smi`):

```sh
- driver: nvidia
 count: 1
 capabilities: [ gpu ]
```

## Building and Running `NDIF`

1. Build the base environment

First, build the base environment using the `make build_all_base` command. This will set up the base images.
```sh
make build_all_base
```

2. Build the service

Next, build the service using the `make build_all_service` command.
```sh
make build_all_service
```

3. Start the development containers

After building the base environment and the service, start the `NDIF` docker containers.
```sh
make up-dev
```

4. Verify server status

After building the `NDIF` containers, you can check the docker logs to verify the services are running correctly.
```sh
docker logs dev-api-1
```
You should expect to see a message like `Application startup complete.` in the api service log.

5. Run tests

```sh
python scripts/test.py
```

This will send 3 NNsight requests to the API service running in the local container.
