FROM model_base:latest

RUN useradd -ms /bin/bash celery \
    && chown -R celery: /opt/conda/envs/service/ 

USER celery

WORKDIR /wd

COPY ./src.tar.gz /src.tar.gz

RUN tar -xvf src.tar.gz\
    && rm src.tar.gz

CMD bash start.sh

