"""Basic benchmark preflight checks."""

from __future__ import annotations

from typing import Any


def check_api_ready(*, api_url: str, timeout: float, get_json) -> dict[str, Any]:
    status, body = get_json(f"{api_url}/health/ready", timeout)
    ok = status == 200 and isinstance(body, dict) and body.get("status") == "ok"
    return {
        "name": "api_ready",
        "ok": ok,
        "detail": {"status": status, "body": body},
        "error": None if ok else "API readiness check failed",
    }


def check_prometheus_ready(*, prom_url: str, timeout: float, http_get) -> dict[str, Any]:
    status, _, _ = http_get(f"{prom_url}/-/ready", timeout)
    ok = status == 200
    return {
        "name": "prometheus_ready",
        "ok": ok,
        "detail": {"status": status, "url": f"{prom_url}/-/ready"},
        "error": None if ok else "Prometheus readiness check failed",
    }


def check_redis_queue_state(
    *,
    redis_host: str,
    redis_port: int,
    redis_password: str | None,
    queue_name: str,
) -> dict[str, Any]:
    try:
        import redis as redislib
    except ImportError:
        return {
            "name": "redis_queue_state",
            "ok": False,
            "detail": None,
            "error": "Python package 'redis' is not installed",
        }

    try:
        client = redislib.Redis(
            host=redis_host,
            port=redis_port,
            password=redis_password or None,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        ping_ok = bool(client.ping())
        queue_length = int(client.llen(queue_name))
        sample_frame_keys = []
        for key in client.scan_iter(match="frame:bench:*", count=20):
            sample_frame_keys.append(
                key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
            )
            if len(sample_frame_keys) >= 5:
                break
        ok = ping_ok and queue_length == 0 and len(sample_frame_keys) == 0
        errors: list[str] = []
        if not ping_ok:
            errors.append("Redis ping failed")
        if queue_length != 0:
            errors.append(f"Queue '{queue_name}' is not empty (LLEN={queue_length})")
        if sample_frame_keys:
            errors.append("Benchmark frame keys already exist in Redis")
        return {
            "name": "redis_queue_state",
            "ok": ok,
            "detail": {
                "queue_name": queue_name,
                "queue_length": queue_length,
                "sample_frame_keys": sample_frame_keys,
            },
            "error": None if ok else "; ".join(errors),
        }
    except Exception as exc:
        return {
            "name": "redis_queue_state",
            "ok": False,
            "detail": None,
            "error": f"Redis preflight failed: {exc}",
        }


def summarize_checks(checks: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    ok = all(bool(check.get("ok")) for check in checks)
    errors = [str(check["error"]) for check in checks if check.get("error")]
    return ok, errors
