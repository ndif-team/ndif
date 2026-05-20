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
.PHONY: check-nnsight check-node build up down ta build-standalone push-standalone run-standalone dashboard-frontend

# =============================================================================
# Dashboard frontend (built on the host, copied into both dashboard + standalone
# images). Same dist also bundles into the pip package down the line.
# =============================================================================

FRONTEND_DIR  := src/ndif/services/dashboard/frontend
FRONTEND_DIST := $(FRONTEND_DIR)/dist/index.html
FRONTEND_SRC  := $(FRONTEND_DIR)/package.json $(FRONTEND_DIR)/package-lock.json \
                 $(FRONTEND_DIR)/index.html $(FRONTEND_DIR)/vite.config.ts \
                 $(FRONTEND_DIR)/tsconfig.json \
                 $(shell find $(FRONTEND_DIR)/src $(FRONTEND_DIR)/public 2>/dev/null)

check-node:
	@if ! command -v npm >/dev/null 2>&1; then \
		echo "ERROR: npm not found on PATH (needed to build the dashboard frontend)."; \
		echo "  Install Node 20+ from https://nodejs.org or via nvm."; \
		exit 1; \
	fi

# File-target: rebuild dist/ only when frontend sources change. The `.PHONY`
# alias below is what humans type; this is what Make uses to skip the rebuild.
$(FRONTEND_DIST): $(FRONTEND_SRC) | check-node
	cd $(FRONTEND_DIR) && npm ci && npm run build

dashboard-frontend: $(FRONTEND_DIST)

build: dashboard-frontend
	docker buildx build --build-arg NAME=api       -t api:latest       -f docker/Dockerfile .
	docker buildx build --build-arg NAME=ray       -t ray:latest       -f docker/Dockerfile .
	docker buildx build --build-arg NAME=dashboard -t dashboard:latest -f docker/Dockerfile .

up: check-nnsight
	export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) NNSIGHT_PATH=$(NNSIGHT_PATH) && \
	docker compose -p dev -f docker/docker-compose.yml up --detach; \

down:
	export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) NNSIGHT_PATH=$(NNSIGHT_PATH) && \
	docker compose -p dev -f docker/docker-compose.yml down

ta: down build up

# =============================================================================
# Standalone all-in-one image (Docker Hub: ndif/ndif).
# =============================================================================

STANDALONE_IMAGE   ?= ndif/ndif
# Pull the version from pyproject.toml so we don't drift.
STANDALONE_VERSION := $(shell python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")

build-standalone: dashboard-frontend
	docker buildx build --build-arg NAME=all \
		-t $(STANDALONE_IMAGE):latest \
		-t $(STANDALONE_IMAGE):$(STANDALONE_VERSION) \
		-f docker/Dockerfile .

push-standalone:
	docker push $(STANDALONE_IMAGE):latest
	docker push $(STANDALONE_IMAGE):$(STANDALONE_VERSION)
	@echo
	@echo "Pushed $(STANDALONE_IMAGE):latest and $(STANDALONE_IMAGE):$(STANDALONE_VERSION)"
	@echo "Remember to paste docker/DOCKERHUB.md into the Docker Hub 'Overview' tab on first push / major edits."

run-standalone:
	docker run --rm -it --gpus all \
		-p 5001:5001 -p 8081:8081 -p 27018:27018 -p 8265:8265 \
		-v $$HOME/.cache/huggingface:/root/.cache/huggingface \
		$(STANDALONE_IMAGE):latest
