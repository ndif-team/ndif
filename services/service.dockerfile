ARG NAME=api

FROM ${NAME}_base:latest

COPY ./src.tar.gz ./src.tar.gz
COPY ./start.sh ./start.sh
COPY ../check_and_update_env.sh ./check_and_update_env.sh
COPY ./environment.yml ./environment.yml

RUN tar -xvf ./src.tar.gz\
    && rm ./src.tar.gz

SHELL ["/bin/bash", "-c"]

# Check and update the environment and start the service
CMD source activate service && bash ./check_and_update_env.sh && bash ./start.sh

