from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _fetch_json(url: str, timeout_seconds: float) -> dict:
    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=max(timeout_seconds, 1.0)) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_json_payload(raw: str) -> dict:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise ValueError("No JSON payload found in command output")
    return json.loads(raw[start : end + 1])


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


def _run_load_test(
    *,
    python_executable: str,
    load_test_script: Path,
    base_url: str,
    workers: int,
    duration_seconds: int,
    target_rps: float,
    search_ratio: float,
    search_mode: str,
    timeout_seconds: float,
) -> dict:
    cmd = [
        python_executable,
        str(load_test_script),
        "--base-url",
        base_url,
        "--workers",
        str(max(workers, 1)),
        "--duration-seconds",
        str(max(duration_seconds, 1)),
        "--target-rps",
        str(max(target_rps, 0.0)),
        "--search-ratio",
        str(max(0.0, min(search_ratio, 1.0))),
        "--search-mode",
        search_mode,
        "--timeout-seconds",
        str(max(timeout_seconds, 1.0)),
    ]
    process = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(
            "Load test failed with code "
            f"{process.returncode}:\nSTDOUT:\n{process.stdout}\nSTDERR:\n{process.stderr}"
        )
    return _extract_json_payload(process.stdout)


def _evaluate_load_report(
    report: dict,
    *,
    min_success_rate_percent: float,
    max_p95_ms: float,
    max_p99_ms: float,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    totals = report.get("totals") or {}
    latency = report.get("latency_ms") or {}

    success_rate = float(totals.get("success_rate_percent") or 0.0)
    p95 = float(latency.get("p95") or 0.0)
    p99 = float(latency.get("p99") or 0.0)

    if success_rate < min_success_rate_percent:
        reasons.append(
            f"success_rate_percent {success_rate:.2f} < required {min_success_rate_percent:.2f}"
        )
    if p95 > max_p95_ms:
        reasons.append(f"p95 {p95:.2f}ms > allowed {max_p95_ms:.2f}ms")
    if p99 > max_p99_ms:
        reasons.append(f"p99 {p99:.2f}ms > allowed {max_p99_ms:.2f}ms")

    return (len(reasons) == 0, reasons)


def _stage_weights(raw: str) -> list[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    filtered = sorted({value for value in values if 0 < value <= 100})
    if not filtered:
        raise ValueError("At least one valid stage weight is required")
    if filtered[-1] != 100:
        filtered.append(100)
    return filtered


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Blue/green cutover with staged traffic and SLO gating")
    parser.add_argument("--blue-url", required=True, help="Current stable deployment base URL")
    parser.add_argument("--green-url", required=True, help="Candidate deployment base URL")
    parser.add_argument(
        "--public-url",
        required=True,
        help="Public URL routed by DNS or load balancer for stage monitoring",
    )
    parser.add_argument(
        "--dns-hook",
        default="",
        help=(
            "Shell command template used to move traffic. "
            "Use placeholders {blue_weight}, {green_weight}, {stage}."
        ),
    )
    parser.add_argument("--stage-weights", default="10,25,50,100", help="Comma-separated green traffic percentages")
    parser.add_argument("--monitor-seconds", type=int, default=45, help="Wait time after each traffic shift")
    parser.add_argument("--python-executable", default=sys.executable, help="Python executable path")
    parser.add_argument(
        "--load-test-script",
        default=str(Path(__file__).resolve().parent / "load_test.py"),
        help="Path to load_test.py",
    )

    parser.add_argument("--pre-workers", type=int, default=6)
    parser.add_argument("--pre-duration-seconds", type=int, default=25)
    parser.add_argument("--pre-target-rps", type=float, default=1.8)

    parser.add_argument("--stage-workers", type=int, default=6)
    parser.add_argument("--stage-duration-seconds", type=int, default=25)
    parser.add_argument("--stage-target-rps", type=float, default=1.8)

    parser.add_argument("--search-ratio", type=float, default=0.9)
    parser.add_argument("--search-mode", default="auto", choices=["auto", "semantic", "keyword"])
    parser.add_argument("--timeout-seconds", type=float, default=12.0)

    parser.add_argument("--min-success-rate", type=float, default=99.0)
    parser.add_argument("--max-p95-ms", type=float, default=450.0)
    parser.add_argument("--max-p99-ms", type=float, default=900.0)

    parser.add_argument(
        "--report-file",
        default="../docs/reports/cutover-report.json",
        help="Output path for cutover report",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip DNS hook execution")
    return parser.parse_args()


def _validate_health(base_url: str, timeout_seconds: float, label: str) -> dict:
    deep_url = f"{base_url.rstrip('/')}/health/deep"
    try:
        payload = _fetch_json(deep_url, timeout_seconds)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{label} health request failed: {exc}") from exc

    status = str(payload.get("overall_status") or "").strip().lower()
    if status == "error":
        raise RuntimeError(f"{label} deep health returned error: {json.dumps(payload, indent=2)}")
    return payload


def main() -> None:
    args = parse_args()
    stages = _stage_weights(args.stage_weights)

    if not args.dry_run and not args.dns_hook.strip():
        raise SystemExit("--dns-hook is required unless --dry-run is set")

    load_test_script = Path(args.load_test_script).resolve()
    if not load_test_script.exists():
        raise SystemExit(f"Load test script not found: {load_test_script}")

    report: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "blue_url": args.blue_url,
            "green_url": args.green_url,
            "public_url": args.public_url,
            "stage_weights": stages,
            "dry_run": bool(args.dry_run),
            "monitor_seconds": args.monitor_seconds,
            "thresholds": {
                "min_success_rate": args.min_success_rate,
                "max_p95_ms": args.max_p95_ms,
                "max_p99_ms": args.max_p99_ms,
            },
        },
        "health": {},
        "pre_cutover_load_test": {},
        "stages": [],
        "status": "running",
    }

    report["health"]["blue"] = _validate_health(args.blue_url, args.timeout_seconds, "blue")
    report["health"]["green"] = _validate_health(args.green_url, args.timeout_seconds, "green")

    pre_report = _run_load_test(
        python_executable=args.python_executable,
        load_test_script=load_test_script,
        base_url=args.green_url,
        workers=args.pre_workers,
        duration_seconds=args.pre_duration_seconds,
        target_rps=args.pre_target_rps,
        search_ratio=args.search_ratio,
        search_mode=args.search_mode,
        timeout_seconds=args.timeout_seconds,
    )
    pre_ok, pre_reasons = _evaluate_load_report(
        pre_report,
        min_success_rate_percent=args.min_success_rate,
        max_p95_ms=args.max_p95_ms,
        max_p99_ms=args.max_p99_ms,
    )
    report["pre_cutover_load_test"] = {
        "report": pre_report,
        "pass": pre_ok,
        "reasons": pre_reasons,
    }
    if not pre_ok:
        report["status"] = "aborted_pre_cutover"
        output_path = Path(args.report_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        raise SystemExit("Pre-cutover load test failed: " + "; ".join(pre_reasons))

    previous_green = 0
    try:
        for stage in stages:
            blue_weight = 100 - stage
            stage_label = f"green_{stage}"

            if args.dry_run:
                print(
                    json.dumps(
                        {
                            "event": "dns_shift",
                            "mode": "dry_run",
                            "blue_weight": blue_weight,
                            "green_weight": stage,
                        }
                    )
                )
            else:
                _run_shell_hook(
                    args.dns_hook,
                    blue_weight=blue_weight,
                    green_weight=stage,
                    stage_label=stage_label,
                )

            if args.monitor_seconds > 0:
                time.sleep(args.monitor_seconds)

            stage_health = _validate_health(args.public_url, args.timeout_seconds, f"public@{stage}%")
            stage_report = _run_load_test(
                python_executable=args.python_executable,
                load_test_script=load_test_script,
                base_url=args.public_url,
                workers=args.stage_workers,
                duration_seconds=args.stage_duration_seconds,
                target_rps=args.stage_target_rps,
                search_ratio=args.search_ratio,
                search_mode=args.search_mode,
                timeout_seconds=args.timeout_seconds,
            )
            stage_ok, stage_reasons = _evaluate_load_report(
                stage_report,
                min_success_rate_percent=args.min_success_rate,
                max_p95_ms=args.max_p95_ms,
                max_p99_ms=args.max_p99_ms,
            )

            stage_entry = {
                "green_weight": stage,
                "blue_weight": blue_weight,
                "health": stage_health,
                "load_test": stage_report,
                "pass": stage_ok,
                "reasons": stage_reasons,
            }
            report["stages"].append(stage_entry)

            if not stage_ok:
                raise RuntimeError(
                    f"Stage {stage}% failed: {'; '.join(stage_reasons)}"
                )

            previous_green = stage

    except Exception as exc:
        report["status"] = "failed_rolled_back"
        report["failure"] = str(exc)
        rollback_blue = 100 - previous_green
        rollback_green = previous_green
        if previous_green > 0:
            rollback_blue = 100
            rollback_green = 0

        if args.dry_run:
            report["rollback"] = {
                "mode": "dry_run",
                "blue_weight": rollback_blue,
                "green_weight": rollback_green,
            }
        else:
            try:
                _run_shell_hook(
                    args.dns_hook,
                    blue_weight=rollback_blue,
                    green_weight=rollback_green,
                    stage_label="rollback",
                )
                report["rollback"] = {
                    "mode": "executed",
                    "blue_weight": rollback_blue,
                    "green_weight": rollback_green,
                }
            except Exception as rollback_exc:
                report["rollback"] = {
                    "mode": "failed",
                    "detail": str(rollback_exc),
                }

        output_path = Path(args.report_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        raise

    report["status"] = "completed"
    output_path = Path(args.report_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({"status": "completed", "report_file": str(output_path)}, indent=2))


if __name__ == "__main__":
    main()
