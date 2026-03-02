# API Service

FastAPI service for health checks, PostgreSQL reads, and live data delivery to the frontend.

This README documents:

1. What exists now.
2. The recommended v1 endpoint contract for the frontend.
3. How each endpoint maps to UI components (source selector, live feed, dashboard).

## Responsibilities

1. Expose liveness/readiness endpoints for operations.
2. Read normalized data from PostgreSQL (`sources`, `jobs`, `results`).
3. Provide frontend-ready query endpoints (history + latest state).
4. Provide live push channel (WebSocket) for low-latency updates.
5. Own migration tooling/config (Alembic) for schema evolution.

## Frontend Components and API Mapping

1. Source selector
- Reads source list and status.
- Primary endpoint: `GET /sources`

2. Live video/feed panel
- Needs latest annotated frame metadata and fast updates.
- Primary endpoints:
  - `GET /sources/{source_id}/latest-frame` (bootstrap/refresh)
  - `GET /sources/{source_id}/frame/latest.jpg` (latest image bytes)
  - `WS /ws/sources/{source_id}` (continuous updates)

3. Live metrics/events dashboard
- Needs both current counters and recent event stream.
- Primary endpoints:
  - `GET /sources/{source_id}/stats`
  - `GET /results` (filtered/paginated history)
  - `WS /ws/sources/{source_id}` (live events)

4. System health banner
- Shows dependency health and degraded mode warnings.
- Primary endpoints:
  - `GET /health/live`
  - `GET /health/ready`

## Endpoint Contract (Current + Recommended v1)

Status tags:

- `IMPLEMENTED`: already in service.
- `RECOMMENDED`: should be implemented next.

### 1) Service and health

1. `GET /` (`IMPLEMENTED`)
- Purpose: identify service and version.
- Frontend use: optional diagnostics screen.

2. `GET /health/live` (`IMPLEMENTED`)
- Purpose: process is up.
- Frontend use: basic heartbeat indicator.
- Notes: no external checks.

3. `GET /health/ready` (`IMPLEMENTED`)
- Purpose: dependency readiness (Redis/PostgreSQL).
- Frontend use: "degraded" banner if not ready.
- Behavior: `200` when ready, `503` when not.

4. `GET /health` (`IMPLEMENTED`)
- Alias to readiness.

### 2) Source discovery and state

1. `GET /sources` (`IMPLEMENTED`, minimal v1)
- Purpose: list all known video sources with status.
- Frontend use: source selector dropdown/list and status chips.
- Query params:
  - `active_only: bool = false`
  - `limit: int = 100` (bounded)
  - `cursor: string | null`
- Notes:
  - `is_active` is derived from last result recency.
  - Recency window is configured by `API_SOURCE_ACTIVE_WINDOW_SECONDS` (default `10`).
- Response example:

```json
{
  "items": [
    {
      "source_id": "KAB_SK_1_undist",
      "display_name": "Camera 1",
      "is_active": true,
      "last_capture_ts_us": 1384779359559997,
      "last_result_ts_us": 1384779359559997
    }
  ],
  "next_cursor": null
}
```

2. `GET /sources/{source_id}` (`IMPLEMENTED`, minimal v1)
- Purpose: details for one source.
- Frontend use: source detail header and metadata panel.
- Response includes:
  - source identity fields
  - latest timestamps
  - optional stream capability flags
- Notes:
  - Uses the same recency-based activity logic as `GET /sources`.
  - Returns `404` when the source does not exist.

### 3) Results/history reads

1. `GET /results` (`IMPLEMENTED`, paginated)
- Purpose: query result history.
- Frontend use: dashboard table, timeline chart, debugging panel.
- Implemented filters:
  - `source_id`
  - `since_us`
  - `until_us`
  - `kind` (`detection|unknown`)
  - `cursor` (keyset cursor by result id for newest-first pagination, integer id encoded as string in `next_cursor`)
  - `limit`
- Response shape (recommended):

```json
{
  "items": [
    {
      "id": 12345,
      "kind": "detection",
      "schema_version": 1,
      "job_id": "KAB_SK_1_undist:202:1384779359559997",
      "frame_id": 202,
      "source_id": "KAB_SK_1_undist",
      "status": "done",
      "model": "yolov8n",
      "inference_ms": 22.4,
      "pipeline_ms": 31.7,
      "processed_at_us": 1730488482000000,
      "detections": [
        {
          "label": "car",
          "class_id": 2,
          "confidence": 0.91,
          "bbox_xyxy": [10.0, 20.0, 120.0, 200.0]
        }
      ]
    }
  ],
  "next_cursor": "12345"
}
```

2. `GET /sources/{source_id}/events` (`IMPLEMENTED`, minimal v1)
- Purpose: source-scoped event/history read with stricter semantics than generic `/results`.
- Frontend use: per-source event feed.
- Query params:
  - `since_us`, `until_us`, `limit`, `cursor`
  - optional `kind` (`detection|unknown`)
- Notes:
  - Returns `404` if source does not exist.
  - Uses newest-first keyset pagination by result id.

### 4) Latest frame and source metrics

