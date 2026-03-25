"""CLI for the new benchmark suite."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

from .compare import compare_run_files
from .preflight import (
    check_api_ready,
    check_prometheus_ready,
    check_redis_queue_state,
    summarize_checks,
)
from .profiles import (
    _get_json,
    _http_get,
    run_api_step_profile,
    run_baseline_profile,
    run_worker_step_profile,
)
from .schema import (
    BENCHMARK_RUN_SCHEMA_VERSION,
    BenchmarkRun,
    EnvironmentMetadata,
    GitMetadata,
    PROFILE_CHOICES,
    Thresholds,
    utc_now_iso,
    validate_run_document,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "audits" / "benchmarks"
JSON_SCHEMA_PATH = REPO_ROOT / "benchmarks" / "schemas" / "benchmark_run.schema.json"
SCHEMA_DOC_PATH = REPO_ROOT / "benchmarks" / "SCHEMA.md"


def _git_output(*args: str) -> str | None:
    """Return git command output, or None when unavailable."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None
    return proc.stdout.strip() or None


def collect_git_metadata() -> GitMetadata:
    """Capture basic git state for benchmark manifests."""
    commit = _git_output("rev-parse", "HEAD")
    branch = _git_output("rev-parse", "--abbrev-ref", "HEAD")
    dirty_output = _git_output("status", "--porcelain")
    return GitMetadata(
        commit=commit,
        branch=branch,
        dirty=bool(dirty_output) if dirty_output is not None else None,
    )


