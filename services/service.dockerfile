ARG NAME

FROM ${NAME}_base:latest

COPY ./src.tar.gz ./src.tar.gz

COPY ./start.sh ./start.sh

RUN tar -xvf ./src.tar.gz\
    && rm ./src.tar.gz

SHELL ["/bin/bash", "-c"]

CMD source activate service && bash ./start.sh

