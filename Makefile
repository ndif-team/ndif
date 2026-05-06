# Load .env.example for defaults, .env for overrides
-include .env.example
-include .env
export

IP_ADDR := $(shell hostname -I | awk '{print $$1}')
N_DEVICES := $(shell command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L | wc -l || echo 0)

# Resolve the local nnsight install path so it can be bind-mounted into the ray
# container. Override by exporting NNSIGHT_PATH before running make.
NNSIGHT_PATH ?= $(shell python -c "import nnsight, os; print(os.path.dirname(nnsight.__file__))" 2>/dev/null)

check-nnsight:
	@if [ -z "$(NNSIGHT_PATH)" ]; then \
		echo "ERROR: could not locate an installed nnsight package."; \
		echo "  Install it (e.g. 'pip install nnsight' or 'pip install -e /path/to/nnsight')"; \
		echo "  or export NNSIGHT_PATH=/absolute/path/to/nnsight before running make."; \
		exit 1; \
	fi
	@echo "Using nnsight from: $(NNSIGHT_PATH)"

# All targets are recipes (no file-target collisions). Without this, the
# stale ``build/`` dir at repo root makes ``make build`` a no-op
# (Make treats the target as "up to date" because a directory by that
# name exists).
.PHONY: check-nnsight build up down ta

build:
	docker buildx build --build-arg NAME=api -t api:latest -f docker/Dockerfile .
	docker buildx build --build-arg NAME=ray -t ray:latest -f docker/Dockerfile .
	docker buildx build -t dashboard:latest -f docker/Dockerfile.dashboard .

up: check-nnsight
	export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) NNSIGHT_PATH=$(NNSIGHT_PATH) && \
	docker compose -p dev -f docker/docker-compose.yml up --detach; \

down:
	export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) NNSIGHT_PATH=$(NNSIGHT_PATH) && \
	docker compose -p dev -f docker/docker-compose.yml down

ta: down build up
