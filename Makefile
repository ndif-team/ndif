IP_ADDR := $(shell hostname -I | awk '{print $$1}')

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

	export HOST_IP=${IP_ADDR} && docker compose -f compose/$(word 2,$(MAKECMDGOALS))/docker-compose.yml up --detach

down:

	export HOST_IP=${IP_ADDR} && docker compose -f compose/$(word 2,$(MAKECMDGOALS))/docker-compose.yml down

ta:
	make down dev
	make build_all_service
	make up dev
	