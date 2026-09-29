from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from huggingface_hub import HfApi


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required(name: str, value: str) -> str:
    if not value:
        raise SystemExit(f"Missing required value: {name}")
    return value


def _space_url(owner: str, space_name: str) -> str:
    return f"https://{owner}-{space_name}.hf.space"


def _check_http(url: str, timeout: float = 10.0, bearer_token: str = "") -> tuple[bool, int | None]:
    try:
        headers = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        req = urllib.request.Request(url=url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return True, int(response.status)
    except urllib.error.HTTPError as exc:
        return False, int(exc.code)
    except Exception:
        return False, None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy backend to Hugging Face Docker Space")
    parser.add_argument("--owner", default=_env("HF_OWNER"), help="HF username or org")
    parser.add_argument("--space-name", default=_env("HF_SPACE_NAME"), help="HF Space name")
    parser.add_argument("--token", default=_env("HF_TOKEN"), help="HF token with write access")
    parser.add_argument("--private", action="store_true", default=_env("HF_PRIVATE", "false").lower() == "true")
    parser.add_argument("--backend-dir", default=str(Path(__file__).resolve().parents[1]), help="Backend directory")

    parser.add_argument("--firebase-project-id", default=_env("FIREBASE_PROJECT_ID"))
    parser.add_argument("--firebase-service-account-json", default=_env("FIREBASE_SERVICE_ACCOUNT_JSON"))
    parser.add_argument("--firebase-service-account-path", default=_env("FIREBASE_SERVICE_ACCOUNT_PATH"))
    parser.add_argument("--allowed-origins", default=_env("ALLOWED_ORIGINS"))

    parser.add_argument("--wait-seconds", type=int, default=900, help="Max wait for runtime readiness")
    parser.add_argument("--poll-interval", type=int, default=15, help="Polling interval for readiness")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    owner = _required("owner", args.owner)
    space_name = _required("space_name", args.space_name)
    token = _required("token", args.token)
    firebase_project_id = _required("firebase_project_id", args.firebase_project_id)

    firebase_service_account_json = (args.firebase_service_account_json or "").strip()
    firebase_service_account_path = (args.firebase_service_account_path or "").strip()
    if not firebase_service_account_json and firebase_service_account_path:
        service_account_path = Path(firebase_service_account_path).expanduser().resolve()
        if not service_account_path.exists():
            raise SystemExit(f"Firebase service account file not found: {service_account_path}")
        firebase_service_account_json = json.dumps(json.loads(service_account_path.read_text(encoding="utf-8")))

    firebase_service_account_json = _required(
        "firebase_service_account_json", firebase_service_account_json
    )
    allowed_origins = _required("allowed_origins", args.allowed_origins).rstrip("/")

    backend_dir = Path(args.backend_dir).resolve()
    if not backend_dir.exists():
        raise SystemExit(f"Backend directory not found: {backend_dir}")

    repo_id = f"{owner}/{space_name}"
    api = HfApi(token=token)

    print(f"[deploy] creating or reusing Space: {repo_id}")
    api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk="docker",
        private=bool(args.private),
        exist_ok=True,
    )

    print("[deploy] uploading backend files")
    api.upload_folder(
        repo_id=repo_id,
        repo_type="space",
        folder_path=str(backend_dir),
        allow_patterns=[
            "Dockerfile",
            ".dockerignore",
            "requirements.txt",
            "app/**",
            "data/**",
        ],
        ignore_patterns=[
            "**/__pycache__/**",
            "**/*.pyc",
            "**/.venv/**",
            "**/.pytest_cache/**",
            "**/.ruff_cache/**",
        ],
        commit_message="Deploy backend API",
    )

    print("[deploy] setting Space variables and secrets")
    api.add_space_variable(repo_id=repo_id, key="ENV", value="production")
    api.add_space_variable(repo_id=repo_id, key="FIREBASE_PROJECT_ID", value=firebase_project_id)
    api.add_space_variable(repo_id=repo_id, key="ALLOWED_ORIGINS", value=allowed_origins)
    api.add_space_variable(repo_id=repo_id, key="SEARCH_ENGINE", value="faiss")
    api.add_space_variable(repo_id=repo_id, key="SEARCH_ENABLE_WARMUP", value="true")
    api.add_space_variable(repo_id=repo_id, key="SEARCH_INDEX_WORKER_MODE", value="embedded")
    api.add_space_variable(repo_id=repo_id, key="SEARCH_INDEX_QUEUE_BACKEND", value="memory")

    api.add_space_secret(
        repo_id=repo_id,
        key="FIREBASE_SERVICE_ACCOUNT_JSON",
        value=firebase_service_account_json,
    )

    print("[deploy] requesting Space restart to apply latest revision")
    try:
        api.restart_space(repo_id)
    except Exception as exc:
        print(f"[deploy] warning: could not trigger restart via API ({exc}); continuing with health checks")

    space_url = _space_url(owner, space_name)
    health_url = f"{space_url}/health/live"

    print(f"[deploy] waiting for runtime up to {args.wait_seconds}s")
    deadline = time.time() + max(args.wait_seconds, 60)
    ready = False
    last_stage = "unknown"
    saw_non_running_stage = False

    while time.time() < deadline:
        try:
            runtime = api.get_space_runtime(repo_id)
            stage_value = getattr(runtime, "stage", None)
            last_stage = str(stage_value) if stage_value is not None else "unknown"
        except Exception:
            last_stage = "unknown"

        if last_stage.upper() != "RUNNING":
            saw_non_running_stage = True

        ok, status_code = _check_http(health_url, timeout=10.0, bearer_token=token)
        if ok and status_code and 200 <= status_code < 500 and last_stage.upper() == "RUNNING" and saw_non_running_stage:
            ready = True
            break

        print(f"[deploy] stage={last_stage} health_status={status_code}")
        time.sleep(max(args.poll_interval, 5))

    if not ready:
        raise SystemExit(
            f"Space deployment submitted but health check did not succeed within timeout. "
            f"Space: https://huggingface.co/spaces/{repo_id}"
        )

    print("[deploy] success")
    print(f"[deploy] Space: https://huggingface.co/spaces/{repo_id}")
    print(f"[deploy] API URL: {space_url}")
    print(f"[deploy] Health: {health_url}")


if __name__ == "__main__":
    main()
