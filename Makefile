-include .config

IP_ADDR := $(shell hostname -I | awk '{print $$1}')
N_DEVICES := $(shell nvidia-smi  -L | wc -l)

# Treat "up", "down" and "ta" as targets (not files)
.PHONY: up down ta

# Define valid environments
VALID_ENVS := dev prod delta

# Default environment
DEFAULT_ENV ?= dev

# Configs for local nnsight installation
DEV_NNS ?= False
NNS_PATH ?= ~/nnsight

# Function to check if the environment is valid
check_env = $(if $(filter $(1),$(VALID_ENVS)),,$(error Invalid environment '$(1)'. Use one of: $(VALID_ENVS)))

# Function to set environment and print message if no environment was specified
set_env = $(eval ENV := $(if $(filter $(words $(MAKECMDGOALS)),1),$(DEFAULT_ENV),$(word 2,$(MAKECMDGOALS)))) \
          $(if $(filter $(words $(MAKECMDGOALS)),1),$(info Using default environment: $(DEFAULT_ENV)),)

build_base:

	docker build --no-cache -t $(NAME)_base:latest -f ../base.dockerfile .

build_service:

	tar -hczvf src.tar.gz src

	docker build --no-cache --build-arg NAME=$(NAME) -t $(NAME):latest -f ../service.dockerfile  . 

	rm src.tar.gz

build_all_base:
	$(call set_env)
	$(call check_env,$(ENV))
	cd services/api && make -f ../../Makefile build_base NAME=api
	cd services/ray_head && make -f ../../Makefile build_base NAME=ray_head
	@if [ "$(ENV)" = "prod" ]; then \
		cd services/ray_worker && make -f ../../Makefile build_base NAME=ray_worker; \
	fi

build_all_service:
	$(call set_env)
	$(call check_env,$(ENV))
	cd services/api && make -f ../../Makefile build_service NAME=api
	cd services/ray_head && make -f ../../Makefile build_service NAME=ray_head
	@if [ "$(ENV)" = "prod" ]; then \
		cd services/ray_worker && make -f ../../Makefile build_service NAME=ray_worker; \
	fi

build:
	$(call set_env)
	$(call check_env,$(ENV))
	make build_all_base
	make build_all_service
	make up $(ENV)

up:
	$(call set_env)
	$(call check_env,$(ENV))
	@if [ "$(ENV)" = "dev" ] && [ "$(DEV_NNS)" = "True" ]; then \
		export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) NNS_PATH=$(NNS_PATH) && \
		docker compose -f compose/dev/docker-compose.yml -f compose/dev/docker-compose.nnsight.yml up --detach; \
	else \
		export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) NNS_PATH=$(NNS_PATH) && \
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