def collect_environment_metadata() -> EnvironmentMetadata:
    """Capture host/runtime metadata for benchmark manifests."""
    return EnvironmentMetadata(
        hostname=platform.node() or None,
        platform=platform.platform(),
        python_version=sys.version.split()[0],
        cwd=str(REPO_ROOT),
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the benchmark CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks",
        description="Benchmark suite CLI scaffold for the Distributed Inference Platform.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser(
        "plan",
        help="Create a benchmark run manifest that follows the canonical result schema.",
    )
    plan.add_argument("--profile", choices=PROFILE_CHOICES, required=True)
    plan.add_argument("--label", default=None, help="Optional short label for this benchmark run.")
    plan.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    plan.add_argument("--api-url", default="http://localhost:8000")
    plan.add_argument("--prom-url", default="http://localhost:9090")
    plan.add_argument("--redis-host", default="localhost")
    plan.add_argument("--redis-port", type=int, default=6380)
    plan.add_argument("--worker-metrics-url", default="http://localhost:9103/metrics")
    plan.add_argument("--max-failure-rate", type=float, default=0.01)
    plan.add_argument("--max-queue-depth-growth", type=float, default=0.0)
    plan.add_argument("--max-latency-p95-ms", type=float, default=None)
    plan.add_argument("--max-latency-p99-ms", type=float, default=None)
    plan.add_argument(
        "--note",
        action="append",
        default=[],
        help="Optional note to include in the planned run manifest. Can be repeated.",
    )

    validate = subparsers.add_parser(
        "validate",
        help="Validate a benchmark run JSON file against the canonical schema contract.",
    )
    validate.add_argument("path", help="Path to a benchmark run JSON file.")

    schema = subparsers.add_parser(
        "schema",
        help="Show benchmark schema documentation or print the JSON schema.",
    )
    schema.add_argument(
        "--format",
        choices=("summary", "json"),
        default="summary",
        help="Choose summary output or the full JSON schema document.",
    )

    baseline = subparsers.add_parser(
        "baseline",
        help="Run the low-load baseline profile and emit a canonical benchmark result.",
    )
    baseline.add_argument("--label", default=None)
    baseline.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    baseline.add_argument("--api-url", default="http://localhost:8000")
    baseline.add_argument("--prom-url", default="http://localhost:9090")
    baseline.add_argument("--timeout", type=float, default=5.0)
    baseline.add_argument("--load-n", type=int, default=50)
    baseline.add_argument("--include-sources", action="store_true")
    baseline.add_argument("--max-failure-rate", type=float, default=0.01)
    baseline.add_argument("--max-queue-depth-growth", type=float, default=0.0)
    baseline.add_argument("--max-latency-p95-ms", type=float, default=250.0)
    baseline.add_argument("--max-latency-p99-ms", type=float, default=500.0)
    baseline.add_argument("--worker-metrics-url", default="http://localhost:9103/metrics")

    worker_step = subparsers.add_parser(
        "worker-step",
        help="Run a paced worker capacity sweep and emit a canonical benchmark result.",
    )
    worker_step.add_argument("--label", default=None)
    worker_step.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    worker_step.add_argument("--prom-url", default="http://localhost:9090")
    worker_step.add_argument("--timeout", type=float, default=5.0)
    worker_step.add_argument("--redis-host", default="localhost")
    worker_step.add_argument("--redis-port", type=int, default=6380)
    worker_step.add_argument("--redis-password", default=None)
    worker_step.add_argument("--queue-name", default="video_stream")
    worker_step.add_argument("--frame-key-prefix", default="frame")
    worker_step.add_argument("--frame-ttl", type=int, default=15)
    worker_step.add_argument("--threads", type=int, default=2)
    worker_step.add_argument("--fps-steps", default="10,20,30,40,50")
    worker_step.add_argument("--step-duration", type=float, default=60.0)
    worker_step.add_argument("--stop-on-saturation", action="store_true")
    worker_step.add_argument("--max-failure-rate", type=float, default=0.01)
    worker_step.add_argument("--max-queue-depth-growth", type=float, default=0.0)
    worker_step.add_argument("--max-latency-p95-ms", type=float, default=75.0)
    worker_step.add_argument("--max-latency-p99-ms", type=float, default=150.0)

    api_step = subparsers.add_parser(
        "api-step",
        help="Run a paced API concurrency sweep and emit a canonical benchmark result.",
    )
    api_step.add_argument("--label", default=None)
    api_step.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    api_step.add_argument("--api-url", default="http://localhost:8000")
    api_step.add_argument("--prom-url", default="http://localhost:9090")
    api_step.add_argument("--timeout", type=float, default=5.0)
    api_step.add_argument("--step-duration", type=float, default=60.0)
    api_step.add_argument("--concurrency-steps", default="1,2,4,8,12,16")
    api_step.add_argument("--stop-on-saturation", action="store_true")
    api_step.add_argument("--include-sources", action="store_true")
    api_step.add_argument("--max-failure-rate", type=float, default=0.01)
    api_step.add_argument("--max-queue-depth-growth", type=float, default=0.0)
    api_step.add_argument("--max-latency-p95-ms", type=float, default=250.0)
    api_step.add_argument("--max-latency-p99-ms", type=float, default=500.0)

    compare = subparsers.add_parser(
        "compare",
        help="Compare two benchmark result files and print metric deltas.",
    )
    compare.add_argument("old_path")
    compare.add_argument("new_path")
    compare.add_argument("--format", choices=("text", "json"), default="text")

    preflight = subparsers.add_parser(
        "preflight",
        help="Run basic benchmark environment checks without executing a benchmark.",
    )
    preflight.add_argument("--profile", choices=("baseline", "api-step", "worker-step"), required=True)
    preflight.add_argument("--api-url", default="http://localhost:8000")
    preflight.add_argument("--prom-url", default="http://localhost:9090")
    preflight.add_argument("--timeout", type=float, default=5.0)
    preflight.add_argument("--redis-host", default="localhost")
    preflight.add_argument("--redis-port", type=int, default=6380)
    preflight.add_argument("--redis-password", default=None)
    preflight.add_argument("--queue-name", default="video_stream")

    return parser


def _make_thresholds(args: argparse.Namespace) -> Thresholds:
    return Thresholds(
        max_failure_rate=args.max_failure_rate,
        max_queue_depth_growth=args.max_queue_depth_growth,
        max_latency_p95_ms=args.max_latency_p95_ms,
        max_latency_p99_ms=args.max_latency_p99_ms,
    )


