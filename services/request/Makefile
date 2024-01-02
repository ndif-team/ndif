
build_base:

	docker build --no-cache -t request_base:latest -f base.dockerfile .

build_service:

	tar -hczvf src.tar.gz src

	docker build --no-cache -t request:latest -f service.dockerfile .

	rm src.tar.gz