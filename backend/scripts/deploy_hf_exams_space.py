"""Deploy the HireLog Exams API to a Hugging Face *Docker Space*.

This is deliberately separate from ``deploy_hf_space.py`` (the legacy search API):
it builds the slim exam image (``Dockerfile.exams`` + ``requirements-exams.lock``),
it only ever targets a Space whose name contains ``staging``, and it refuses to
overwrite an existing Space that this script did not create (it checks for the
``hirelog-exams-staging`` tag in the Space README).

Secrets come from the environment and are never printed. ``--dry-run`` builds the
upload bundle and prints the plan without any network access.

    HF_TOKEN=... FIREBASE_SERVICE_ACCOUNT_PATH=... EXAM_OTP_HMAC_SECRET=... \
      python scripts/deploy_hf_exams_space.py --owner <hf-user> --visibility public \
        --firebase-project-id <project> --allowed-origins https://<staging>.vercel.app
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional

DEFAULT_SPACE_NAME = "hirelog-exams-staging"
SPACE_MARKER_TAG = "hirelog-exams-staging"
APP_PORT = 7862
SPACE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*staging[a-z0-9-]*$")

README_TEMPLATE = """---
title: HireLog Exams Staging
sdk: docker
app_port: {port}
pinned: false
short_description: Staging API for HireLog Exams (not production)
tags:
  - {marker}
---

# HireLog Exams - staging API

FastAPI service (`app.exam_main:app`) for the HireLog Exams staging environment.
It is not the production HireLog API. Health: `/health/live`, `/health/ready`.
"""

_BUNDLE_IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}


def check_space_name(name: str) -> None:
    """Only ever deploy to a Space that is plainly a staging Space."""
    if not SPACE_NAME_PATTERN.fullmatch(name):
        raise SystemExit(
            f"Refusing to deploy to Space name {name!r}: the name must be lower-case and contain 'staging' "
            "so this script can never overwrite a production Space."
        )


def assemble_bundle(backend_dir: Path, out_dir: Path) -> list[str]:
    """Copy exactly what the exam image needs into ``out_dir`` (a Space repo layout)."""
    app_dir = backend_dir / "app"
    required = {
        "Dockerfile": backend_dir / "Dockerfile.exams",
        "requirements-exams.lock": backend_dir / "requirements-exams.lock",
    }
    for source in (*required.values(), app_dir):
        if not source.exists():
            raise SystemExit(f"Missing file needed for the Space bundle: {source}")

    out_dir.mkdir(parents=True, exist_ok=True)
    for target, source in required.items():
        shutil.copyfile(source, out_dir / target)
    (out_dir / "README.md").write_text(
        README_TEMPLATE.format(port=APP_PORT, marker=SPACE_MARKER_TAG), encoding="utf-8", newline="\n"
    )

    for path in sorted(app_dir.rglob("*")):
        relative = path.relative_to(backend_dir)
        if path.is_dir() or _BUNDLE_IGNORED_DIRS.intersection(relative.parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        destination = out_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)

    return sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())


def space_state(api, repo_id: str, not_found: tuple[type[BaseException], ...]) -> str:
    """'new' when the Space does not exist, 'ours' when it carries our marker tag.
    Anything else is somebody else's Space: refuse to touch it."""
    try:
        info = api.space_info(repo_id)
    except not_found:
        return "new"
    if SPACE_MARKER_TAG not in set(getattr(info, "tags", None) or []):
        raise SystemExit(
            f"Space {repo_id} already exists but was not created by this script (missing the "
            f"'{SPACE_MARKER_TAG}' tag). Refusing to overwrite it."
        )
    return "ours"