1. `GET /sources/{source_id}/latest-frame` (`IMPLEMENTED`, minimal v1)
- Purpose: fetch most recent frame metadata for immediate UI paint.
- Frontend use: initial image render before live socket data arrives.
- Response fields:
  - `source_id`
  - `capture_ts_us`
  - `annotated_image_url` or `frame_key`
  - `result_id` / `job_id`
- Notes:
  - If you store images in object storage (MinIO/S3), return URL.
  - If you keep Redis-only frame bytes, return a short-lived API URL that streams bytes.
  - Current implementation checks Redis live metadata first (`live:meta:latest:{source_id}`), then falls back to DB.
  - Current v1 uses `processed_at_us` as `capture_ts_us` fallback until capture timestamp is persisted in SQL.

2. `GET /sources/{source_id}/frame/latest.jpg` (`IMPLEMENTED`, minimal v1)
- Purpose: return latest live annotated frame bytes for a source.
- Frontend use: direct `<img>` source for bootstrap/live refresh loops.
- Behavior:
  - Resolves frame key from Redis live metadata.
  - Returns `image/jpeg` bytes when present.
  - Returns `404` when missing/expired.

3. `GET /sources/{source_id}/stats` (`IMPLEMENTED`, minimal v1)
- Purpose: current rolling metrics for dashboard cards.
- Frontend use: live KPI cards and small trend charts.
- Suggested fields:
  - `input_fps`, `processed_fps`
  - `avg_latency_ms`
  - `queue_depth`
  - `last_result_ts_us`
- Notes:
  - `processed_fps` is computed over `API_SOURCE_ACTIVE_WINDOW_SECONDS`.
  - `queue_depth` is current Redis queue length for `QUEUE_NAME` (global queue depth).

### 5) Live channel

1. `WS /ws/sources/{source_id}` (`RECOMMENDED`)
- Purpose: live push for frame/result/metric updates.
- Frontend use: real-time feed + dashboard updates without polling bursts.
- Message envelope (recommended):

```json
{
  "type": "result",
  "source_id": "KAB_SK_1_undist",
  "capture_ts_us": 1384779359559997,
  "payload": {
    "job_id": "KAB_SK_1_undist:202:1384779359559997",
    "kind": "detection",
    "annotated_image_url": "/media/annotated/KAB_SK_1_undist/1384779359559997.jpg",
    "metrics": {
      "inference_ms": 24.1
    }
  }
}
```

- Recommended event `type` values:
  - `result`
  - `metric`
  - `heartbeat`
  - `error`
- Operational notes:
  - Send heartbeat every 10-30s.
  - Use small payloads and reference URLs for large images.
  - Support reconnect by allowing client to provide `since_us` on reconnect via query param.

## Implementation Order (Backend)

1. Keep health endpoints as-is (done).
2. Add image delivery path for `latest-frame` (`annotated_image_url` or frame stream endpoint).
3. Implement richer source stats (input fps, per-source queue lag, latency percentiles).
4. Add `WS /ws/sources/{source_id}` and emit DB-backed live updates.
5. Add source/job management write endpoints (control-plane).

## Database Layer

Shared modules used by API:

- `platform_shared.db_utils`: DB URL + engine/session helpers
- `platform_shared.db_base`: SQLAlchemy `Base`
- `platform_shared.models`: shared ORM models

Recommended query ownership:

1. Keep route handlers thin.
2. Add query/service functions per endpoint area (`sources`, `results`, `stats`).
3. Keep response schemas in API service for explicit contract control.

## Alembic Migrations

Location:

- `services/api/alembic/`
- `services/api/alembic.ini`

Initial revision:

- `20260301_0001_initial_schema`

Run migration:

```bash
docker compose run --rm --workdir /app/services/api api alembic -c alembic.ini upgrade head
```

Check current revision:

```bash
docker compose run --rm --workdir /app/services/api api alembic -c alembic.ini current
```

## Key Files

- `src/main.py`: FastAPI routes, health checks
- `src/contracts.py`: OpenAPI request/response contract models
- `src/db.py`: API engine/session setup
- `alembic/env.py`: migration runtime wiring
- `alembic/versions/*`: schema revisions
- `Dockerfile`
- `requirements.txt`

## Run

From project root:

```bash
docker compose up --build api redis postgres
```

## API Config

- `API_SOURCE_ACTIVE_WINDOW_SECONDS`:
  - Default: `10`
  - Meaning: how recent a source's latest result must be to mark `is_active=true` in `GET /sources`.
- `WORKER_LIVE_FRAME_KEY_PREFIX` / `WORKER_LIVE_META_KEY_PREFIX`:
  - Shared key prefixes used by API to locate latest live frame metadata/bytes in Redis.

## Current Tradeoffs

1. Read-first API before full control plane.
- Tradeoff: faster iteration for frontend + observability, slower rollout of job/source management writes.

2. Health/readiness in same service as query API.
- Tradeoff: simpler deployment now, but future split into API gateway + internal services may scale better.

3. WebSocket-first live path (recommended) vs polling-only.
- Tradeoff: better latency and lower poll overhead, but more connection/state management complexity.

4. Endpoint contract stabilization before frontend implementation.
- Tradeoff: short backend-first delay, much lower frontend churn.
