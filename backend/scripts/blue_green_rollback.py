from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.request


def _fetch_json(url: str, timeout_seconds: float) -> dict:
    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=max(timeout_seconds, 1.0)) as response:
        return json.loads(response.read().decode("utf-8"))


def _run_shell_hook(template: str, *, blue_weight: int, green_weight: int, stage_label: str) -> None:
    command = template.format(
        blue_weight=blue_weight,
        green_weight=green_weight,
        stage=stage_label,
    )
    process = subprocess.run(
        command,
        shell=True,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(
            "DNS hook failed with code "
            f"{process.returncode}:\nSTDOUT:\n{process.stdout}\nSTDERR:\n{process.stderr}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rollback traffic to blue deployment")
    parser.add_argument("--public-url", default="", help="Optional public URL to validate after rollback")
    parser.add_argument(
        "--dns-hook",
        default="",
        help=(
            "Shell command template used to move traffic. "
            "Use placeholders {blue_weight}, {green_weight}, {stage}."
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=12.0)
    parser.add_argument(
        "--report-file",
        default="../docs/reports/rollback-report.json",
        help="Output path for rollback report",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip DNS hook execution")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.dry_run and not args.dns_hook.strip():
        raise SystemExit("--dns-hook is required unless --dry-run is set")

    report: dict = {
        "action": "rollback",
        "target": {
            "blue_weight": 100,
            "green_weight": 0,
        },
        "status": "running",
    }

    if args.dry_run:
        report["dns"] = {
            "mode": "dry_run",
            "blue_weight": 100,
            "green_weight": 0,
        }
    else:
        _run_shell_hook(
            args.dns_hook,
            blue_weight=100,
            green_weight=0,
            stage_label="rollback",
        )
        report["dns"] = {
            "mode": "executed",
            "blue_weight": 100,
            "green_weight": 0,
        }

    if args.public_url:
        deep_url = f"{args.public_url.rstrip('/')}/health/deep"
        try:
            report["public_health"] = _fetch_json(deep_url, args.timeout_seconds)
        except urllib.error.URLError as exc:
            report["public_health"] = {
                "status": "error",
                "detail": str(exc),
            }

    report["status"] = "completed"
    output_path = Path(args.report_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({"status": "completed", "report_file": str(output_path)}, indent=2))


if __name__ == "__main__":
    main()
