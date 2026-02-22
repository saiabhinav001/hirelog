#!/usr/bin/env bash
set -e

# Resolve paths — script may run from repo root or backend/
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Install CPU-only PyTorch FIRST (avoids ~700MB CUDA deps entirely)
pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Now install the rest — pip sees torch is already satisfied, skips it
pip install --no-cache-dir -r requirements.txt

# Pre-download the embedding model so startup is fast
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', backend='onnx')"

echo "Build complete"
