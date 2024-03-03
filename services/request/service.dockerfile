FROM request_base:latest

RUN useradd -ms /bin/bash celery \
    && chown -R celery: /opt/conda/envs/service/ 

USER celery

WORKDIR /wd

COPY ./src.tar.gz /wd/src.tar.gz

RUN tar -xvf src.tar.gz\
    && rm src.tar.gz

CMD conda run --no-capture-output -n service celery -A src.celery.tasks worker --loglevel=INFO --api_url $API_URL -Q request --concurrency 1

