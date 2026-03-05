#!/usr/bin/env python3
"""Distributed Inference Platform — Stress Test.

Two independent test modes (run one or both via flags):

  --worker-flood   Inject synthetic frames directly into Redis at high rate to
                   saturate the worker(s) regardless of producer FPS.
                   This is the right way to stress workers when the real
                   producer is running in 'realtime' replay mode.

  --api-hammer     Drive all read-only API endpoints with a real thread pool
                   (concurrent requests, not serial).  Serial round-trips
                   grossly under-report API capacity.

Prometheus is polled at the start and end of each phase so you get
before/after deltas for every key metric.

Usage
-----
  # Both phases, moderate concurrency
  python scripts/stress.py

  # Only flood the workers for 60 s with 8 threads
  python scripts/stress.py --worker-flood --flood-duration 60 --flood-workers 8

  # Only hammer the API with 32 concurrent threads for 30 s
  python scripts/stress.py --api-hammer --hammer-duration 30 --hammer-concurrency 32

  # Both, custom settings, save to audits/
  python scripts/stress.py --flood-duration 30 --hammer-duration 20 \\
      --api-url http://localhost:8000 --prom-url http://localhost:9090
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import socket
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths / optional shared-lib import
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
AUDITS_DIR = REPO_ROOT / "audits"
AUDITS_DIR.mkdir(exist_ok=True)

# Attempt to import shared deps; fall back to inline stubs so the script
# runs without the venv when only doing API hammering.
try:
    sys.path.insert(0, str(REPO_ROOT / "libs" / "platform_shared" / "src"))
    from platform_shared.schemas import QueueJob
    _HAVE_SHARED = True
except ImportError:
    _HAVE_SHARED = False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: float) -> tuple[int, float, bytes]:
    """GET url, return (status, elapsed_ms, body). Never raises network errors."""
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url=url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return int(resp.getcode()), (time.perf_counter() - t0) * 1000.0, body
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp else b""
        return int(exc.code), (time.perf_counter() - t0) * 1000.0, body
    except KeyboardInterrupt:
        raise
    except Exception:
        return 0, (time.perf_counter() - t0) * 1000.0, b""


def _get_json(url: str, timeout: float) -> tuple[int, Any]:
    status, _, body = _http_get(url, timeout)
    try:
        return status, json.loads(body.decode("utf-8")) if body else None
    except Exception:
        return status, None


# ---------------------------------------------------------------------------
# Prometheus helpers
# ---------------------------------------------------------------------------

def prom_query(prom_url: str, promql: str, timeout: float) -> float | None:
    params = urllib.parse.urlencode({"query": promql})
    status, data = _get_json(f"{prom_url}/api/v1/query?{params}", timeout)
    if status != 200 or not data:
        return None
    try:
        results = data["data"]["result"]
        if not results:
            return None
        val = float(results[0]["value"][1])
        return None if math.isnan(val) else val
    except Exception:
        return None


def _snap_prom(prom_url: str, timeout: float) -> dict[str, Any]:
    """Capture a snapshot of all key metrics via PromQL instant queries."""
    ns = "dip"
    w = "1m"

    def q(expr: str) -> float | None:
        return prom_query(prom_url, expr, timeout)

    return {
        "ts": time.time(),
        "producer_published_rate":    q(f"rate({ns}_producer_frames_published_total[{w}])"),
        "producer_dropped_rate":      q(f"rate({ns}_producer_frames_dropped_total[{w}])"),
        "worker_popped_rate":         q(f"rate({ns}_worker_jobs_popped_total[{w}])"),
        "worker_processed_rate":      q(f"rate({ns}_worker_jobs_processed_total[{w}])"),
        "worker_failures_rate":       q(f"rate({ns}_worker_failures_total[{w}])"),
        "worker_queue_depth":         q(f"{ns}_worker_queue_depth"),
        "worker_inference_p50_ms":    q(f"histogram_quantile(0.50, rate({ns}_worker_inference_duration_ms_bucket[{w}]))"),
        "worker_inference_p95_ms":    q(f"histogram_quantile(0.95, rate({ns}_worker_inference_duration_ms_bucket[{w}]))"),
        "worker_pipeline_p95_ms":     q(f"histogram_quantile(0.95, rate({ns}_worker_pipeline_duration_ms_bucket[{w}]))"),
        "api_request_rate":           q(f"rate({ns}_api_requests_total[{w}])"),
        "api_duration_p50_ms":        q(f"histogram_quantile(0.50, rate({ns}_api_request_duration_ms_bucket[{w}]))"),
        "api_duration_p95_ms":        q(f"histogram_quantile(0.95, rate({ns}_api_request_duration_ms_bucket[{w}]))"),
    }


def _fmt(val: float | None, unit: str = "", decimals: int = 2) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{decimals}f}{unit}"


def _delta(after: float | None, before: float | None) -> str:
    if after is None or before is None:
        return "n/a"
    d = after - before
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.2f}"


# ---------------------------------------------------------------------------
# Synthetic frame generation
# ---------------------------------------------------------------------------

# A minimal 1×1 white JPEG (~360 bytes) — valid enough for cv2.imdecode in worker.
_TINY_JPEG_1x1 = bytes([
    0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
    0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
    0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
    0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
    0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
    0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
    0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
    0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
    0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
    0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
    0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
    0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
    0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
    0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
    0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
    0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4,
    0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7,
    0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA,
    0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2, 0xE3,
    0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5,
    0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00,
    0x00, 0x3F, 0x00, 0xFB, 0xD2, 0x8A, 0x28, 0x03, 0xFF, 0xD9,
])


def _make_jpeg_frame(width: int = 64, height: int = 64) -> bytes:
    """Return a minimal solid-grey JPEG. Falls back to tiny stub if cv2 absent."""
    try:
        import cv2
        import numpy as np
        img = np.full((height, width, 3), 128, dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 50])
        if ok:
            return bytes(buf)
    except ImportError:
        pass
    return _TINY_JPEG_1x1


# ---------------------------------------------------------------------------
# Worker flood
# ---------------------------------------------------------------------------

class FloodStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enqueued: int = 0
        self.redis_errors: int = 0
        self.start_s: float = time.monotonic()

    def record_enqueued(self, n: int = 1) -> None:
        with self._lock:
            self.enqueued += n

    def record_error(self) -> None:
        with self._lock:
            self.redis_errors += 1

    def elapsed_s(self) -> float:
        return time.monotonic() - self.start_s

    def fps(self) -> float:
        e = self.elapsed_s()
        return self.enqueued / e if e > 0 else 0.0


def _flood_worker(
    redis_host: str,
    redis_port: int,
    redis_password: str | None,
    queue_name: str,
    frame_key_prefix: str,
    frame_ttl_s: int,
    source_id: str,
    frame_bytes: bytes,
    stop_event: threading.Event,
    stats: FloodStats,
    target_fps: float | None,
) -> None:
    """One flood thread: push jobs to Redis as fast as possible (or at target_fps)."""
    try:
        import redis as redislib
    except ImportError:
        print("  [flood] redis-py not available — install it in the venv", file=sys.stderr)
        return

    try:
        r = redislib.Redis(
            host=redis_host, port=redis_port,
            password=redis_password or None,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        r.ping()
    except Exception as exc:
        print(f"  [flood] Redis connection failed: {exc}", file=sys.stderr)
        return

    frame_id = 0
    interval_s = (1.0 / target_fps) if target_fps and target_fps > 0 else 0.0

    while not stop_event.is_set():
        loop_start = time.perf_counter()
        now_us = int(time.time() * 1_000_000)
        job_id = str(uuid.uuid4())
        frame_key = f"{frame_key_prefix}:stress:{source_id}:{frame_id}"

        if _HAVE_SHARED:
            job = QueueJob(
                schema_version=1,
                job_id=job_id,
                frame_id=frame_id,
                source_id=source_id,
                capture_ts_us=now_us,
                enqueued_at_us=now_us,
                frame_key=frame_key,
            )
            job_bytes = job.to_json_bytes()
        else:
            job_bytes = json.dumps({
                "schema_version": 1,
                "job_id": job_id,
                "frame_id": frame_id,
                "source_id": source_id,
                "capture_ts_us": now_us,
                "enqueued_at_us": now_us,
                "frame_key": frame_key,
                "attempt": 0,
            }).encode()

        try:
            pipe = r.pipeline(transaction=False)
            pipe.setex(frame_key, frame_ttl_s, frame_bytes)
            pipe.lpush(queue_name, job_bytes)
            pipe.execute()
            stats.record_enqueued()
        except Exception:
            stats.record_error()

        frame_id += 1

        if interval_s > 0:
            elapsed = time.perf_counter() - loop_start
            sleep = interval_s - elapsed
            if sleep > 0:
                time.sleep(sleep)


def run_worker_flood(
    redis_host: str,
    redis_port: int,
    redis_password: str | None,
    queue_name: str,
    frame_key_prefix: str,
    frame_ttl_s: int,
    prom_url: str,
    duration_s: float,
    num_workers: int,
    target_fps: float | None,
    timeout: float,
) -> dict[str, Any]:
    print(f"\n  Generating synthetic JPEG frame…", end=" ", flush=True)
    frame_bytes = _make_jpeg_frame()
    print(f"OK ({len(frame_bytes)} bytes)")

    # Use a unique source_id so stress results don't pollute production data.
    source_id = f"stress_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    print(f"  Source ID for this run: {source_id}")
    print(f"  Workers: {num_workers}  Duration: {duration_s}s  ", end="")
    if target_fps:
        print(f"Target: {target_fps:.0f} fps per thread ({target_fps * num_workers:.0f} fps total)")
    else:
        print("Target: max rate")

    snap_before = _snap_prom(prom_url, timeout)
    print(f"  Queue depth at start: {_fmt(snap_before.get('worker_queue_depth'), ' jobs', 0)}")

    stats = FloodStats()
    stop_event = threading.Event()

    threads = []
    for _ in range(num_workers):
        t = threading.Thread(
            target=_flood_worker,
            args=(
                redis_host, redis_port, redis_password,
                queue_name, frame_key_prefix, frame_ttl_s,
                source_id, frame_bytes, stop_event, stats, target_fps,
            ),
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Progress ticker
    interval_samples: list[dict[str, Any]] = []
    tick_every = max(5.0, duration_s / 12)
    last_tick = time.monotonic()
    run_start = time.monotonic()

    try:
        while time.monotonic() - run_start < duration_s:
            time.sleep(0.5)
            now = time.monotonic()
            if now - last_tick >= tick_every:
                depth = prom_query(prom_url, "dip_worker_queue_depth", timeout)
                processed_rate = prom_query(prom_url, f"rate(dip_worker_jobs_processed_total[30s])", timeout)
                sample = {
                    "elapsed_s": round(now - run_start, 1),
                    "enqueued_total": stats.enqueued,
                    "enqueue_fps": round(stats.fps(), 1),
                    "queue_depth": depth,
                    "worker_processed_rate_fps": processed_rate,
                    "redis_errors": stats.redis_errors,
                }
                interval_samples.append(sample)
                print(
                    f"  t={sample['elapsed_s']:5.1f}s  "
                    f"enqueued={sample['enqueued_total']:6d}  "
                    f"enqueue_fps={sample['enqueue_fps']:6.1f}  "
                    f"queue_depth={_fmt(depth, '', 0):>5}  "
                    f"worker_fps={_fmt(processed_rate, '', 1):>6}  "
                    f"redis_err={sample['redis_errors']}"
                )
                last_tick = now
    except KeyboardInterrupt:
        print("\n  (interrupted)")

    stop_event.set()
    for t in threads:
        t.join(timeout=3.0)

    elapsed = stats.elapsed_s()
    snap_after = _snap_prom(prom_url, timeout)

    result = {
        "source_id": source_id,
        "duration_actual_s": round(elapsed, 2),
        "num_threads": num_workers,
        "target_fps_per_thread": target_fps,
        "frame_bytes": len(frame_bytes),
        "enqueued_total": stats.enqueued,
        "enqueue_fps_avg": round(stats.fps(), 2),
        "redis_errors": stats.redis_errors,
        "interval_samples": interval_samples,
        "prom_before": snap_before,
        "prom_after": snap_after,
    }

    print()
    print(f"  ── Worker Flood Results ──────────────────────────────────────")
    print(f"  Frames enqueued total      : {stats.enqueued}")
    print(f"  Average enqueue rate       : {stats.fps():.1f} fps")
    print(f"  Redis errors               : {stats.redis_errors}")
    print()
    print(f"  {'Metric':<40} {'Before':>10} {'After':>10} {'Δ':>10}")
    print(f"  {'─'*72}")

    def _delta_row(label: str, key: str) -> None:
        b = snap_before.get(key)
        a = snap_after.get(key)
        print(f"  {label:<40} {_fmt(b):>10} {_fmt(a):>10} {_delta(a, b):>10}")

    _delta_row("Worker processed rate (fps)", "worker_processed_rate")
    _delta_row("Worker queue depth",          "worker_queue_depth")
    _delta_row("Worker inference p50 (ms)",   "worker_inference_p50_ms")
    _delta_row("Worker inference p95 (ms)",   "worker_inference_p95_ms")
    _delta_row("Worker pipeline p95 (ms)",    "worker_pipeline_p95_ms")
    _delta_row("Worker failures rate",        "worker_failures_rate")

    return result


# ---------------------------------------------------------------------------
# API hammer
# ---------------------------------------------------------------------------

class HammerStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.completed: int = 0
        self.errors: int = 0
        self.status_counts: dict[int, int] = defaultdict(int)
        self.latencies_ms: list[float] = []

    def record(self, status: int, ms: float) -> None:
        with self._lock:
            self.completed += 1
            if status == 0:
                self.errors += 1
            else:
                self.status_counts[status] += 1
                self.latencies_ms.append(ms)

    def rps(self, elapsed: float) -> float:
        return self.completed / elapsed if elapsed > 0 else 0.0


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (k - lo) * (s[hi] - s[lo])


def _hammer_thread(
    urls: list[str],
    timeout: float,
    stop_event: threading.Event,
    stats: HammerStats,
) -> None:
    idx = 0
    while not stop_event.is_set():
        url = urls[idx % len(urls)]
        idx += 1
        try:
            status, ms, _ = _http_get(url, timeout)
        except KeyboardInterrupt:
            break
        stats.record(status, ms)


def run_api_hammer(
    api_url: str,
    prom_url: str,
    duration_s: float,
    concurrency: int,
    timeout: float,
) -> dict[str, Any]:
    # Endpoints to hit (exclude /sources — known slow, tracked separately)
    endpoints = [
        "/",
        "/health/live",
        "/health/ready",
        "/results",
        "/results?limit=1",
    ]
    urls = [f"{api_url}{p}" for p in endpoints]
    print(f"  Concurrency: {concurrency}  Duration: {duration_s}s  Endpoints: {len(endpoints)}")
    print(f"  NOTE: /sources excluded (known slow — missing DB index).")

    snap_before = _snap_prom(prom_url, timeout)

    stats = HammerStats()
    stop_event = threading.Event()
    threads = []
    for _ in range(concurrency):
        t = threading.Thread(
            target=_hammer_thread,
            args=(urls, timeout, stop_event, stats),
            daemon=True,
        )
        t.start()
        threads.append(t)

    tick_every = max(5.0, duration_s / 10)
    last_tick = time.monotonic()
    run_start = time.monotonic()
    interval_samples: list[dict[str, Any]] = []

    try:
        while time.monotonic() - run_start < duration_s:
            time.sleep(0.5)
            now = time.monotonic()
            if now - last_tick >= tick_every:
                elapsed = now - run_start
                with stats._lock:
                    completed = stats.completed
                    errors = stats.errors
                    lats = list(stats.latencies_ms)
                sample = {
                    "elapsed_s": round(elapsed, 1),
                    "completed": completed,
                    "errors": errors,
                    "rps": round(completed / elapsed, 1) if elapsed > 0 else 0.0,
                    "p95_ms": round(percentile(lats, 95), 1) if lats else None,
                }
                interval_samples.append(sample)
                print(
                    f"  t={sample['elapsed_s']:5.1f}s  "
                    f"completed={sample['completed']:6d}  "
                    f"rps={sample['rps']:7.1f}  "
                    f"p95={_fmt(sample['p95_ms'], 'ms', 1):>10}  "
                    f"err={sample['errors']}"
                )
                last_tick = now
    except KeyboardInterrupt:
        print("\n  (interrupted)")

    stop_event.set()
    for t in threads:
        t.join(timeout=3.0)

    elapsed = time.monotonic() - run_start
    snap_after = _snap_prom(prom_url, timeout)

    with stats._lock:
        lats = list(stats.latencies_ms)
        completed = stats.completed
        errors = stats.errors
        status_counts = dict(stats.status_counts)

    latency_stats: dict[str, Any] | None = None
    if lats:
        latency_stats = {
            "min": min(lats),
            "mean": statistics.mean(lats),
            "median": statistics.median(lats),
            "p95": percentile(lats, 95),
            "p99": percentile(lats, 99),
            "max": max(lats),
            "stdev": statistics.stdev(lats) if len(lats) > 1 else 0.0,
        }

    result = {
        "duration_actual_s": round(elapsed, 2),
        "concurrency": concurrency,
        "endpoints": endpoints,
        "completed_requests": completed,
        "errors": errors,
        "rps_avg": round(completed / elapsed, 2) if elapsed > 0 else 0.0,
        "status_counts": status_counts,
        "latency_ms": latency_stats,
        "interval_samples": interval_samples,
        "prom_before": snap_before,
        "prom_after": snap_after,
    }

    print()
    print(f"  ── API Hammer Results ───────────────────────────────────────")
    print(f"  Completed requests         : {completed}")
    print(f"  Average RPS                : {result['rps_avg']:.1f}")
    print(f"  Errors                     : {errors}")
    print(f"  Status counts              : {status_counts}")
    if latency_stats:
        print(f"  Latency min                : {latency_stats['min']:.1f} ms")
        print(f"  Latency mean               : {latency_stats['mean']:.1f} ms")
        print(f"  Latency p95                : {latency_stats['p95']:.1f} ms")
        print(f"  Latency p99                : {latency_stats['p99']:.1f} ms")
        print(f"  Latency max                : {latency_stats['max']:.1f} ms")
    print()
    print(f"  {'Metric':<40} {'Before':>10} {'After':>10} {'Δ':>10}")
    print(f"  {'─'*72}")

    def _delta_row(label: str, key: str) -> None:
        b = snap_before.get(key)
        a = snap_after.get(key)
        print(f"  {label:<40} {_fmt(b):>10} {_fmt(a):>10} {_delta(a, b):>10}")

    _delta_row("API request rate (rps)",        "api_request_rate")
    _delta_row("API request duration p50 (ms)", "api_duration_p50_ms")
    _delta_row("API request duration p95 (ms)", "api_duration_p95_ms")

    return result


# ---------------------------------------------------------------------------
# Section formatting
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    print()
    print("─" * 72)
    print(f"  {title}")
    print("─" * 72)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Stress test the DIP platform.")
    parser.add_argument("--api-url",     default="http://localhost:8000")
    parser.add_argument("--prom-url",    default="http://localhost:9090")
    parser.add_argument("--redis-host",  default="localhost")
    parser.add_argument("--redis-port",  type=int, default=6380,
                        help="Mapped host port for Redis (default 6380 per docker-compose.yml)")
    parser.add_argument("--redis-password", default=None)
    parser.add_argument("--queue-name",  default="video_stream")
    parser.add_argument("--frame-key-prefix", default="frame")
    parser.add_argument("--frame-ttl",   type=int, default=15)
    parser.add_argument("--timeout",     type=float, default=5.0)

    # Phase toggles — if neither is given, run both
    parser.add_argument("--worker-flood", action="store_true", help="Run the Redis worker flood phase")
    parser.add_argument("--api-hammer",   action="store_true", help="Run the API concurrency hammer phase")

    # Flood options
    parser.add_argument("--flood-duration",  type=float, default=45.0)
    parser.add_argument("--flood-workers",   type=int,   default=4,
                        help="Number of concurrent Redis-push threads")
    parser.add_argument("--flood-fps",       type=float, default=None,
                        help="Target fps per flood thread (omit for max rate)")

    # Hammer options
    parser.add_argument("--hammer-duration",    type=float, default=30.0)
    parser.add_argument("--hammer-concurrency", type=int,   default=20,
                        help="Number of concurrent HTTP threads")

    args = parser.parse_args()

    run_flood  = args.worker_flood or not (args.worker_flood or args.api_hammer)
    run_hammer = args.api_hammer   or not (args.worker_flood or args.api_hammer)

    ts = datetime.now(tz=timezone.utc)
    ts_str = ts.strftime("%Y%m%d_%H%M%S")
    ts_iso = ts.isoformat()

    snapshot: dict[str, Any] = {
        "captured_at_utc": ts_iso,
        "config": vars(args),
    }

    print(f"\n{'='*72}")
    print(f"  Distributed Inference Platform — Stress Test")
    print(f"  {ts_iso}")
    print(f"{'='*72}")

    if run_flood:
        _section("Phase 1 / 2  Worker Flood  (synthetic Redis injection)")
        flood_result = run_worker_flood(
            redis_host=args.redis_host,
            redis_port=args.redis_port,
            redis_password=args.redis_password,
            queue_name=args.queue_name,
            frame_key_prefix=args.frame_key_prefix,
            frame_ttl_s=args.frame_ttl,
            prom_url=args.prom_url,
            duration_s=args.flood_duration,
            num_workers=args.flood_workers,
            target_fps=args.flood_fps,
            timeout=args.timeout,
        )
        snapshot["worker_flood"] = flood_result

    if run_hammer:
        _section("Phase 2 / 2  API Hammer  (concurrent HTTP requests)")
        hammer_result = run_api_hammer(
            api_url=args.api_url,
            prom_url=args.prom_url,
            duration_s=args.hammer_duration,
            concurrency=args.hammer_concurrency,
            timeout=args.timeout,
        )
        snapshot["api_hammer"] = hammer_result

    out_path = AUDITS_DIR / f"stress_{ts_str}.json"
    out_path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")

    print()
    print(f"{'='*72}")
    print(f"  Results saved → {out_path.relative_to(REPO_ROOT)}")
    print(f"  Compare with baseline:  audits/baseline_*.json")
    print(f"{'='*72}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
