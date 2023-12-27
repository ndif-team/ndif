
build_base:

	docker build -t model_base:latest -f base.dockerfile .

build_service:

	tar -hczvf src.tar.gz src

	docker build -t model:latest -f service.dockerfile .

	rm src.tar.gz