FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  PIP_NO_CACHE_DIR=1 \
  VIRTUAL_ENV=/opt/venv \
  PATH=/opt/venv/bin:$PATH

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124
ARG TORCH_VERSION=

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    ffmpeg \
    libsndfile1 \
    libgl1 \
    libglib2.0-0 \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv "$VIRTUAL_ENV"

WORKDIR /app

COPY pyproject.toml README.md ./
COPY abogen ./abogen

RUN pip install --upgrade pip \
  && if [ -n "$TORCH_VERSION" ]; then \
       pip install torch=="$TORCH_VERSION" torchvision=="$TORCH_VERSION" torchaudio=="$TORCH_VERSION" --index-url "$TORCH_INDEX_URL"; \
     else \
       pip install torch torchvision torchaudio --index-url "$TORCH_INDEX_URL"; \
     fi \
  && pip install --no-cache-dir .

ENV ABOGEN_HOST=0.0.0.0 \
  ABOGEN_PORT=8808

EXPOSE 8808

VOLUME ["/data"]

ENV ABOGEN_UPLOAD_ROOT=/data/uploads \
  ABOGEN_OUTPUT_ROOT=/data/outputs

RUN mkdir -p /data/uploads /data/outputs

CMD ["abogen"]
