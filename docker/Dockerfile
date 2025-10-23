FROM python:3.12-slim-trixie AS ndif_base

ENV DEBIAN_FRONTEND=noninteractive

# Install base utilities in single layer with cleanup
RUN apt-get update -y \
    && apt-get install -y build-essential wget \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install miniconda and create environment in single layer
ENV CONDA_DIR=/opt/conda
ENV PATH=$CONDA_DIR/bin:$PATH

COPY src/services/base/environment.yml /tmp/environment.yml

RUN wget --quiet https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh \
    && /bin/bash /tmp/miniconda.sh -b -p /opt/conda \
    && rm /tmp/miniconda.sh \
    && conda config --set always_yes yes --set changeps1 no \
    && conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main \
    && conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r \
    && conda env create --name service -f /tmp/environment.yml \
    && conda clean -afy \
    && rm /tmp/environment.yml

ENV CONDA_DEFAULT_ENV=service
ENV PATH=$CONDA_DIR/envs/service/bin:$PATH

FROM ndif_base

ARG NAME
COPY src/services/${NAME}/environment.yml .
RUN conda env update --name service -f environment.yml \
    && conda clean -afy \
    && find /opt/conda -type d -name "__pycache__" -exec rm -rf {} + \
    && find /opt/conda -name "*.pyc" -delete \
    && find /opt/conda -name "*.pyo" -delete \
    && find /opt/conda -type d -name "tests" -exec rm -rf {} + \
    && find /opt/conda -type d -name "test" -exec rm -rf {} + \
    && find /opt/conda -name "*.a" -delete \
    && rm -rf /opt/conda/pkgs \
    && rm -rf /opt/conda/envs/service/conda-meta/history \
    && rm -rf /tmp/*

COPY ./src ./src
COPY src/services/${NAME}/start.sh ./start.sh

SHELL ["/bin/bash", "-c"]
CMD source activate service && bash ./start.sh
