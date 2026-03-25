"""Executable benchmark profiles for the benchmark suite."""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import base64
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .schema import BenchmarkRun, Thresholds, utc_now_iso

FALLBACK_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+y3X8AAAAASUVORK5CYII="
)


def _make_frame_bytes() -> bytes:
    """Return a small valid image payload for worker-step injection."""
    try:
        import cv2
        import numpy as np

        image = np.full((32, 32, 3), 128, dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ok:
            return bytes(encoded)
    except Exception:
        pass
    return FALLBACK_PNG_1X1


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    values = sorted(data)
    k = (len(values) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (k - lo) * (values[hi] - values[lo])


def _http_get(url: str, timeout: float) -> tuple[int, float, bytes]:
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url=url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.getcode()), (time.perf_counter() - start) * 1000.0, resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp else b""
        return int(exc.code), (time.perf_counter() - start) * 1000.0, body
    except Exception:
        return 0, (time.perf_counter() - start) * 1000.0, b""


def _get_json(url: str, timeout: float) -> tuple[int, Any]:
    status, _, body = _http_get(url, timeout)
    if not body:
        return status, None
    try:
        return status, json.loads(body.decode("utf-8"))
    except Exception:
        return status, None


def _prom_query(prom_url: str, expr: str, timeout: float) -> float | None:
    params = urllib.parse.urlencode({"query": expr})
    status, data = _get_json(f"{prom_url}/api/v1/query?{params}", timeout)
    if status != 200 or not isinstance(data, dict):
        return None
    try:
        results = data["data"]["result"]
        if not results:
            return None
        value = float(results[0]["value"][1])
        return None if math.isnan(value) else value
    except Exception:
        return None


def collect_prometheus_snapshot(prom_url: str, timeout: float) -> dict[str, float | None]:
    ns = "dip"
    window = "1m"
    return {
        "worker_processed_rate": _prom_query(prom_url, f"rate({ns}_worker_jobs_processed_total[{window}])", timeout),
        "worker_failures_rate": _prom_query(prom_url, f"rate({ns}_worker_failures_total[{window}])", timeout),
        "worker_queue_depth": _prom_query(prom_url, f"{ns}_worker_queue_depth", timeout),
        "worker_inference_p95_ms": _prom_query(
            prom_url,
            f"histogram_quantile(0.95, rate({ns}_worker_inference_duration_ms_bucket[{window}]))",
            timeout,
        ),
        "worker_pipeline_p95_ms": _prom_query(
            prom_url,
            f"histogram_quantile(0.95, rate({ns}_worker_pipeline_duration_ms_bucket[{window}]))",
            timeout,
        ),
        "api_request_rate": _prom_query(prom_url, f"rate({ns}_api_requests_total[{window}])", timeout),
        "api_duration_p95_ms": _prom_query(
            prom_url,
            f"histogram_quantile(0.95, rate({ns}_api_request_duration_ms_bucket[{window}]))",
            timeout,
        ),
    }


def classify_run(
    *,
    thresholds: Thresholds,
    failure_rate: float | None = None,
    queue_depth_growth: float | None = None,
    latency_p95_ms: float | None = None,
    latency_p99_ms: float | None = None,
    invalid: bool = False,
) -> tuple[str, list[str]]:
    verdicts: list[str] = []
    if invalid:
        verdicts.append("run_invalid")
        return "invalid", verdicts

    classification = "stable"
    if failure_rate is not None and thresholds.max_failure_rate is not None:
        if failure_rate > thresholds.max_failure_rate:
            verdicts.append("failure_rate_exceeded")
            classification = "saturated"
    if queue_depth_growth is not None and thresholds.max_queue_depth_growth is not None:
        if queue_depth_growth > thresholds.max_queue_depth_growth:
            verdicts.append("queue_growth_exceeded")
            classification = "saturated" if classification == "stable" else classification
    if latency_p95_ms is not None and thresholds.max_latency_p95_ms is not None:
        if latency_p95_ms > thresholds.max_latency_p95_ms:
            verdicts.append("latency_p95_exceeded")
            classification = "degrading" if classification == "stable" else classification
    if latency_p99_ms is not None and thresholds.max_latency_p99_ms is not None:
        if latency_p99_ms > thresholds.max_latency_p99_ms:
            verdicts.append("latency_p99_exceeded")
            classification = "degrading" if classification == "stable" else classification
    if not verdicts:
        verdicts.append("within_thresholds")
    return classification, verdicts


def _api_load_test_endpoint(url: str, n: int, timeout: float) -> dict[str, Any]:
    latencies: list[float] = []
    status_counts: dict[int, int] = {}
    errors = 0
    for _ in range(n):
        status, elapsed_ms, _ = _http_get(url, timeout)
        if status == 0:
            errors += 1
            continue
        status_counts[status] = status_counts.get(status, 0) + 1
        latencies.append(elapsed_ms)
    return {
        "url": url,
        "requests": n,
        "errors": errors,
        "status_counts": status_counts,
        "latency_ms": None if not latencies else {
            "min": min(latencies),
            "mean": statistics.mean(latencies),
            "median": statistics.median(latencies),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": max(latencies),
            "stdev": statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
        },
    }


def run_baseline_profile(
    run: BenchmarkRun,
    *,
    api_url: str,
    prom_url: str,
    timeout: float,
    load_n: int,
    include_sources: bool,
) -> BenchmarkRun:
    endpoints = ["/", "/health/live", "/health/ready", "/results", "/results?limit=1"]
    if include_sources:
        endpoints.insert(3, "/sources")

    health_live_status, health_live = _get_json(f"{api_url}/health/live", timeout)
    health_ready_status, health_ready = _get_json(f"{api_url}/health/ready", timeout)
    prom = collect_prometheus_snapshot(prom_url, timeout)
    load_results = {path: _api_load_test_endpoint(f"{api_url}{path}", load_n, timeout) for path in endpoints}

    p95_values = [
        result["latency_ms"]["p95"]
        for result in load_results.values()
        if result["latency_ms"] is not None
    ]
    error_count = sum(result["errors"] for result in load_results.values())
    classification, verdicts = classify_run(
        thresholds=run.thresholds,
        failure_rate=float(error_count) / max(load_n * len(endpoints), 1),
        queue_depth_growth=0.0,
        latency_p95_ms=max(p95_values) if p95_values else None,
        invalid=health_ready_status != 200,
    )

    run.status = "completed"
    run.finished_at_utc = utc_now_iso()
    run.summary.duration_s = None
    run.summary.classification = classification
    run.summary.success = classification not in {"invalid", "saturated"}
    run.summary.headline = (
        f"Baseline completed across {len(endpoints)} endpoints with max p95 "
        f"{max(p95_values):.1f} ms" if p95_values else "Baseline completed"
    )
    run.summary.primary_metrics = {
        "api_baseline_max_p95_ms": max(p95_values) if p95_values else None,
        "api_baseline_error_count": error_count,
        "worker_processed_rate": prom.get("worker_processed_rate"),
        "worker_failure_rate": prom.get("worker_failures_rate"),
        "worker_queue_depth": prom.get("worker_queue_depth"),
    }
    run.verdicts = verdicts
    run.measurements = {
        "health": {
            "live": {"status": health_live_status, "body": health_live},
            "ready": {"status": health_ready_status, "body": health_ready},
        },
        "prometheus": prom,
        "api_serial_load": load_results,
    }
    if not include_sources:
        run.notes.append("The /sources endpoint was intentionally excluded from baseline load.")
    return run


class _HammerStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.completed = 0
        self.errors = 0
        self.latencies_ms: list[float] = []
        self.status_counts: dict[int, int] = defaultdict(int)

    def record(self, status: int, elapsed_ms: float) -> None:
        with self.lock:
            self.completed += 1
            if status == 0:
                self.errors += 1
            else:
                self.status_counts[status] += 1
                self.latencies_ms.append(elapsed_ms)


def _api_hammer_step(
    *,
    api_url: str,
    prom_url: str,
    concurrency: int,
    duration_s: float,
    timeout: float,
    endpoints: list[str],
    thresholds: Thresholds,
) -> dict[str, Any]:
    stop = threading.Event()
    stats = _HammerStats()
    urls = [f"{api_url}{path}" for path in endpoints]

    def worker() -> None:
        idx = 0
        while not stop.is_set():
            url = urls[idx % len(urls)]
            idx += 1
            status, elapsed_ms, _ = _http_get(url, timeout)
            stats.record(status, elapsed_ms)

    before = collect_prometheus_snapshot(prom_url, timeout)
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for thread in threads:
        thread.start()

    start = time.monotonic()
    interval_samples: list[dict[str, Any]] = []
    while time.monotonic() - start < duration_s:
        time.sleep(min(5.0, duration_s))
        elapsed = time.monotonic() - start
        with stats.lock:
            latencies = list(stats.latencies_ms)
            completed = stats.completed
            errors = stats.errors
        interval_samples.append({
            "elapsed_s": round(elapsed, 1),
            "completed": completed,
            "errors": errors,
            "rps": completed / elapsed if elapsed > 0 else 0.0,
            "p95_ms": percentile(latencies, 95) if latencies else None,
        })

    stop.set()
    for thread in threads:
        thread.join(timeout=2.0)
    elapsed = time.monotonic() - start
    after = collect_prometheus_snapshot(prom_url, timeout)

    with stats.lock:
        latencies = list(stats.latencies_ms)
        completed = stats.completed
        errors = stats.errors
        status_counts = dict(stats.status_counts)
    latency = None if not latencies else {
        "min": min(latencies),
        "mean": statistics.mean(latencies),
        "median": statistics.median(latencies),
        "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99),
        "max": max(latencies),
        "stdev": statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
    }
    classification, verdicts = classify_run(
        thresholds=thresholds,
        failure_rate=(errors / completed) if completed else 1.0,
        queue_depth_growth=(after.get("worker_queue_depth") or 0.0) - (before.get("worker_queue_depth") or 0.0),
        latency_p95_ms=None if latency is None else latency["p95"],
        latency_p99_ms=None if latency is None else latency["p99"],
        invalid=completed == 0,
    )
    return {
        "concurrency": concurrency,
        "duration_s": elapsed,
        "completed_requests": completed,
        "errors": errors,
        "status_counts": status_counts,
        "rps_avg": completed / elapsed if elapsed > 0 else 0.0,
        "latency_ms": latency,
        "prom_before": before,
        "prom_after": after,
        "queue_depth_growth": (after.get("worker_queue_depth") or 0.0) - (before.get("worker_queue_depth") or 0.0),
        "classification": classification,
        "verdicts": verdicts,
        "interval_samples": interval_samples,
    }


def run_api_step_profile(
    run: BenchmarkRun,
    *,
    api_url: str,
    prom_url: str,
    timeout: float,
    duration_s: float,
    endpoints: list[str],
    concurrencies: list[int],
    stop_on_saturation: bool,
) -> BenchmarkRun:
    steps: list[dict[str, Any]] = []
    stable_rps: float | None = None
    stable_concurrency: int | None = None

    for concurrency in concurrencies:
        step = _api_hammer_step(
            api_url=api_url,
            prom_url=prom_url,
            concurrency=concurrency,
            duration_s=duration_s,
            timeout=timeout,
            endpoints=endpoints,
            thresholds=run.thresholds,
        )
        steps.append(step)
        if step["classification"] == "stable":
            stable_rps = step["rps_avg"]
            stable_concurrency = concurrency
        if stop_on_saturation and step["classification"] in {"saturated", "invalid"}:
            break

    final_classification = "stable"
    for step in steps:
        if step["classification"] == "invalid":
            final_classification = "invalid"
            break
        if step["classification"] == "saturated":
            final_classification = "saturated"
        elif step["classification"] == "degrading" and final_classification == "stable":
            final_classification = "degrading"

    run.status = "completed"
    run.finished_at_utc = utc_now_iso()
    run.summary.duration_s = sum(step["duration_s"] for step in steps)
    run.summary.classification = final_classification
    run.summary.success = final_classification not in {"invalid", "saturated"}
    run.summary.headline = (
        f"API step benchmark completed across {len(steps)} step(s); "
        f"best stable concurrency={stable_concurrency}, best stable rps={stable_rps:.2f}"
        if stable_rps is not None else "API step benchmark completed with no stable step"
    )
    run.summary.primary_metrics = {
        "best_stable_concurrency": stable_concurrency,
        "best_stable_rps": stable_rps,
        "max_tested_concurrency": max(concurrencies) if concurrencies else None,
        "final_step_rps": steps[-1]["rps_avg"] if steps else None,
        "final_step_p95_ms": steps[-1]["latency_ms"]["p95"] if steps and steps[-1]["latency_ms"] else None,
    }
    run.verdicts = [f"step_{step['concurrency']}={step['classification']}" for step in steps]
    run.measurements = {
        "endpoints": endpoints,
        "steps": steps,
    }
    return run


class _FloodStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.enqueued = 0
        self.redis_errors = 0
        self.next_frame_id = 0

    def record_enqueued(self) -> None:
        with self.lock:
            self.enqueued += 1

    def record_error(self) -> None:
        with self.lock:
            self.redis_errors += 1

    def allocate_frame_id(self) -> int:
        with self.lock:
            frame_id = self.next_frame_id
            self.next_frame_id += 1
            return frame_id


def _load_redis_module():
    try:
        import redis as redislib
    except ImportError as exc:
        raise RuntimeError("The worker-step profile requires the 'redis' package.") from exc
    return redislib


def _flood_thread(
    *,
    redislib: Any,
    redis_host: str,
    redis_port: int,
    redis_password: str | None,
    queue_name: str,
    frame_key_prefix: str,
    frame_ttl_s: int,
    source_id: str,
    target_fps: float,
    stop: threading.Event,
    stats: _FloodStats,
    frame_bytes: bytes,
) -> None:
    client = redislib.Redis(
        host=redis_host,
        port=redis_port,
        password=redis_password or None,
        socket_timeout=2,
        socket_connect_timeout=2,
    )
    interval_s = 1.0 / target_fps if target_fps > 0 else 0.0
    while not stop.is_set():
        started = time.perf_counter()
        now_us = int(time.time() * 1_000_000)
        frame_id = stats.allocate_frame_id()
        job = {
            "schema_version": 1,
            "job_id": str(uuid.uuid4()),
            "frame_id": frame_id,
            "source_id": source_id,
            "capture_ts_us": now_us,
            "enqueued_at_us": now_us,
            "frame_key": f"{frame_key_prefix}:bench:{source_id}:{frame_id}",
            "attempt": 0,
        }
        try:
            pipe = client.pipeline(transaction=False)
            pipe.setex(job["frame_key"], frame_ttl_s, frame_bytes)
            pipe.lpush(queue_name, json.dumps(job).encode("utf-8"))
            pipe.execute()
            stats.record_enqueued()
        except Exception:
            stats.record_error()
        if interval_s > 0:
            sleep_s = interval_s - (time.perf_counter() - started)
            if sleep_s > 0:
                time.sleep(sleep_s)


def _worker_step(
    *,
    prom_url: str,
    timeout: float,
    redis_host: str,
    redis_port: int,
    redis_password: str | None,
    queue_name: str,
    frame_key_prefix: str,
    frame_ttl_s: int,
    num_workers: int,
    target_fps_per_thread: float,
    duration_s: float,
    thresholds: Thresholds,
) -> dict[str, Any]:
    try:
        redislib = _load_redis_module()
    except RuntimeError as exc:
        return {
            "source_id": None,
            "num_threads": num_workers,
            "target_fps_per_thread": target_fps_per_thread,
            "target_fps_total": target_fps_per_thread * num_workers,
            "duration_s": 0.0,
            "enqueued_total": 0,
            "enqueue_fps_avg": 0.0,
            "redis_errors": 0,
            "prom_before": collect_prometheus_snapshot(prom_url, timeout),
            "prom_after": collect_prometheus_snapshot(prom_url, timeout),
            "queue_depth_growth": None,
            "observed_failure_rate": None,
            "classification": "invalid",
            "verdicts": ["run_invalid", "redis_package_missing"],
            "interval_samples": [],
            "errors": [str(exc)],
        }
    before = collect_prometheus_snapshot(prom_url, timeout)
    frame_bytes = _make_frame_bytes()
    stats = _FloodStats()
    stop = threading.Event()
    source_id = f"bench_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    threads = [
        threading.Thread(
            target=_flood_thread,
            kwargs={
                "redis_host": redis_host,
                "redis_port": redis_port,
                "redis_password": redis_password,
                "redislib": redislib,
                "queue_name": queue_name,
                "frame_key_prefix": frame_key_prefix,
                "frame_ttl_s": frame_ttl_s,
                "source_id": source_id,
                "target_fps": target_fps_per_thread,
                "stop": stop,
                "stats": stats,
                "frame_bytes": frame_bytes,
            },
            daemon=True,
        )
        for _ in range(num_workers)
    ]
    for thread in threads:
        thread.start()

    start = time.monotonic()
    interval_samples: list[dict[str, Any]] = []
    while time.monotonic() - start < duration_s:
        time.sleep(min(5.0, duration_s))
        elapsed = time.monotonic() - start
        snap = collect_prometheus_snapshot(prom_url, timeout)
        with stats.lock:
            enqueued = stats.enqueued
            redis_errors = stats.redis_errors
        interval_samples.append({
            "elapsed_s": round(elapsed, 1),
            "enqueued_total": enqueued,
            "enqueue_fps": enqueued / elapsed if elapsed > 0 else 0.0,
            "worker_processed_rate": snap.get("worker_processed_rate"),
            "worker_failures_rate": snap.get("worker_failures_rate"),
            "worker_queue_depth": snap.get("worker_queue_depth"),
            "redis_errors": redis_errors,
        })

    stop.set()
    for thread in threads:
        thread.join(timeout=2.0)
    elapsed = time.monotonic() - start
    after = collect_prometheus_snapshot(prom_url, timeout)
    with stats.lock:
        enqueued = stats.enqueued
        redis_errors = stats.redis_errors

    queue_growth = (after.get("worker_queue_depth") or 0.0) - (before.get("worker_queue_depth") or 0.0)
    processed = after.get("worker_processed_rate")
    failures = after.get("worker_failures_rate")
    total_observed = (processed or 0.0) + (failures or 0.0)
    failure_rate = ((failures or 0.0) / total_observed) if total_observed > 0 else None
    classification, verdicts = classify_run(
        thresholds=thresholds,
        failure_rate=failure_rate,
        queue_depth_growth=queue_growth,
        latency_p95_ms=after.get("worker_pipeline_p95_ms"),
        invalid=enqueued == 0,
    )
    return {
        "source_id": source_id,
        "num_threads": num_workers,
        "target_fps_per_thread": target_fps_per_thread,
        "target_fps_total": target_fps_per_thread * num_workers,
        "duration_s": elapsed,
        "enqueued_total": enqueued,
        "enqueue_fps_avg": enqueued / elapsed if elapsed > 0 else 0.0,
        "redis_errors": redis_errors,
        "prom_before": before,
        "prom_after": after,
        "queue_depth_growth": queue_growth,
        "observed_failure_rate": failure_rate,
        "classification": classification,
        "verdicts": verdicts,
        "interval_samples": interval_samples,
    }


