from __future__ import annotations

import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.append(str(root))

from app.services.seed_data import ensure_seeded


def main() -> None:
    report = ensure_seeded()
    print(f"Seed completed: {report}")


if __name__ == "__main__":
    main()