def _parse_float_list(raw: str) -> list[float]:
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def _parse_int_list(raw: str) -> list[int]:
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def _write_run(run: BenchmarkRun, output_dir: str | Path) -> Path:
    path = Path(output_dir) / f"{run.run_id}.json"
    run.write_json(path)
    return path


def command_plan(args: argparse.Namespace) -> int:
    """Create a benchmark manifest document for a future run."""
    output_dir = Path(args.output_dir)
    thresholds = Thresholds(
        max_failure_rate=args.max_failure_rate,
        max_queue_depth_growth=args.max_queue_depth_growth,
        max_latency_p95_ms=args.max_latency_p95_ms,
        max_latency_p99_ms=args.max_latency_p99_ms,
    )
    run = BenchmarkRun.planned(
        profile=args.profile,
        label=args.label,
        inputs={
            "api_url": args.api_url,
            "prom_url": args.prom_url,
            "redis_host": args.redis_host,
            "redis_port": args.redis_port,
            "worker_metrics_url": args.worker_metrics_url,
        },
        thresholds=thresholds,
        git=collect_git_metadata(),
        environment=collect_environment_metadata(),
        notes=list(args.note),
    )
    path = output_dir / f"{run.run_id}.json"
    run.write_json(path)

    print(f"Created benchmark manifest: {path}")
    print(f"schema_version={BENCHMARK_RUN_SCHEMA_VERSION}")
    print(f"profile={run.profile}")
    print("status=planned")
    print(f"schema_doc={SCHEMA_DOC_PATH}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    """Validate an existing benchmark JSON document."""
    path = Path(args.path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Benchmark file not found: {path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON in {path}: {exc}", file=sys.stderr)
        return 1

    errors = validate_run_document(doc)
    if errors:
        print(f"{path} is invalid.")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"{path} is valid.")
    return 0


def command_schema(args: argparse.Namespace) -> int:
    """Print schema information."""
    if args.format == "json":
        print(JSON_SCHEMA_PATH.read_text(encoding="utf-8"))
        return 0

    print("Benchmark run schema")
    print(f"- version: {BENCHMARK_RUN_SCHEMA_VERSION}")
    print(f"- json_schema: {JSON_SCHEMA_PATH}")
    print(f"- documentation: {SCHEMA_DOC_PATH}")
    print(f"- profiles: {', '.join(PROFILE_CHOICES)}")
    print("- commands:")
    print("  - python -m benchmarks plan --profile baseline")
    print("  - python -m benchmarks baseline --label current-baseline")
    print("  - python -m benchmarks worker-step --threads 2 --fps-steps 10,20,30")
    print("  - python -m benchmarks api-step --concurrency-steps 1,2,4,8")
    print("  - python -m benchmarks compare old.json new.json")
    print("  - python -m benchmarks validate audits/benchmarks/<run>.json")
    print("  - python -m benchmarks schema --format json")
    return 0


def command_baseline(args: argparse.Namespace) -> int:
    run = BenchmarkRun.planned(
        profile="baseline",
        label=args.label,
        inputs={
            "api_url": args.api_url,
            "prom_url": args.prom_url,
            "timeout_s": args.timeout,
            "load_n": args.load_n,
            "include_sources": args.include_sources,
            "worker_metrics_url": args.worker_metrics_url,
        },
        thresholds=_make_thresholds(args),
        git=collect_git_metadata(),
        environment=collect_environment_metadata(),
    )
    preflight_checks = [
        check_api_ready(api_url=args.api_url, timeout=args.timeout, get_json=_get_json),
        check_prometheus_ready(prom_url=args.prom_url, timeout=args.timeout, http_get=_http_get),
    ]
    preflight_ok, preflight_errors = summarize_checks(preflight_checks)
    run.measurements["preflight"] = preflight_checks
    if not preflight_ok:
        run.status = "failed"
        run.finished_at_utc = utc_now_iso()
        run.summary.classification = "invalid"
        run.summary.success = False
        run.summary.headline = "Baseline preflight failed"
        run.errors.extend(preflight_errors)
        path = _write_run(run, args.output_dir)
        print(f"Created baseline result: {path}")
        print("classification=invalid")
        print("headline=Baseline preflight failed")
        for error in preflight_errors:
            print(f"error={error}")
        return 1
    run = run_baseline_profile(
        run,
        api_url=args.api_url,
        prom_url=args.prom_url,
        timeout=args.timeout,
        load_n=args.load_n,
        include_sources=args.include_sources,
    )
    path = _write_run(run, args.output_dir)
    print(f"Created baseline result: {path}")
    print(f"classification={run.summary.classification}")
    print(f"headline={run.summary.headline}")
    return 0


def command_worker_step(args: argparse.Namespace) -> int:
    fps_steps = _parse_float_list(args.fps_steps)
    run = BenchmarkRun.planned(
        profile="worker-step",
        label=args.label,
        inputs={
            "prom_url": args.prom_url,
            "timeout_s": args.timeout,
            "redis_host": args.redis_host,
            "redis_port": args.redis_port,
            "queue_name": args.queue_name,
            "frame_key_prefix": args.frame_key_prefix,
            "frame_ttl": args.frame_ttl,
            "threads": args.threads,
            "fps_steps": fps_steps,
            "step_duration_s": args.step_duration,
            "stop_on_saturation": args.stop_on_saturation,
        },
        thresholds=_make_thresholds(args),
        git=collect_git_metadata(),
        environment=collect_environment_metadata(),
    )
    preflight_checks = [
        check_prometheus_ready(prom_url=args.prom_url, timeout=args.timeout, http_get=_http_get),
        check_redis_queue_state(
            redis_host=args.redis_host,
            redis_port=args.redis_port,
            redis_password=args.redis_password,
            queue_name=args.queue_name,
        ),
    ]
    preflight_ok, preflight_errors = summarize_checks(preflight_checks)
    run.measurements["preflight"] = preflight_checks
    if not preflight_ok:
        run.status = "failed"
        run.finished_at_utc = utc_now_iso()
        run.summary.classification = "invalid"
        run.summary.success = False
        run.summary.headline = "Worker-step preflight failed"
        run.errors.extend(preflight_errors)
        path = _write_run(run, args.output_dir)
        print(f"Created worker-step result: {path}")
        print("classification=invalid")
        print("headline=Worker-step preflight failed")
        for error in preflight_errors:
            print(f"error={error}")
        return 1
    run = run_worker_step_profile(
        run,
        prom_url=args.prom_url,
        timeout=args.timeout,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_password=args.redis_password,
        queue_name=args.queue_name,
        frame_key_prefix=args.frame_key_prefix,
        frame_ttl_s=args.frame_ttl,
        num_workers=args.threads,
        fps_steps=fps_steps,
        duration_s=args.step_duration,
        stop_on_saturation=args.stop_on_saturation,
    )
    path = _write_run(run, args.output_dir)
    print(f"Created worker-step result: {path}")
    print(f"classification={run.summary.classification}")
    print(f"headline={run.summary.headline}")
    return 0


def command_api_step(args: argparse.Namespace) -> int:
    concurrencies = _parse_int_list(args.concurrency_steps)
    endpoints = ["/", "/health/live", "/health/ready", "/results", "/results?limit=1"]
    if args.include_sources:
        endpoints.insert(3, "/sources")
    run = BenchmarkRun.planned(
        profile="api-step",
        label=args.label,
        inputs={
            "api_url": args.api_url,
            "prom_url": args.prom_url,
            "timeout_s": args.timeout,
            "step_duration_s": args.step_duration,
            "concurrency_steps": concurrencies,
            "endpoints": endpoints,
            "stop_on_saturation": args.stop_on_saturation,
        },
        thresholds=_make_thresholds(args),
        git=collect_git_metadata(),
        environment=collect_environment_metadata(),
    )
    preflight_checks = [
        check_api_ready(api_url=args.api_url, timeout=args.timeout, get_json=_get_json),
        check_prometheus_ready(prom_url=args.prom_url, timeout=args.timeout, http_get=_http_get),
    ]
    preflight_ok, preflight_errors = summarize_checks(preflight_checks)
    run.measurements["preflight"] = preflight_checks
    if not preflight_ok:
        run.status = "failed"
        run.finished_at_utc = utc_now_iso()
        run.summary.classification = "invalid"
        run.summary.success = False
        run.summary.headline = "API-step preflight failed"
        run.errors.extend(preflight_errors)
        path = _write_run(run, args.output_dir)
        print(f"Created api-step result: {path}")
        print("classification=invalid")
        print("headline=API-step preflight failed")
        for error in preflight_errors:
            print(f"error={error}")
        return 1
    run = run_api_step_profile(
        run,
        api_url=args.api_url,
        prom_url=args.prom_url,
        timeout=args.timeout,
        duration_s=args.step_duration,
        endpoints=endpoints,
        concurrencies=concurrencies,
        stop_on_saturation=args.stop_on_saturation,
    )
    path = _write_run(run, args.output_dir)
    print(f"Created api-step result: {path}")
    print(f"classification={run.summary.classification}")
    print(f"headline={run.summary.headline}")
    return 0


def command_compare(args: argparse.Namespace) -> int:
    comparison = compare_run_files(args.old_path, args.new_path)
    if args.format == "json":
        print(json.dumps(comparison, indent=2))
        return 0

    print("Benchmark comparison")
    print(f"- old_run_id: {comparison['old_run_id']}")
    print(f"- new_run_id: {comparison['new_run_id']}")
    print(f"- old_profile: {comparison['old_profile']}")
    print(f"- new_profile: {comparison['new_profile']}")
    print(f"- old_classification: {comparison['old_classification']}")
    print(f"- new_classification: {comparison['new_classification']}")
    if comparison["old_profile"] == "worker-step" and comparison["new_profile"] == "worker-step":
        print("- note: worker-step capacity is step-based. 'best_stable_total_input_fps' means")
        print("  the highest tested step that stayed within thresholds, not a precise capacity estimate.")
    print("- metric_deltas:")
    for key, value in comparison["metric_deltas"].items():
        if "delta" in value:
            print(f"  - {key}: old={value['old']} new={value['new']} delta={value['delta']}")
        else:
            print(f"  - {key}: old={value['old']} new={value['new']}")
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    checks: list[dict[str, object]] = []
    if args.profile in {"baseline", "api-step"}:
        checks.append(check_api_ready(api_url=args.api_url, timeout=args.timeout, get_json=_get_json))
    checks.append(check_prometheus_ready(prom_url=args.prom_url, timeout=args.timeout, http_get=_http_get))
    if args.profile == "worker-step":
        checks.append(
            check_redis_queue_state(
                redis_host=args.redis_host,
                redis_port=args.redis_port,
                redis_password=args.redis_password,
                queue_name=args.queue_name,
            )
        )
    ok, errors = summarize_checks(checks)
    print(f"profile={args.profile}")
    print(f"ok={str(ok).lower()}")
    for check in checks:
        print(f"- {check['name']}: ok={str(check['ok']).lower()} detail={json.dumps(check['detail'])}")
        if check.get("error"):
            print(f"  error={check['error']}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "plan":
        return command_plan(args)
    if args.command == "validate":
        return command_validate(args)
    if args.command == "schema":
        return command_schema(args)
    if args.command == "baseline":
        return command_baseline(args)
    if args.command == "worker-step":
        return command_worker_step(args)
    if args.command == "api-step":
        return command_api_step(args)
    if args.command == "compare":
        return command_compare(args)
    if args.command == "preflight":
        return command_preflight(args)

    parser.error(f"Unknown command: {args.command}")
    return 2
