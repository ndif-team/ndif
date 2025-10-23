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

COPY src/services/api/environment.yml .
RUN conda env update --quiet --name service -f environment.yml \
    && conda clean -afy \
    && find /opt/conda -type d -name "__pycache__" -exec rm -rf {} + \
    && rm -rf /opt/conda/pkgs

COPY ./src ./src
COPY src/services/api/start.sh ./start.sh

SHELL ["/bin/bash", "-c"]
CMD source activate service && bash ./start.sh
