"""Writes a throwaway service-account file (a fresh RSA key for a demo project) so the exam API can
run against the Firebase emulators. It can sign custom tokens for the Auth emulator and is worthless
against any real project.

    python e2e-local/make_service_account.py <output.json> [project-id]
"""

import json
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

output = sys.argv[1]
project = sys.argv[2] if len(sys.argv) > 2 else "demo-hirelog-exams"

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
with open(output, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "type": "service_account",
            "project_id": project,
            "private_key_id": "e2e",
            "private_key": pem,
            "client_email": f"e2e@{project}.iam.gserviceaccount.com",
            "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        handle,
    )
