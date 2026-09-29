from __future__ import annotations

import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.append(str(root))

from app.services.faiss_store import faiss_store


def main() -> None:
    print(f"FAISS index ready at {faiss_store.index_path} with {faiss_store.index.ntotal} vectors.")


if __name__ == "__main__":
    main()