def build_settings(args: argparse.Namespace, service_account_json: str) -> tuple[dict[str, str], dict[str, str]]:
    """(variables, secrets). Variables are visible on a public Space; anything
    sensitive is a secret."""
    variables = {
        "EXAMS_ENABLED": "true",
        "ENV": "staging",
        "FIREBASE_PROJECT_ID": args.firebase_project_id,
        "ALLOWED_ORIGINS": args.allowed_origins,
        "EXAM_STORAGE_BOUNDARY": "true",
    }
    optional_variables = {
        "EXAM_COLLECTION_PREFIX": args.collection_prefix,
        "EXAM_OTP_SMTP_HOST": args.smtp_host,
        "EXAM_OTP_SMTP_PORT": args.smtp_port,
        "EXAM_OTP_FROM_ADDRESS": args.smtp_from,
        "EXAM_OTP_SMTP_USE_SSL": "true" if args.smtp_use_ssl else "",
    }
    variables.update({key: value for key, value in optional_variables.items() if value})

    secrets = {"FIREBASE_SERVICE_ACCOUNT_JSON": service_account_json}
    optional_secrets = {
        "EXAM_OTP_HMAC_SECRET": args.otp_hmac_secret,
        "EXAM_FACULTY_EMAILS": args.faculty_emails,
        "EXAM_OTP_SMTP_USERNAME": args.smtp_username,
        "EXAM_OTP_SMTP_PASSWORD": args.smtp_password,
    }
    secrets.update({key: value for key, value in optional_secrets.items() if value})
    return variables, secrets


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy the exam API to a Hugging Face staging Docker Space")
    parser.add_argument("--owner", default=_env("HF_OWNER"), help="HF username or organisation")
    parser.add_argument("--space-name", default=_env("HF_SPACE_NAME", DEFAULT_SPACE_NAME))
    parser.add_argument("--token", default=_env("HF_TOKEN"), help="HF token with write access (prefer the env var)")
    parser.add_argument(
        "--visibility",
        choices=("private", "public"),
        help="Required when creating a Space. A browser cannot call a private Space, so the staging web app "
        "needs 'public'; the API still requires a Firebase sign-in for every protected route.",
    )
    parser.add_argument("--backend-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--firebase-project-id", default=_env("FIREBASE_PROJECT_ID"))
    parser.add_argument("--firebase-service-account-json", default=_env("FIREBASE_SERVICE_ACCOUNT_JSON"))
    parser.add_argument("--firebase-service-account-path", default=_env("FIREBASE_SERVICE_ACCOUNT_PATH"))
    parser.add_argument("--allowed-origins", default=_env("ALLOWED_ORIGINS"), help="Exact staging web origin(s), comma separated")
    parser.add_argument("--collection-prefix", default=_env("EXAM_COLLECTION_PREFIX"))
    parser.add_argument("--faculty-emails", default=_env("EXAM_FACULTY_EMAILS"), help="Platform operator emails (stored as a secret)")
    parser.add_argument("--otp-hmac-secret", default=_env("EXAM_OTP_HMAC_SECRET"))
    parser.add_argument("--smtp-host", default=_env("EXAM_OTP_SMTP_HOST"))
    parser.add_argument("--smtp-port", default=_env("EXAM_OTP_SMTP_PORT"))
    parser.add_argument("--smtp-username", default=_env("EXAM_OTP_SMTP_USERNAME"))
    parser.add_argument("--smtp-password", default=_env("EXAM_OTP_SMTP_PASSWORD"))
    parser.add_argument("--smtp-from", default=_env("EXAM_OTP_FROM_ADDRESS"))
    parser.add_argument("--smtp-use-ssl", action="store_true", default=_env("EXAM_OTP_SMTP_USE_SSL").lower() == "true")
    parser.add_argument("--wait-seconds", type=int, default=900)
    parser.add_argument("--poll-interval", type=int, default=15)
    parser.add_argument("--dry-run", action="store_true", help="Build the bundle and print the plan; no network access")
    return parser.parse_args(list(argv) if argv is not None else None)


def _load_service_account(args: argparse.Namespace) -> str:
    raw = (args.firebase_service_account_json or "").strip()
    path = (args.firebase_service_account_path or "").strip()
    if not raw and path:
        file_path = Path(path).expanduser().resolve()
        if not file_path.exists():
            raise SystemExit(f"Firebase service account file not found: {file_path}")
        raw = json.dumps(json.loads(file_path.read_text(encoding="utf-8")))
    if not raw:
        raise SystemExit("Missing required value: firebase service account (JSON or path)")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("The Firebase service account value is not valid JSON") from exc
    if parsed.get("project_id") and parsed["project_id"] != args.firebase_project_id:
        raise SystemExit(
            "The service account belongs to a different Firebase project than --firebase-project-id; refusing to continue."
        )
    return raw


