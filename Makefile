build_base:

	cd services/api && make build_base
	cd services/request && make build_base
	cd services/model && make build_base


build_service:

	cd services/api && make build_service
	cd services/request && make build_service
	cd services/model && make build_service

up:
	docker compose -f ndif/docker-compose.yml up --detach

down:
	docker compose -f ndif/docker-compose.yml down

up-dev:
	docker compose -f compose/docker-compose-dev.yml up --detach

down-dev:
	docker compose -f compose/docker-compose-dev.yml down