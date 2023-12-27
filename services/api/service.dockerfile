FROM api_base:latest

WORKDIR /wd

COPY ./src.tar.gz /wd/src.tar.gz

RUN tar -xvf src.tar.gz\
    && rm src.tar.gz

CMD conda run --no-capture-output -n service uvicorn src.app:app --host 0.0.0.0 --port 80 --workers $WORKERS

