
build_base:

	docker build --no-cache -t model_base:latest -f base.dockerfile .

build_service:

	tar -hczvf src.tar.gz src

	docker build --no-cache -t model:latest -f service.dockerfile .

	rm src.tar.gz