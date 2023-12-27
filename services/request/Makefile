
build_base:

	docker build -t request_base:latest -f base.dockerfile .

build_service:

	tar -hczvf src.tar.gz src

	docker build -t request:latest -f service.dockerfile .

	rm src.tar.gz