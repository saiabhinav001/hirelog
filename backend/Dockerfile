FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt

RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y \
      triton \
      nvidia-cublas-cu12 \
      nvidia-cuda-cupti-cu12 \
      nvidia-cuda-nvrtc-cu12 \
      nvidia-cuda-runtime-cu12 \
      nvidia-cudnn-cu12 \
      nvidia-cufft-cu12 \
      nvidia-curand-cu12 \
      nvidia-cusolver-cu12 \
      nvidia-cusparse-cu12 \
      nvidia-cusparselt-cu12 \
      nvidia-nccl-cu12 \
      nvidia-nvjitlink-cu12 \
      nvidia-nvtx-cu12 \
      nvidia-nvshmem-cu12 \
      nvidia-cufile-cu12 \
      || true

COPY app ./app
COPY data ./data

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD sh -c "curl -fsS http://127.0.0.1:${PORT:-7860}/health/ready >/dev/null || exit 1"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
