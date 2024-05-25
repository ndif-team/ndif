# Development

Welcome to the development guide for the `NDIF` project! This document explains how to set up a development environment, install dependencies, and get started with contributing to the project.

## Prerequisites

- Python 3.10
- Docker
- Docker Compose


## Installing dependencies

If you haven't already, install Conda by downloading and installing Anaconda or Miniconda from the [official Conda website](https://docs.conda.io/en/latest/miniconda.html).

Fork the NDIF repository to your local machine. Create a new Conda virtual environment for the `NDIF` project with required dependencies. Replace `ndif-dev` with your desired environment name.
```sh
conda env create --name ndif-dev -f requirements.yml
conda activate ndif-dev
```


## Testing

1. Build the base environment

First, build the base environment using the `make build_base` command. This will set up the base images.
```sh
make build_base
```

2. Build the service

Next, build the service using the `make build_service` command.
```sh
make build_service
```

3. Start the development containers

After building the base environment and the service, start the NDIF docker containers.
```sh
make up-dev
```

4. Inspect server status

After building the NDIF containers, you can check the docker logs to verify the services are running correctly.
```sh
docker logs ndif-api-dev-1
```
You should expect to see a message like `Application startup complete.` in the api service log.

5. Run tests

We can now execute the test which will send 3 nnsight requests to the api service running inside the local container.
```sh
python dev-test.py
```