def run_worker_step_profile(
    run: BenchmarkRun,
    *,
    prom_url: str,
    timeout: float,
    redis_host: str,
    redis_port: int,
    redis_password: str | None,
    queue_name: str,
    frame_key_prefix: str,
    frame_ttl_s: int,
    num_workers: int,
    fps_steps: list[float],
    duration_s: float,
    stop_on_saturation: bool,
) -> BenchmarkRun:
    steps: list[dict[str, Any]] = []
    best_stable_total_fps: float | None = None
    first_saturated_total_fps: float | None = None

    for fps_step in fps_steps:
        step = _worker_step(
            prom_url=prom_url,
            timeout=timeout,
            redis_host=redis_host,
            redis_port=redis_port,
            redis_password=redis_password,
            queue_name=queue_name,
            frame_key_prefix=frame_key_prefix,
            frame_ttl_s=frame_ttl_s,
            num_workers=num_workers,
            target_fps_per_thread=fps_step,
            duration_s=duration_s,
            thresholds=run.thresholds,
        )
        steps.append(step)
        if step["classification"] == "stable":
            best_stable_total_fps = step["target_fps_total"]
        if first_saturated_total_fps is None and step["classification"] in {"saturated", "invalid"}:
            first_saturated_total_fps = step["target_fps_total"]
        if stop_on_saturation and step["classification"] in {"saturated", "invalid"}:
            break

    final_classification = "stable"
    for step in steps:
        if step["classification"] == "invalid":
            final_classification = "invalid"
            break
        if step["classification"] == "saturated":
            final_classification = "saturated"
        elif step["classification"] == "degrading" and final_classification == "stable":
            final_classification = "degrading"

    run.status = "completed"
    run.finished_at_utc = utc_now_iso()
    run.summary.duration_s = sum(step["duration_s"] for step in steps)
    run.summary.classification = final_classification
    run.summary.success = final_classification not in {"invalid", "saturated"}
    run.summary.headline = (
        f"Worker step benchmark completed across {len(steps)} step(s); "
        f"highest stable tested input fps={best_stable_total_fps:.2f}; "
        f"first saturated tested input fps={first_saturated_total_fps:.2f}"
        if best_stable_total_fps is not None and first_saturated_total_fps is not None
        else (
            f"Worker step benchmark completed across {len(steps)} step(s); "
            f"highest stable tested input fps={best_stable_total_fps:.2f}"
            if best_stable_total_fps is not None
            else "Worker step benchmark completed with no stable step"
        )
    )
    run.summary.primary_metrics = {
        "best_stable_total_input_fps": best_stable_total_fps,
        "highest_stable_tested_input_fps": best_stable_total_fps,
        "first_saturated_tested_input_fps": first_saturated_total_fps,
        "tested_threads": num_workers,
        "final_queue_growth": steps[-1]["queue_depth_growth"] if steps else None,
        "final_observed_failure_rate": steps[-1]["observed_failure_rate"] if steps else None,
        "final_worker_processed_rate": steps[-1]["prom_after"]["worker_processed_rate"] if steps else None,
    }
    run.verdicts = [f"step_{step['target_fps_total']:.2f}fps={step['classification']}" for step in steps]
    run.errors = [error for step in steps for error in step.get("errors", [])]
    run.measurements = {
        "steps": steps,
        "queue_name": queue_name,
        "frame_ttl_s": frame_ttl_s,
    }
    if redis_password:
        run.notes.append("Redis password was provided but is not stored in measurements.")
    return run


