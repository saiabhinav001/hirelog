"""The Hugging Face deploy script guards a production Space and leaks no secrets."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _load_script():
    path = BACKEND_DIR / "scripts" / "deploy_hf_exams_space.py"
    spec = importlib.util.spec_from_file_location("deploy_hf_exams_space", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_hf_exams_space"] = module
    spec.loader.exec_module(module)
    return module


deploy = _load_script()

SERVICE_ACCOUNT = json.dumps({"project_id": "staging-project", "private_key": "TOP-SECRET-KEY"})


def _argv(**overrides: str) -> list[str]:
    values = {
        "--owner": "someone",
        "--firebase-project-id": "staging-project",
        "--firebase-service-account-json": SERVICE_ACCOUNT,
        "--allowed-origins": "https://exams-staging.example.app",
        "--otp-hmac-secret": "HMAC-SECRET-VALUE",
        "--faculty-emails": "operator@example.edu",
        "--backend-dir": str(BACKEND_DIR),
    }
    values.update(overrides)
    argv = [item for pair in values.items() for item in pair]
    return argv + ["--dry-run"]


@pytest.mark.parametrize("name", ["hirelog-exams-staging", "exams-staging-2", "x-staging"])
def test_staging_space_names_are_accepted(name: str) -> None:
    deploy.check_space_name(name)


@pytest.mark.parametrize("name", ["hirelog-exams", "hirelog", "Hirelog-Exams-Staging", "staging", "prod-exams"])
def test_anything_that_could_be_a_production_space_is_refused(name: str) -> None:
    with pytest.raises(SystemExit):
        deploy.check_space_name(name)


def test_bundle_for_the_real_backend_is_slim_and_complete(tmp_path: Path) -> None:
    files = deploy.assemble_bundle(BACKEND_DIR, tmp_path)
    assert {"Dockerfile", "README.md", "requirements-exams.lock", "app/exam_main.py"} <= set(files)
    assert "requirements.txt" not in files and not any(f.startswith(("data/", "tests/", "scripts/")) for f in files)
    assert not any("__pycache__" in f or f.endswith(".pyc") for f in files)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "sdk: docker" in readme and "app_port: 7862" in readme and deploy.SPACE_MARKER_TAG in readme
    assert "EXAM_STORAGE_BOUNDARY=true" in (tmp_path / "Dockerfile").read_text(encoding="utf-8")


def test_bundle_refuses_to_build_without_the_lockfile(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "Dockerfile.exams").write_text("FROM scratch\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        deploy.assemble_bundle(tmp_path, tmp_path / "out")


class _NotFound(Exception):
    pass


class _FakeApi:
    def __init__(self, tags=None, exists=True) -> None:
        self._tags, self._exists = tags, exists

    def space_info(self, repo_id):
        if not self._exists:
            raise _NotFound(repo_id)
        return SimpleNamespace(tags=self._tags)


def test_a_missing_space_is_new() -> None:
    assert deploy.space_state(_FakeApi(exists=False), "o/hirelog-exams-staging", (_NotFound,)) == "new"


def test_a_space_we_created_may_be_updated() -> None:
    api = _FakeApi(tags=["docker", deploy.SPACE_MARKER_TAG])
    assert deploy.space_state(api, "o/hirelog-exams-staging", (_NotFound,)) == "ours"


def test_a_space_we_did_not_create_is_never_overwritten() -> None:
    with pytest.raises(SystemExit, match="not created by this script"):
        deploy.space_state(_FakeApi(tags=["docker"]), "o/hirelog-exams-staging", (_NotFound,))
    with pytest.raises(SystemExit):
        deploy.space_state(_FakeApi(tags=None), "o/hirelog-exams-staging", (_NotFound,))


def test_sensitive_values_are_secrets_not_public_variables() -> None:
    args = deploy.parse_args(_argv(**{"--smtp-host": "smtp.example.edu", "--smtp-password": "SMTP-PASSWORD"}))
    variables, secrets = deploy.build_settings(args, SERVICE_ACCOUNT)
    assert variables["EXAM_STORAGE_BOUNDARY"] == "true" and variables["EXAMS_ENABLED"] == "true"
    assert variables["ENV"] == "staging"
    assert {"FIREBASE_SERVICE_ACCOUNT_JSON", "EXAM_OTP_HMAC_SECRET", "EXAM_FACULTY_EMAILS", "EXAM_OTP_SMTP_PASSWORD"} <= set(secrets)
    public = " ".join(variables.values())
    for secret_value in ("TOP-SECRET-KEY", "HMAC-SECRET-VALUE", "operator@example.edu", "SMTP-PASSWORD"):
        assert secret_value not in public


def test_dry_run_prints_the_plan_without_secret_values(capsys) -> None:
    assert deploy.main(_argv()) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "FIREBASE_SERVICE_ACCOUNT_JSON" in out
    for secret_value in ("TOP-SECRET-KEY", "HMAC-SECRET-VALUE", "operator@example.edu"):
        assert secret_value not in out


def test_deploy_refuses_a_service_account_from_another_project() -> None:
    with pytest.raises(SystemExit, match="different Firebase project"):
        deploy.main(_argv(**{"--firebase-project-id": "some-other-project"}))


def test_deploy_refuses_wildcard_cors_and_non_staging_names() -> None:
    with pytest.raises(SystemExit, match="exact staging web origin"):
        deploy.main(_argv(**{"--allowed-origins": "*"}))
    with pytest.raises(SystemExit, match="staging"):
        deploy.main(_argv(**{"--space-name": "hirelog-exams"}))
