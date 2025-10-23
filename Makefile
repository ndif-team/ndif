-include .config

IP_ADDR := $(shell hostname -I | awk '{print $$1}')
N_DEVICES := $(shell command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L | wc -l || echo 0)

# Treat "up", "down" and "ta" as targets (not files)
.PHONY: up down ta

# Define valid environments
VALID_ENVS := dev prod delta

# Default environment
DEFAULT_ENV ?= dev

# Configs for local nnsight installation
DEV_NNS ?= False
NNS_PATH ?= ~/nnsight
TAG ?= latest

# Function to check if the environment is valid
check_env = $(if $(filter $(1),$(VALID_ENVS)),,$(error Invalid environment '$(1)'. Use one of: $(VALID_ENVS)))

# Function to set environment and print message if no environment was specified
set_env = $(eval ENV := $(if $(filter $(words $(MAKECMDGOALS)),1),$(DEFAULT_ENV),$(word 2,$(MAKECMDGOALS)))) \
          $(if $(filter $(words $(MAKECMDGOALS)),1),$(info Using default environment: $(DEFAULT_ENV)),)

build_base:
	docker build --no-cache -t ndif_base:$(TAG) -f docker/dockerfile.base .

build_conda:
	docker build --no-cache --build-arg NAME=$(NAME) --build-arg TAG=$(TAG) -t $(NAME)_conda:$(TAG) -f docker/dockerfile.conda .

build_service:
	cp docker/helpers/check_and_update_env.sh ./
	tar -hczvf src.tar.gz --directory=src/services/$(NAME) src
	docker build --no-cache --build-arg NAME=$(NAME) --build-arg TAG=$(TAG) -t $(NAME):$(TAG) -f docker/dockerfile.service  .
	rm src.tar.gz
	rm check_and_update_env.sh

build_all_base:
	$(call set_env)
	$(call check_env,$(ENV))
	make build_base

build_all_conda:
	$(call set_env)
	$(call check_env,$(ENV))
	make build_conda NAME=api
	make build_conda NAME=queue
	make build_conda NAME=ray

build_all_service:
	$(call set_env)
	$(call check_env,$(ENV))
	make build_service NAME=api
	make build_service NAME=queue
	make build_service NAME=ray

build_ci:
	docker buildx build --file docker/Dockerfile --tag ndif_base:latest --target ndif_base .
	cp docker/helpers/check_and_update_env.sh ./
	docker buildx build --build-arg NAME=api -t api:latest -f docker/Dockerfile .
	docker buildx build --build-arg NAME=queue -t queue:latest -f docker/Dockerfile .
	docker buildx build --build-arg NAME=ray -t ray:latest -f docker/Dockerfile .
	rm check_and_update_env.sh

build:
	$(call set_env)
	$(call check_env,$(ENV))
	make build_all_base
	make build_all_conda
	make build_all_service
	make up $(ENV)

up:
	$(call set_env)
	$(call check_env,$(ENV))
	@if [ "$(ENV)" = "dev" ] && [ "$(DEV_NNS)" = "True" ]; then \
		export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) NNS_PATH=$(NNS_PATH) \
		RAY_VERSION=$(TAG) API_VERSION=$(TAG) QUEUE_VERSION=$(TAG) && \
		docker compose -f compose/dev/docker-compose.yml -f compose/dev/docker-compose.nnsight.yml up --detach; \
	else \
		export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) NNS_PATH=$(NNS_PATH) \
		RAY_VERSION=$(TAG) API_VERSION=$(TAG) QUEUE_VERSION=$(TAG) && \
		docker compose -f compose/$(ENV)/docker-compose.yml up --detach; \
	fi

down:
	$(call set_env)
	$(call check_env,$(ENV))
	export HOST_IP=${IP_ADDR} N_DEVICES=${N_DEVICES} && docker compose -f compose/$(ENV)/docker-compose.yml down

ta:
	$(call set_env)
	$(call check_env,$(ENV))
	make down $(ENV)
	make build_all_service
	make up $(ENV)

save-vars:
	@echo "DEFAULT_ENV=$(DEFAULT_ENV)" > .config
	@echo "DEV_NNS=$(DEV_NNS)" >> .config
	@echo "NNS_PATH=$(NNS_PATH)" >> .config

reset-vars:
	@rm ./.config

# Consumes the second argument (e.g. 'dev', 'prod') so it doesn't cause an error.
%:
	@:
