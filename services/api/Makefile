
build_base:

	docker build -t api_base:latest -f base.dockerfile .

build_service:

	tar -hczvf src.tar.gz src

	docker build -t api:latest -f service.dockerfile .

	rm src.tar.gz