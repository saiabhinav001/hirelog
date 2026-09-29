from __future__ import annotations

import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.append(str(root))

from firebase_admin import firestore

from app.core.firebase import db


def main() -> None:
    db.collection("metadata").document("bootstrap").set(
        {"bootstrapped_at": firestore.SERVER_TIMESTAMP},
        merge=True,
    )
    print("Firebase connection verified and bootstrap document created.")


if __name__ == "__main__":
    main()
