# NDIF Development Guide

This guide explains how to set up a development environment, install dependencies, and get started with contributing to the `NDIF` project.

## Prerequisites

- Python 3.10
- Docker
- Docker Compose


## Setup

## 1. Install Conda
If you don’t have Conda installed, download and install Anaconda or Miniconda from the [official Conda website](https://docs.conda.io/en/latest/miniconda.html).

## 2. Create Conda Environment

Fork the `NDIF` repository (or clone it directly) to your local machine. Then create a new Conda virtual environment:

```sh
conda create -n ndif-dev python=3.10
conda activate ndif-dev
```

## 3. Install NNsight 

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

## Building and Running `NDIF`

### 1. Build and start the development environment

For first-time setup, use:

```sh
make build
```

If you’ve made changes to the codebase but did not modify the `environment.yml` files, you can quickly rebuild the services using:

```sh
make ta
```

This method is faster than running `make build` again.

### 2. Verify server status

After building the `NDIF` containers, you can check the docker logs to verify the services are running correctly.
```sh
docker logs dev-api-1
```
You should expect to see a message like `Application startup complete.` in the api service log.

### 3. Run tests

```sh
python scripts/test.py
```

For more comprehensive testing, install pytest
```sh
conda activate ndif-dev
pip install pytest==8.4.0
pytest
```


This will send a test NNsight request to the API service running in the local container.

## Additional Commands

- To start the deployment environment without rebuilding:

```sh
make up
```

- To stop the development environment:

```sh
make down
```

- To rebuild services and restart the environment (useful during development):

```sh
make ta
```

_Note: Modifying any of the `environment.yml` files will require you to rebuild from scratch._

# Environment Configuration

The project uses separate `.env` files for development and production environments:

- Development: `compose/dev/.env`
- Production: `compose/prod/.env`

For most users, only the development environment is necessary. The production environment is configured separately and is not required for local development.

### Note

The Makefile includes configurations for both development and production environments. As an end user or developer, you'll primarily interact with the development environment. The production environment settings are managed separately and are not typically needed for local development work.