def compare_runs(old_doc: dict[str, Any], new_doc: dict[str, Any]) -> dict[str, Any]:
    """Compare two benchmark run documents."""
    old_summary = old_doc.get("summary", {})
    new_summary = new_doc.get("summary", {})
    old_metrics = old_summary.get("primary_metrics", {}) if isinstance(old_summary, dict) else {}
    new_metrics = new_summary.get("primary_metrics", {}) if isinstance(new_summary, dict) else {}
    shared_keys = sorted(set(old_metrics).intersection(new_metrics))
    metric_deltas: dict[str, Any] = {}
    for key in shared_keys:
        old_value = old_metrics.get(key)
        new_value = new_metrics.get(key)
        if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
            metric_deltas[key] = {
                "old": old_value,
                "new": new_value,
                "delta": new_value - old_value,
            }
        else:
            metric_deltas[key] = {
                "old": old_value,
                "new": new_value,
            }
    return {
        "old_run_id": old_doc.get("run_id"),
        "new_run_id": new_doc.get("run_id"),
        "old_profile": old_doc.get("profile"),
        "new_profile": new_doc.get("profile"),
        "old_classification": old_summary.get("classification") if isinstance(old_summary, dict) else None,
        "new_classification": new_summary.get("classification") if isinstance(new_summary, dict) else None,
        "metric_deltas": metric_deltas,
    }
