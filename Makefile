
IP_ADDR := $(shell hostname -I | awk '{print $$1}')
N_DEVICES := $(shell command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L | wc -l || echo 0)

build:
	docker buildx build --file docker/Dockerfile --tag ndif_base:latest --target ndif_base .
	docker buildx build --build-arg NAME=api -t api:latest -f docker/Dockerfile .
	docker buildx build --build-arg NAME=queue -t queue:latest -f docker/Dockerfile .
	docker buildx build --build-arg NAME=ray -t ray:latest -f docker/Dockerfile .

up:
	export HOST_IP=$(IP_ADDR) N_DEVICES=$(N_DEVICES) && \
	docker compose -p dev -f docker/docker-compose.yml up --detach; \

down:
	export HOST_IP=${IP_ADDR} N_DEVICES=${N_DEVICES} && docker compose -p dev -f docker/docker-compose.yml down

ta:
	make down
	make build
	make up
