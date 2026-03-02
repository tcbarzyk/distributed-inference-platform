# Distributed Inference Platform

Learning project for building a scalable distributed video inference system in small steps.

## Current System (As Implemented)

The platform currently has three application services plus infrastructure:

1. `producer`
- Reads frames from sample files or livestream input.
- Encodes frames and stores frame bytes in Redis with TTL.
- Pushes `QueueJob` metadata to a Redis queue.

2. `worker`
- Consumes jobs from Redis.
- Fetches and decodes frame bytes.
- Runs YOLOv8 inference.
- Publishes `InferenceResult` in one of two modes:
  - `local_jsonl` (implemented)
  - `postgres` (implemented)

3. `api`
- FastAPI service.
- Health endpoints (`/health/live`, `/health/ready`).
- DB-backed read endpoint (`GET /results`).

4. Infrastructure
- Redis for queue + frame blob handoff.
- PostgreSQL for durable metadata/results storage.
- Docker Compose for local orchestration.

## Repository Layout

```text
platform/
  libs/
    platform_shared/              # shared config, schemas, db utils/models
  services/
    producer/
    worker/
    api/
    postgres/data/                # local postgres volume data (ignored in git)
  docker-compose.yml
  .env
```

## Shared Contracts and DB Modules

- Shared event/data schemas:
  - `platform_shared.schemas` (`QueueJob`, `InferenceResult`, `Detection`)
- Shared DB modules:
  - `platform_shared.db_utils` (DB URL + engine/session factory helpers)
  - `platform_shared.db_base` (SQLAlchemy `Base`)
  - `platform_shared.models` (`SourceModel`, `JobModel`, `ResultModel`)

## Runtime Flow

1. Producer publishes frame bytes to Redis key `frame:<source_id>:<frame_id>`.
2. Producer enqueues `QueueJob` to Redis queue.
3. Worker consumes job, pulls frame, infers detections.
4. Worker publishes result (`local_jsonl` or PostgreSQL).
5. API reads persisted results from PostgreSQL (`GET /results`).

## Run with Docker Compose

From repo root:

```bash
docker compose up --build
```

Common endpoints:

- API root: `http://localhost:8000/`
- Liveness: `http://localhost:8000/health/live`
- Readiness: `http://localhost:8000/health/ready`
- Results: `http://localhost:8000/results`

## Faster Worker Dev Loop (No Rebuild for Code-Only Changes)

A `worker-dev` service is defined in `docker-compose.override.yml` under profile `dev`.
It bind-mounts:

- `services/worker/src`
- `libs/platform_shared/src`

This lets Python source edits take effect on container restart without rebuilding
the worker image.

Start dev worker:

```bash
docker compose --profile dev up -d worker-dev redis postgres
```

Restart after code edits:

```bash
docker compose restart worker-dev
```

You still need `docker compose build worker` when changing:

- `services/worker/Dockerfile`
- `services/worker/requirements.txt`
- system-level dependencies

## Database Migrations (Alembic)

Migrations live under `services/api/alembic`.

Apply latest migration:

```bash
docker compose run --rm --workdir /app/services/api api alembic -c alembic.ini upgrade head
```

Check current revision:

```bash
docker compose run --rm --workdir /app/services/api api alembic -c alembic.ini current
```

## Current Status

Implemented:

- Redis producer/worker pipeline
- Shared queue/result schemas
- API health and DB read endpoint
- Shared SQLAlchemy models and Alembic baseline migration

In progress:

- Live streaming results channel to frontend (WebSocket/SSE)
- Video streaming endpoint for browser UI

## Key Decisions and Tradeoffs

1. Redis list + frame key TTL for fast local iteration
- Tradeoff: simple and fast to prototype, but not yet durable/exactly-once.

2. Shared schema module (`platform_shared.schemas`)
- Tradeoff: stronger consistency across services, with some coordination overhead for schema changes.

3. Shared DB utility + model modules in `platform_shared`
- Tradeoff: prevents duplication and drift, but tightens coupling between services and shared package versioning.

4. Keep JSONL output mode while adding Postgres
- Tradeoff: dual paths increase complexity temporarily, but provide safer migration/debug fallback.

5. Health split into live vs ready
- Tradeoff: more explicit operations behavior, slightly more endpoint surface.

6. Docker-first local workflow
- Tradeoff: reproducible environment, but slower rebuild cycles than pure local venv runs.

## Future Features for a Complete Distributed System

1. Reliable delivery semantics
- Retry policy, dead-letter queue, visibility timeout/ack strategy, idempotency guarantees.

2. Reliable worker DB write semantics
- Retry/idempotency policy and robust job state transitions.

3. Realtime event fanout
- Worker publishes result events; API pushes via WebSocket/SSE.

4. Live video feed
- Start with MJPEG or WS frames, later consider WebRTC for low latency.

5. Backpressure and flow control
- Adaptive producer throttling and queue depth controls.

6. Observability
- Structured logs, metrics, tracing, dashboards, alerts.

7. API surface expansion
- Source registration/control, job lifecycle endpoints, pagination/cursors.

8. Security hardening
- AuthN/AuthZ, secret management, network policy, TLS.

9. Scalability upgrades
- Horizontal worker scaling, partitioning/sharding strategy, autoscaling triggers.

10. Durability and platform ops
- Backups, migration lifecycle policy, CI checks, chaos/load testing, deployment strategy.
