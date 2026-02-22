#!/usr/bin/env bash
set -e

# Resolve paths — script may run from repo root or backend/
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

pip install --no-cache-dir -r requirements.txt

# Remove CUDA/GPU packages to save ~300MB (CPU-only + ONNX is enough)
pip uninstall -y \
  triton \
  nvidia-cublas-cu12 nvidia-cuda-cupti-cu12 nvidia-cuda-nvrtc-cu12 \
  nvidia-cuda-runtime-cu12 nvidia-cudnn-cu12 nvidia-cufft-cu12 \
  nvidia-curand-cu12 nvidia-cusolver-cu12 nvidia-cusparse-cu12 \
  nvidia-cusparselt-cu12 nvidia-nccl-cu12 nvidia-nvjitlink-cu12 \
  nvidia-nvtx-cu12 nvidia-nvshmem-cu12 nvidia-cufile-cu12 \
  cuda-bindings cuda-pathfinder 2>/dev/null || true

# Pre-download the embedding model so startup is fast
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', backend='onnx')"

echo "Build complete"