def _commit_message(backend_dir: Path) -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=backend_dir, capture_output=True, text=True, timeout=10, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return f"Deploy exam API {sha}".strip()


def _probe(url: str, token: str, timeout: float = 10.0) -> Optional[int]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url=url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError):
        return None


def wait_until_live(
    api, repo_id: str, health_url: str, token: str, wait_seconds: int, poll_interval: int, sleep: Callable[[float], None] = time.sleep
) -> bool:
    deadline = time.time() + max(wait_seconds, 60)
    saw_build = False
    while time.time() < deadline:
        try:
            stage = str(getattr(api.get_space_runtime(repo_id), "stage", "unknown"))
        except Exception:  # transient API errors just mean "keep waiting"
            stage = "unknown"
        if stage.upper() != "RUNNING":
            saw_build = True
        status = _probe(health_url, token)
        print(f"[deploy] stage={stage} health_status={status}")
        if stage.upper() == "RUNNING" and status == 200 and saw_build:
            return True
        sleep(max(poll_interval, 5))
    return False


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    check_space_name(args.space_name)
    if not args.firebase_project_id:
        raise SystemExit("Missing required value: --firebase-project-id")
    if not args.allowed_origins or "*" in args.allowed_origins:
        raise SystemExit("--allowed-origins must list the exact staging web origin(s); '*' is not allowed")
    service_account_json = _load_service_account(args)
    variables, secrets = build_settings(args, service_account_json)

    backend_dir = Path(args.backend_dir).resolve()
    bundle_dir = Path(tempfile.mkdtemp(prefix="exams-space-"))
    try:
        files = assemble_bundle(backend_dir, bundle_dir)
        owner = args.owner or "<owner>"
        repo_id = f"{owner}/{args.space_name}"
        print(f"[deploy] target Space: {repo_id}")
        print(f"[deploy] bundle: {len(files)} files ({', '.join(f for f in files if '/' not in f)}, app/...)")
        print("[deploy] variables:", ", ".join(sorted(variables)))
        print("[deploy] secrets (values never printed):", ", ".join(sorted(secrets)))
        if args.dry_run:
            print("[deploy] dry run: nothing was uploaded")
            return 0

        if not args.owner or not args.token:
            raise SystemExit("--owner and a Hugging Face token (HF_TOKEN) are required to deploy")
        if not args.otp_hmac_secret:
            raise SystemExit("--otp-hmac-secret (EXAM_OTP_HMAC_SECRET) is required so OTP codes survive a restart")

        from huggingface_hub import HfApi  # imported lazily so the helpers above are testable without it
        from huggingface_hub.utils import RepositoryNotFoundError

        api = HfApi(token=args.token)
        state = space_state(api, repo_id, (RepositoryNotFoundError,))
        if state == "new":
            if not args.visibility:
                raise SystemExit("--visibility is required when the Space does not exist yet")
            print(f"[deploy] creating Space ({args.visibility})")
            api.create_repo(
                repo_id=repo_id, repo_type="space", space_sdk="docker", private=args.visibility == "private", exist_ok=False
            )
        else:
            print("[deploy] reusing the existing staging Space (visibility is left unchanged)")

        # Secrets and variables first, so the very first build already starts with them.
        for key, value in variables.items():
            api.add_space_variable(repo_id=repo_id, key=key, value=value)
        for key, value in secrets.items():
            api.add_space_secret(repo_id=repo_id, key=key, value=value)

        api.upload_folder(
            repo_id=repo_id, repo_type="space", folder_path=str(bundle_dir), commit_message=_commit_message(backend_dir)
        )
        try:
            api.restart_space(repo_id)
        except Exception as exc:  # the upload already triggers a rebuild
            print(f"[deploy] note: restart request failed ({type(exc).__name__}); waiting for the rebuild instead")

        space_url = f"https://{owner}-{args.space_name}.hf.space"
        if not wait_until_live(api, repo_id, f"{space_url}/health/live", args.token, args.wait_seconds, args.poll_interval):
            print(f"[deploy] the Space did not become live in time: https://huggingface.co/spaces/{repo_id}", file=sys.stderr)
            return 1
        print(f"[deploy] live: {space_url}  (readiness: {space_url}/health/ready)")
        return 0
    finally:
        shutil.rmtree(bundle_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
