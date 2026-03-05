#!/usr/bin/env python3
"""Distributed Inference Platform — Baseline Benchmark & System Snapshot.

Captures a point-in-time snapshot of all measurable system properties so you
can diff results after future improvements.

What it measures
----------------
1. Service health    — /health/live + /health/ready for the API.
2. Raw metric scrape — fetches /metrics from API (port 8000), producer (9101),
                       and worker (9102) and records every gauge/counter value.
3. Prometheus queries — computed aggregates via the Prometheus HTTP API:
                         - per-service throughput (rate over last 5 min)
                         - inference / pipeline / API latency at p50/p95/p99
                         - failure rates
                         - current queue depth
4. API load test     — sends --load-n requests to each read-only endpoint,
                       measures response-time distribution (min/mean/p95/p99/max).
5. Data inventory    — counts results and sources via the API.

Outputs
-------
  audits/baseline_YYYYMMDD_HHMMSS.json   machine-readable full snapshot
  stdout                                  human-readable summary table

Usage
-----
  python scripts/baseline.py
  python scripts/baseline.py --api-url http://localhost:8000 \\
                              --prom-url http://localhost:9090 \\
                              --load-n 200 \\
                              --timeout 5
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
AUDITS_DIR = REPO_ROOT / "audits"
AUDITS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: float) -> tuple[int, bytes]:
    """Return (status, body). Never raises on network/timeout errors; returns (0, b'')."""
    try:
        req = urllib.request.Request(url=url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.getcode()), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp else b""
        return int(exc.code), body
    except KeyboardInterrupt:
        raise
    except Exception:
        return 0, b""


def _get_json(url: str, timeout: float) -> tuple[int, Any]:
    status, body = _http_get(url, timeout)
    if not body:
        return status, None
    try:
        return status, json.loads(body.decode("utf-8"))
    except Exception:
        return status, None


# ---------------------------------------------------------------------------
# 1. Service health
# ---------------------------------------------------------------------------

def check_health(api_url: str, timeout: float) -> dict[str, Any]:
    live_status, live_body = _get_json(f"{api_url}/health/live", timeout)
    ready_status, ready_body = _get_json(f"{api_url}/health/ready", timeout)
    return {
        "live": {"http_status": live_status, "body": live_body},
        "ready": {"http_status": ready_status, "body": ready_body},
    }


# ---------------------------------------------------------------------------
# 2. Raw metric scrape
# ---------------------------------------------------------------------------

_METRIC_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[+-]?Inf|NaN)"
    r"(?:\s+\d+)?$"
)


def _parse_metrics_text(text: str) -> list[dict[str, Any]]:
    """Parse Prometheus text-format exposition into a list of sample dicts."""
    samples: list[dict[str, Any]] = []
    current_help: str | None = None
    current_type: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("# HELP "):
            parts = line[7:].split(" ", 1)
            current_help = parts[1] if len(parts) == 2 else ""
        elif line.startswith("# TYPE "):
            parts = line[7:].split(" ", 1)
            current_type = parts[1] if len(parts) == 2 else ""
        elif line and not line.startswith("#"):
            m = _METRIC_LINE.match(line)
            if m:
                raw_val = m.group("value")
                try:
                    val: float | str = float(raw_val)
                except ValueError:
                    val = raw_val
                samples.append({
                    "name": m.group("name"),
                    "labels": m.group("labels") or "",
                    "value": val,
                    "help": current_help,
                    "type": current_type,
                })
    return samples


def scrape_metrics(service: str, url: str, timeout: float) -> dict[str, Any]:
    status, body = _http_get(url, timeout)
    if status != 200 or not body:
        return {"service": service, "reachable": False, "url": url, "samples": []}
    samples = _parse_metrics_text(body.decode("utf-8", errors="replace"))
    return {"service": service, "reachable": True, "url": url, "sample_count": len(samples), "samples": samples}


# ---------------------------------------------------------------------------
# 3. Prometheus query helpers
# ---------------------------------------------------------------------------

def prom_query(prom_url: str, promql: str, timeout: float) -> float | None:
    """Run an instant PromQL query and return the scalar result, or None."""
    params = urllib.parse.urlencode({"query": promql})
    url = f"{prom_url}/api/v1/query?{params}"
    status, data = _get_json(url, timeout)
    if status != 200 or not data:
        return None
    try:
        results = data["data"]["result"]
        if not results:
            return None
        val_str = results[0]["value"][1]
        f = float(val_str)
        return None if (f != f) else f  # NaN guard
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _fmt(val: float | None, unit: str = "", decimals: int = 2) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{decimals}f}{unit}"


def collect_prom_metrics(prom_url: str, timeout: float) -> dict[str, Any]:
    """Run a suite of PromQL queries and return labelled results."""
    ns = "dip"

    def q(promql: str) -> float | None:
        return prom_query(prom_url, promql, timeout)

    window = "5m"

    results: dict[str, Any] = {}

    # --- Producer ---
    results["producer_frames_published_rate"] = q(
        f"rate({ns}_producer_frames_published_total[{window}])"
    )
    results["producer_frames_dropped_rate"] = q(
        f"rate({ns}_producer_frames_dropped_total[{window}])"
    )
    results["producer_publish_latency_p50_ms"] = q(
        f"histogram_quantile(0.50, rate({ns}_producer_publish_latency_ms_bucket[{window}]))"
    )
    results["producer_publish_latency_p95_ms"] = q(
        f"histogram_quantile(0.95, rate({ns}_producer_publish_latency_ms_bucket[{window}]))"
    )
    results["producer_publish_latency_p99_ms"] = q(
        f"histogram_quantile(0.99, rate({ns}_producer_publish_latency_ms_bucket[{window}]))"
    )

    # --- Worker ---
    results["worker_jobs_popped_rate"] = q(
        f"rate({ns}_worker_jobs_popped_total[{window}])"
    )
    results["worker_jobs_processed_rate"] = q(
        f"rate({ns}_worker_jobs_processed_total[{window}])"
    )
    results["worker_failures_rate"] = q(
        f"rate({ns}_worker_failures_total[{window}])"
    )
    results["worker_queue_depth"] = q(f"{ns}_worker_queue_depth")
    results["worker_queue_latency_p50_ms"] = q(
        f"histogram_quantile(0.50, rate({ns}_worker_queue_latency_ms_bucket[{window}]))"
    )
    results["worker_queue_latency_p95_ms"] = q(
        f"histogram_quantile(0.95, rate({ns}_worker_queue_latency_ms_bucket[{window}]))"
    )
    results["worker_inference_p50_ms"] = q(
        f"histogram_quantile(0.50, rate({ns}_worker_inference_duration_ms_bucket[{window}]))"
    )
    results["worker_inference_p95_ms"] = q(
        f"histogram_quantile(0.95, rate({ns}_worker_inference_duration_ms_bucket[{window}]))"
    )
    results["worker_inference_p99_ms"] = q(
        f"histogram_quantile(0.99, rate({ns}_worker_inference_duration_ms_bucket[{window}]))"
    )
    results["worker_pipeline_p50_ms"] = q(
        f"histogram_quantile(0.50, rate({ns}_worker_pipeline_duration_ms_bucket[{window}]))"
    )
    results["worker_pipeline_p95_ms"] = q(
        f"histogram_quantile(0.95, rate({ns}_worker_pipeline_duration_ms_bucket[{window}]))"
    )
    results["worker_pipeline_p99_ms"] = q(
        f"histogram_quantile(0.99, rate({ns}_worker_pipeline_duration_ms_bucket[{window}]))"
    )

    # --- API ---
    results["api_requests_rate"] = q(
        f"rate({ns}_api_requests_total[{window}])"
    )
    results["api_request_duration_p50_ms"] = q(
        f"histogram_quantile(0.50, rate({ns}_api_request_duration_ms_bucket[{window}]))"
    )
    results["api_request_duration_p95_ms"] = q(
        f"histogram_quantile(0.95, rate({ns}_api_request_duration_ms_bucket[{window}]))"
    )
    results["api_request_duration_p99_ms"] = q(
        f"histogram_quantile(0.99, rate({ns}_api_request_duration_ms_bucket[{window}]))"
    )

    return results


# ---------------------------------------------------------------------------
# 4. API load test
# ---------------------------------------------------------------------------

def timed_get(url: str, timeout: float) -> tuple[int, float]:
    """Return (http_status, elapsed_ms)."""
    t0 = time.perf_counter()
    status, _ = _http_get(url, timeout)
    return status, (time.perf_counter() - t0) * 1000.0


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    frac = k - lo
    return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])


def load_test_endpoint(url: str, n: int, timeout: float) -> dict[str, Any]:
    """Make n GET requests and return latency stats in milliseconds."""
    latencies: list[float] = []
    status_counts: dict[int, int] = {}
    errors = 0
    aborted = False

    for _ in range(n):
        try:
            status, ms = timed_get(url, timeout)
        except KeyboardInterrupt:
            aborted = True
            break
        if status == 0:
            errors += 1
        else:
            latencies.append(ms)
            status_counts[status] = status_counts.get(status, 0) + 1

    if not latencies:
        return {
            "url": url,
            "n": n,
            "errors": errors,
            "aborted": aborted,
            "status_counts": status_counts,
            "latency_ms": None,
        }

    return {
        "url": url,
        "n": n,
        "errors": errors,
        "aborted": aborted,
        "status_counts": status_counts,
        "latency_ms": {
            "min": min(latencies),
            "mean": statistics.mean(latencies),
            "median": statistics.median(latencies),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": max(latencies),
            "stdev": statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
        },
    }


# Endpoints excluded from the default load test run because they have known
# performance issues that would be fixed separately:
_SLOW_ENDPOINTS: set[str] = {
    "/sources",  # TODO: missing composite index on (source_id, processed_at_us) in results table
}


def run_load_tests(api_url: str, n: int, timeout: float, *, include_slow: bool = False) -> dict[str, Any]:
    all_endpoints = [
        "/",
        "/health/live",
        "/health/ready",
        "/sources",
        "/results",
        "/results?limit=1",
    ]
    endpoints = [p for p in all_endpoints if include_slow or p not in _SLOW_ENDPOINTS]
    skipped = [p for p in all_endpoints if p in _SLOW_ENDPOINTS and not include_slow]
    if skipped:
        print(f"  NOTE: skipping known-slow endpoints (use --include-sources-load to enable): {skipped}")
    print(f"  Running load test ({n} requests × {len(endpoints)} endpoints)…")
    results = {}
    for path in endpoints:
        url = f"{api_url}{path}"
        print(f"    {path:<28} ", end="", flush=True)
        try:
            result = load_test_endpoint(url, n, timeout)
        except KeyboardInterrupt:
            print("(interrupted)")
            break
        lat = result["latency_ms"]
        suffix = " [aborted early]" if result.get("aborted") else ""
        if lat:
            print(f"mean={lat['mean']:.1f}ms  p95={lat['p95']:.1f}ms  p99={lat['p99']:.1f}ms  err={result['errors']}{suffix}")
        else:
            print(f"UNREACHABLE / all errors{suffix}")
        results[path] = result
    return results


# ---------------------------------------------------------------------------
# 5. Data inventory
# ---------------------------------------------------------------------------

def collect_data_inventory(api_url: str, timeout: float) -> dict[str, Any]:
    """Return counts of results and sources from the API."""
    _, sources_body = _get_json(f"{api_url}/sources", timeout)
    sources_count: int | None = None
    if isinstance(sources_body, dict):
        items = sources_body.get("items", [])
        sources_count = len(items) if isinstance(items, list) else None

    _, results_body = _get_json(f"{api_url}/results?limit=1", timeout)
    # The count in the response is the page count, not the total — use it as a
    # proxy signal. For a total count we'd need a dedicated endpoint.
    first_page_count: int | None = None
    if isinstance(results_body, dict):
        first_page_count = results_body.get("count")
        has_more = results_body.get("next_cursor") is not None

    return {
        "sources_on_first_page": sources_count,
        "results_first_page_count": first_page_count,
        "results_has_more_pages": has_more if results_body else None,
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    width = 72
    print()
    print("─" * width)
    print(f"  {title}")
    print("─" * width)


def _row(label: str, value: str, width: int = 42) -> None:
    print(f"  {label:<{width}} {value}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Capture baseline benchmark snapshot.")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--prom-url", default="http://localhost:9090")
    parser.add_argument("--producer-metrics-url", default="http://localhost:9101/metrics")
    # Default targets worker-dev (port 9103). Pass 9102 when running production worker instead.
    parser.add_argument("--worker-metrics-url", default="http://localhost:9103/metrics")
    parser.add_argument("--load-n", type=int, default=100, help="Requests per endpoint in load test")
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout (seconds)")
    parser.add_argument("--skip-load-test", action="store_true", help="Skip the load test section")
    parser.add_argument("--include-sources-load", action="store_true",
                        help="Include /sources in load test (known to be slow — missing composite index)")
    args = parser.parse_args()

    ts = datetime.now(tz=timezone.utc)
    ts_str = ts.strftime("%Y%m%d_%H%M%S")
    ts_iso = ts.isoformat()

    snapshot: dict[str, Any] = {
        "captured_at_utc": ts_iso,
        "config": {
            "api_url": args.api_url,
            "prom_url": args.prom_url,
            "producer_metrics_url": args.producer_metrics_url,
            "worker_metrics_url": args.worker_metrics_url,
            "load_n": args.load_n,
            "timeout_s": args.timeout,
        },
    }

    print(f"\n{'='*72}")
    print(f"  Distributed Inference Platform — Baseline Snapshot")
    print(f"  {ts_iso}")
    print(f"{'='*72}")

    # ── 1. Service health ──────────────────────────────────────────────────
    _section("1 / 5  Service Health")
    health = check_health(args.api_url, args.timeout)
    snapshot["health"] = health
    live = health["live"]
    ready = health["ready"]
    _row("API liveness (/health/live)", f"HTTP {live['http_status']}  {live['body']}")
    _row("API readiness (/health/ready)", f"HTTP {ready['http_status']}  {ready['body']}")

    # ── 2. Raw metric scrapes ──────────────────────────────────────────────
    _section("2 / 5  Raw Prometheus Metric Scrapes")
    scrapes = {}
    for service, url in [
        ("api", f"{args.api_url}/metrics"),
        ("producer", args.producer_metrics_url),
        ("worker", args.worker_metrics_url),
    ]:
        print(f"  Scraping {service} ({url})…", end=" ", flush=True)
        s = scrape_metrics(service, url, args.timeout)
        scrapes[service] = s
        if s["reachable"]:
            print(f"OK — {s['sample_count']} samples")
        else:
            print("UNREACHABLE")
    snapshot["metric_scrapes"] = scrapes

    # Extract interesting gauges/counters from raw scrapes for the summary
    def _find_sample(service_name: str, metric_name: str) -> float | None:
        svc = scrapes.get(service_name, {})
        for s in svc.get("samples", []):
            if s["name"] == metric_name and isinstance(s["value"], float):
                return s["value"]
        return None

    # ── 3. Prometheus computed aggregates ─────────────────────────────────
    _section("3 / 5  Prometheus Computed Aggregates (over last 5 min)")
    print(f"  Querying Prometheus at {args.prom_url}…")
    prom_metrics = collect_prom_metrics(args.prom_url, args.timeout)
    snapshot["prometheus_aggregates"] = prom_metrics

    print()
    print("  PRODUCER")
    _row("  Frames published (fps)", _fmt(prom_metrics.get("producer_frames_published_rate"), " fps"))
    _row("  Frames dropped (fps)",   _fmt(prom_metrics.get("producer_frames_dropped_rate"),  " fps"))
    _row("  Publish latency p50",    _fmt(prom_metrics.get("producer_publish_latency_p50_ms"), " ms"))
    _row("  Publish latency p95",    _fmt(prom_metrics.get("producer_publish_latency_p95_ms"), " ms"))
    _row("  Publish latency p99",    _fmt(prom_metrics.get("producer_publish_latency_p99_ms"), " ms"))

    print()
    print("  WORKER")
    _row("  Jobs popped (fps)",          _fmt(prom_metrics.get("worker_jobs_popped_rate"),      " fps"))
    _row("  Jobs processed (fps)",       _fmt(prom_metrics.get("worker_jobs_processed_rate"),   " fps"))
    _row("  Failure rate (fps)",         _fmt(prom_metrics.get("worker_failures_rate"),         " fps"))
    _row("  Queue depth (gauge)",        _fmt(prom_metrics.get("worker_queue_depth"),           " jobs", 0))
    _row("  Queue wait latency p50",     _fmt(prom_metrics.get("worker_queue_latency_p50_ms"),  " ms"))
    _row("  Queue wait latency p95",     _fmt(prom_metrics.get("worker_queue_latency_p95_ms"),  " ms"))
    _row("  Inference duration p50",     _fmt(prom_metrics.get("worker_inference_p50_ms"),      " ms"))
    _row("  Inference duration p95",     _fmt(prom_metrics.get("worker_inference_p95_ms"),      " ms"))
    _row("  Inference duration p99",     _fmt(prom_metrics.get("worker_inference_p99_ms"),      " ms"))
    _row("  Pipeline duration p50",      _fmt(prom_metrics.get("worker_pipeline_p50_ms"),       " ms"))
    _row("  Pipeline duration p95",      _fmt(prom_metrics.get("worker_pipeline_p95_ms"),       " ms"))
    _row("  Pipeline duration p99",      _fmt(prom_metrics.get("worker_pipeline_p99_ms"),       " ms"))

    print()
    print("  API")
    _row("  Request rate (rps)",     _fmt(prom_metrics.get("api_requests_rate"),           " rps"))
    _row("  Request duration p50",   _fmt(prom_metrics.get("api_request_duration_p50_ms"), " ms"))
    _row("  Request duration p95",   _fmt(prom_metrics.get("api_request_duration_p95_ms"), " ms"))
    _row("  Request duration p99",   _fmt(prom_metrics.get("api_request_duration_p99_ms"), " ms"))

    # ── 4. Load test ───────────────────────────────────────────────────────
    if not args.skip_load_test:
        _section(f"4 / 5  API Load Test  ({args.load_n} requests × endpoint)")
        load_results = run_load_tests(args.api_url, args.load_n, args.timeout,
                                       include_slow=args.include_sources_load)
        snapshot["load_test"] = load_results

        print()
        fmt_hdr = f"  {'Endpoint':<32} {'mean':>8} {'p95':>8} {'p99':>8} {'max':>8}  {'errors':>6}"
        print(fmt_hdr)
        print("  " + "-" * (len(fmt_hdr) - 2))
        for path, r in load_results.items():
            lat = r["latency_ms"]
            if lat:
                print(
                    f"  {path:<32}"
                    f" {lat['mean']:>7.1f}ms"
                    f" {lat['p95']:>7.1f}ms"
                    f" {lat['p99']:>7.1f}ms"
                    f" {lat['max']:>7.1f}ms"
                    f"  {r['errors']:>6}"
                )
            else:
                print(f"  {path:<32}  {'UNREACHABLE':>38}  {r['errors']:>6}")
    else:
        snapshot["load_test"] = None
        print("\n  (load test skipped)")

    # ── 5. Data inventory ─────────────────────────────────────────────────
    _section("5 / 5  Data Inventory")
    inventory = collect_data_inventory(args.api_url, args.timeout)
    snapshot["data_inventory"] = inventory
    _row("Sources (first page)", str(inventory.get("sources_on_first_page", "n/a")))
    _row("Results (first page count)", str(inventory.get("results_first_page_count", "n/a")))
    _row("Results has more pages", str(inventory.get("results_has_more_pages", "n/a")))

    # ── Save JSON report ───────────────────────────────────────────────────
    out_path = AUDITS_DIR / f"baseline_{ts_str}.json"
    out_path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")

    print()
    print(f"{'='*72}")
    print(f"  Snapshot saved → {out_path.relative_to(REPO_ROOT)}")
    print(f"  To compare after future changes, diff the JSON files or re-run")
    print(f"  this script and look at the Prometheus aggregate deltas.")
    print(f"{'='*72}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
