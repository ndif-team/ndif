
IP_ADDR := $(shell if [ "$$(uname)" = "Darwin" ]; then echo "host.docker.internal"; else (hostname -I | awk '{print $$1}' || echo "127.0.0.1"); fi)
N_DEVICES := $(shell command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L | wc -l || echo 0)

build:
	docker buildx build --build-arg NAME=api -t api:latest -f docker/Dockerfile .
	docker buildx build --build-arg NAME=ray -t ray:latest -f docker/Dockerfile .

up:
	export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) && \
	docker compose -p dev -f docker/docker-compose.yml up; \

down:
	export HOST_IP=${IP_ADDR} N_DEVICES=${N_DEVICES} && docker compose -p dev -f docker/docker-compose.yml down

ta:
	make down
	make build
	make up
