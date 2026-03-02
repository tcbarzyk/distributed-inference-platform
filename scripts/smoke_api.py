#!/usr/bin/env python3
"""Simple API smoke test for frontend-readiness checks.

Checks:
1. GET /health/ready
2. GET /sources
3. GET /sources/{source_id}/latest-frame
4. GET /sources/{source_id}/frame/latest.jpg
5. GET /results pagination + no duplicate ids across first two pages
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request


class SmokeFailure(RuntimeError):
    pass


def _request(url: str, timeout_s: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url=url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = int(resp.getcode())
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read()
            return status, headers, body
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        body = exc.read() if exc.fp else b""
        return int(exc.code), headers, body
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise SmokeFailure(f"Request failed for {url}: {exc}") from exc


def _get_json(base_url: str, path: str, timeout_s: float) -> tuple[int, dict]:
    url = f"{base_url.rstrip('/')}{path}"
    status, _, body = _request(url, timeout_s)
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except Exception as exc:
        raise SmokeFailure(f"{path} returned non-JSON payload (status={status}): {exc}") from exc
    return status, payload


def _get_bytes(base_url: str, path: str, timeout_s: float) -> tuple[int, dict[str, str], bytes]:
    url = f"{base_url.rstrip('/')}{path}"
    return _request(url, timeout_s)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run API smoke checks.")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--timeout", type=float, default=3.0, help="Per-request timeout seconds")
    parser.add_argument(
        "--require-live-frame",
        action="store_true",
        help="Fail if /frame/latest.jpg is not available (404).",
    )
    parser.add_argument(
        "--require-mjpeg-stream",
        action="store_true",
        help="Fail if /streams/{source_id}.mjpeg does not yield frame bytes within timeout.",
    )
    args = parser.parse_args()

    failures: list[str] = []
    warnings: list[str] = []

    def check(cond: bool, ok_msg: str, fail_msg: str) -> None:
        if cond:
            print(f"[PASS] {ok_msg}")
        else:
            print(f"[FAIL] {fail_msg}")
            failures.append(fail_msg)

    # 1) Health readiness
    try:
        status, ready = _get_json(args.base_url, "/health/ready", args.timeout)
    except SmokeFailure as exc:
        print(f"[FAIL] {exc}")
        print(
            "[HINT] Ensure docker services are running (api/redis/postgres) and "
            f"the base URL is correct: {args.base_url}"
        )
        return 1
    check(
        status == 200 and ready.get("status") == "ok",
        "GET /health/ready returned ready=ok",
        f"GET /health/ready unexpected response: status={status}, body={ready}",
    )

    # 2) Sources list
    try:
        status, sources_resp = _get_json(args.base_url, "/sources?limit=5", args.timeout)
    except SmokeFailure as exc:
        print(f"[FAIL] {exc}")
        return 1
    items = sources_resp.get("items") if isinstance(sources_resp, dict) else None
    source_id = items[0].get("source_id") if isinstance(items, list) and items else None
    check(
        status == 200 and isinstance(items, list) and len(items) > 0 and isinstance(source_id, str),
        "GET /sources returned at least one source",
        f"GET /sources unexpected response: status={status}, body={sources_resp}",
    )
    if not source_id:
        print("[ABORT] Cannot continue without a source_id from /sources.")
        return 1

    quoted_source = urllib.parse.quote(source_id, safe="")

    # 3) Latest frame metadata
    latest_path = f"/sources/{quoted_source}/latest-frame"
    try:
        status, latest_resp = _get_json(args.base_url, latest_path, args.timeout)
    except SmokeFailure as exc:
        print(f"[FAIL] {exc}")
        failures.append(str(exc))
        latest_resp = {}
        status = 0
    check(
        status == 200 and isinstance(latest_resp, dict),
        f"GET {latest_path} returned metadata",
        f"GET {latest_path} unexpected response: status={status}, body={latest_resp}",
    )

    # 4) Latest frame image
    image_path = f"/sources/{quoted_source}/frame/latest.jpg"
    try:
        status, headers, body = _get_bytes(args.base_url, image_path, args.timeout)
        content_type = headers.get("content-type", "")
        if status == 200:
            check(
                content_type.startswith("image/jpeg") and len(body) > 0,
                f"GET {image_path} returned JPEG bytes",
                f"GET {image_path} expected JPEG but got content_type={content_type} bytes={len(body)}",
            )
        else:
            msg = f"GET {image_path} returned status={status} (live frame may be unavailable/expired)"
            if args.require_live_frame:
                print(f"[FAIL] {msg}")
                failures.append(msg)
            else:
                print(f"[WARN] {msg}")
                warnings.append(msg)
    except SmokeFailure as exc:
        msg = str(exc)
        if args.require_live_frame:
            print(f"[FAIL] {msg}")
            failures.append(msg)
        else:
            print(f"[WARN] {msg}")
            warnings.append(msg)

    # 4b) MJPEG stream endpoint
    mjpeg_path = f"/streams/{quoted_source}.mjpeg"
    mjpeg_url = f"{args.base_url.rstrip('/')}{mjpeg_path}"
    try:
        req = urllib.request.Request(url=mjpeg_url, method="GET")
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            status = int(resp.getcode())
            content_type = (resp.headers.get("Content-Type") or "").lower()
            is_mjpeg = content_type.startswith("multipart/x-mixed-replace")
            if not (status == 200 and is_mjpeg):
                msg = f"GET {mjpeg_path} expected multipart MJPEG; status={status} content_type={content_type}"
                print(f"[FAIL] {msg}")
                failures.append(msg)
            else:
                print(f"[PASS] GET {mjpeg_path} returned multipart MJPEG headers")
                if args.require_mjpeg_stream:
                    sample = resp.read(64)
                    if len(sample) > 0:
                        print(f"[PASS] GET {mjpeg_path} produced MJPEG stream bytes")
                    else:
                        msg = f"GET {mjpeg_path} returned headers but no stream bytes"
                        print(f"[FAIL] {msg}")
                        failures.append(msg)
                else:
                    print(f"[INFO] GET {mjpeg_path} stream-byte check skipped (use --require-mjpeg-stream)")
    except urllib.error.HTTPError as exc:
        msg = f"GET {mjpeg_path} returned HTTP {exc.code}"
        print(f"[FAIL] {msg}")
        failures.append(msg)
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        msg = f"GET {mjpeg_path} request failed: {exc}"
        if args.require_mjpeg_stream:
            print(f"[FAIL] {msg}")
            failures.append(msg)
        else:
            print(f"[WARN] {msg}")
            warnings.append(msg)

    # 5) Results pagination / no duplicate ids across pages
    try:
        status, page1 = _get_json(args.base_url, "/results?limit=5", args.timeout)
    except SmokeFailure as exc:
        print(f"[FAIL] {exc}")
        failures.append(str(exc))
        status = 0
        page1 = {}
    page1_items = page1.get("items") if isinstance(page1, dict) else None
    check(
        status == 200 and isinstance(page1_items, list),
        "GET /results page 1 returned list payload",
        f"GET /results page 1 unexpected response: status={status}, body={page1}",
    )
    if status == 200 and isinstance(page1_items, list):
        next_cursor = page1.get("next_cursor")
        ids1 = [
            item_id
            for item in page1_items
            if isinstance(item, dict)
            for item_id in [item.get("id")]
            if isinstance(item_id, int)
        ]
        if next_cursor:
            page2_path = f"/results?limit=5&cursor={urllib.parse.quote(str(next_cursor), safe='')}"
            status2, page2 = _get_json(args.base_url, page2_path, args.timeout)
            page2_items = page2.get("items") if isinstance(page2, dict) else None
            check(
                status2 == 200 and isinstance(page2_items, list),
                "GET /results page 2 returned list payload",
                f"GET /results page 2 unexpected response: status={status2}, body={page2}",
            )
            if status2 == 200 and isinstance(page2_items, list):
                ids2 = [
                    item_id
                    for item in page2_items
                    if isinstance(item, dict)
                    for item_id in [item.get("id")]
                    if isinstance(item_id, int)
                ]
                overlap = set(ids1).intersection(ids2)
                check(
                    len(overlap) == 0,
                    "GET /results pagination has no duplicate ids across first two pages",
                    f"Duplicate ids across paginated /results pages: {sorted(overlap)}",
                )
        else:
            print("[INFO] /results returned no next_cursor (single-page result set).")

    if warnings:
        print(f"\nWarnings: {len(warnings)}")
        for msg in warnings:
            print(f"- {msg}")

    if failures:
        print(f"\nSmoke test FAILED ({len(failures)} checks failed).")
        return 1

    print("\nSmoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
