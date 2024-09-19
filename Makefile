IP_ADDR := $(shell hostname -I | awk '{print $$1}')
N_DEVICES := $(shell nvidia-smi  -L | wc -l)

# Treat "up", "down" and "ta" as targets (not files)
.PHONY: up down ta

# Define valid environments
VALID_ENVS := dev prod delta

# Default environment
DEFAULT_ENV := dev

# Function to check if the environment is valid
check_env = $(if $(filter $(1),$(VALID_ENVS)),,$(error Invalid environment '$(1)'. Use one of: $(VALID_ENVS)))

build_base:

	docker build --no-cache -t $(NAME)_base:latest -f ../base.dockerfile .

build_service:

	tar -hczvf src.tar.gz src

	docker build --no-cache --build-arg NAME=$(NAME) -t $(NAME):latest -f ../service.dockerfile  . 

	rm src.tar.gz

build_all_base:

	cd services/api && make -f ../../Makefile build_base NAME=api
	cd services/ray_head && make -f ../../Makefile build_base NAME=ray_head
	cd services/ray_worker && make -f ../../Makefile build_base NAME=ray_worker

build_all_service:

	cd services/api && make -f ../../Makefile build_service NAME=api
	cd services/ray_head && make -f ../../Makefile build_service NAME=ray_head
	cd services/ray_worker && make -f ../../Makefile build_service NAME=ray_worker
up:
	$(call check_env,$(word 2,$(MAKECMDGOALS)))
	export HOST_IP=${IP_ADDR} N_DEVICES=${N_DEVICES} && docker compose -f compose/$(word 2,$(MAKECMDGOALS))/docker-compose.yml up --detach

down:
	$(call check_env,$(word 2,$(MAKECMDGOALS)))
	export HOST_IP=${IP_ADDR} N_DEVICES=${N_DEVICES} && docker compose -f compose/$(word 2,$(MAKECMDGOALS))/docker-compose.yml down

ta:
	$(eval ENV := $(if $(filter $(words $(MAKECMDGOALS)),1),$(DEFAULT_ENV),$(word 2,$(MAKECMDGOALS))))
	$(call check_env,$(ENV))
	make down $(ENV)
	make build_all_service
	make up $(ENV)

# Consumes the second argument (e.g. 'dev', 'prod') so it doesn't cause an error.
%:
	@: