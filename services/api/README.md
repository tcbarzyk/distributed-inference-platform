# API Service

FastAPI service for health, readiness, and database-backed result reads.

## Responsibilities

1. Expose health endpoints for orchestration/ops.
2. Validate dependency readiness (Redis + PostgreSQL).
3. Provide result query endpoint from PostgreSQL.
4. Own migration tooling/config (Alembic).

## Current Endpoints

1. `GET /`
- Basic service info.

2. `GET /health/live`
- Process liveness only (no external dependency checks).

3. `GET /health/ready`
- Dependency readiness:
  - Redis ping
  - PostgreSQL `SELECT 1`
- Returns `200` when ready, `503` otherwise.

4. `GET /health`
- Alias for `/health/ready`.

5. `GET /results`
- Reads from `results` table.
- Supports:
  - `source_id` filter
  - `since_us` filter
  - `limit` (bounded)

## Database Layer

Uses shared modules:

- `platform_shared.db_utils`: DB URL + engine/session helpers
- `platform_shared.db_base`: SQLAlchemy `Base`
- `platform_shared.models`: shared ORM models

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

- `src/main.py`: FastAPI routes and checks
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

## Current Tradeoffs

1. Read API only (no write/control endpoints yet).
- Tradeoff: narrow surface for now, delayed orchestration/control features.

2. Polling-friendly endpoint first (`GET /results`).
- Tradeoff: fast to ship; realtime push channels still pending.

3. Alembic owned by API service directory.
- Tradeoff: clear ownership, but requires careful shared-model import wiring.

## Near-term Improvements

1. WebSocket/SSE endpoint for live results.
2. Source/job management endpoints.
3. Pagination/cursor strategy for high-volume result history.
