-include .config

IP_ADDR := $(shell hostname -I | awk '{print $$1}')
N_DEVICES := $(shell command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L | wc -l || echo 0)
COMMIT_HASH := $(shell git rev-parse HEAD)

# Treat "up", "down" and "ta" as targets (not files)
.PHONY: up down ta

# Define valid environments
VALID_ENVS := dev prod delta harness

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
	tar -hczvf src.tar.gz --directory=services/$(NAME) src
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
	make build_conda NAME=ray_head
	make build_conda NAME=harness
	@if [ "$(ENV)" = "prod" ]; then \
		make build_conda NAME=ray_worker; \
	fi

build_all_service:
	$(call set_env)
	$(call check_env,$(ENV))
	make build_service NAME=api
	make build_service NAME=ray_head
	make build_service NAME=harness
	@if [ "$(ENV)" = "prod" ]; then \
		make build_service NAME=ray_worker; \
	fi

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
		export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) NNS_PATH=$(NNS_PATH) && \
		docker compose -f compose/dev/docker-compose.yml -f compose/dev/docker-compose.nnsight.yml up --detach; \
	elif [ "$(ENV)" = "harness" ]; then \
		export export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) COMMIT_HASH=$(COMMIT_HASH) ENV=$(ENV) && \
		docker compose --env-file compose/dev/.env  --env-file services/harness/.config.harness -f compose/dev/docker-compose.harness.yml up --detach; \
	else \
		export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) NNS_PATH=$(NNS_PATH) && \
		docker compose -f compose/$(ENV)/docker-compose.yml up --detach; \
	fi

down:
	$(call set_env)
	$(call check_env,$(ENV))
	@if [ "$(ENV)" = "harness" ]; then \
		export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) COMMIT_HASH=$(COMMIT_HASH) ENV=$(ENV) && \
		docker compose -f compose/dev/docker-compose.harness.yml down; \
	else \
		export HOST_IP=${IP_ADDR} N_DEVICES=${N_DEVICES} && docker compose -f compose/$(ENV)/docker-compose.yml down; \
	fi

test:
	@if ! docker ps --filter "name=dev-harness-1" --format '{{.Names}}' | grep -q "dev-harness-1"; then \
		make up harness; \
	fi

	docker exec dev-harness-1 bash -c "source activate service && bash /start.sh"

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
