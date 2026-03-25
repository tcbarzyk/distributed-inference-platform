# Distributed Inference Platform

Learning project for building a scalable distributed video inference system in small steps.

## Current System (As Implemented)

The platform currently has three application services plus infrastructure:

1. `producer`
- Reads frames from sample files or livestream input.
- Encodes frames and stores frame bytes in Redis with TTL.
- Pushes `QueueJob` metadata to a Redis queue.
- Supports merged timeline mode and optional parallel-per-source publish mode.

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
- DB-backed read endpoints (`/results`, `/sources`, `/sources/{source_id}`, `/sources/{source_id}/events`, `/sources/{source_id}/stats`).
- Live-frame bootstrap endpoints:
  - `GET /sources/{source_id}/latest-frame` (Redis-first, DB fallback)
  - `GET /sources/{source_id}/frame/latest.jpg` (latest JPEG bytes from Redis key)
  - `GET /streams/{source_id}.mjpeg` (continuous MJPEG stream via Redis Pub/Sub)

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
5. Worker writes latest annotated frame + metadata to Redis (feature-flagged).
6. API reads persisted results from PostgreSQL and live frame metadata/bytes from Redis.

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
- Sources: `http://localhost:8000/sources`

## API Smoke Test

Run a quick frontend-readiness smoke test:

```bash
python scripts/smoke_api.py
```

Strict mode (fails if live frame image endpoint is not currently serving bytes):

```bash
python scripts/smoke_api.py --require-live-frame
```

Strict MJPEG mode (also requires stream endpoint to emit bytes within timeout):

```bash
python scripts/smoke_api.py --require-live-frame --require-mjpeg-stream
```

## Benchmark Suite

A new benchmark suite scaffold lives in `benchmarks/`.

This is the long-term replacement for one-off audit scripts when you want
repeatable performance comparisons across changes.

Phase 1 currently includes:

- a top-level CLI entry point
- a canonical benchmark result schema
- schema documentation
- executable `baseline`, `worker-step`, and `api-step` profiles
- a `compare` command for before/after deltas

Run the CLI:

```bash
python -m benchmarks --help
```

Create a benchmark manifest:

```bash
python -m benchmarks plan --profile baseline --label current-baseline
```

Run a baseline benchmark:

```bash
python -m benchmarks baseline --label current-baseline
```

Run a paced worker sweep:

```bash
python -m benchmarks worker-step --threads 2 --fps-steps 10,20,30,40 --step-duration 60 --stop-on-saturation
```

Run a paced API sweep:

```bash
python -m benchmarks api-step --concurrency-steps 1,2,4,8,12,16 --step-duration 60 --stop-on-saturation
```

Validate a benchmark result:

```bash
python -m benchmarks validate audits/benchmarks/<run>.json
```

Compare two benchmark results:

```bash
python -m benchmarks compare old.json new.json
```

Read the benchmark docs:

- `benchmarks/README.md`
- `benchmarks/SCHEMA.md`

## Worker Optimization Log

Use this section as the brief changelog for worker performance phases. Keep each
entry short and link to the detailed benchmark/audit notes under
`audits/benchmarks/worker/`.

### Phase 1: Reduce Per-Frame Overhead And Decouple Durable Publishing

- Detailed notes: [`audits/benchmarks/worker/worker_performance_improvement_20260325.md`](/C:/Users/tcbar/Desktop/ds/platform/audits/benchmarks/worker/worker_performance_improvement_20260325.md)
- Baseline benchmark reference: [`audits/benchmarks/worker/v0/bench_94eb060b872f.json`](/C:/Users/tcbar/Desktop/ds/platform/audits/benchmarks/worker/v0/bench_94eb060b872f.json)
- Latest benchmark reference from this phase: [`audits/benchmarks/worker/v1/bench_fc895e68df71.json`](/C:/Users/tcbar/Desktop/ds/platform/audits/benchmarks/worker/v1/bench_fc895e68df71.json)
- Implemented:
  - worker stage timing instrumentation
  - periodic queue-depth sampling instead of per-frame `LLEN`
  - gated debug log construction
  - shared detection payload reuse
  - shared JPEG reuse across live-frame and MJPEG paths
  - reduced Postgres lookup/update overhead
  - background thread for concurrent Postgres/JSONL result writes
- Key design decisions:
  - keep concurrency inside the existing worker process
  - decouple durable result publishing before redesigning live streaming
  - keep live-frame Redis writes and MJPEG Pub/Sub on the foreground path for now
  - retain minimal `jobs` inserts because `results.job_id` still has a foreign key to `jobs.job_id`
- Measured outcome:
  - `final_worker_processed_rate` improved from `39.44066625450049` to `49.65996290504419`
  - net gain: about `+10.22 fps`

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
- Worker live-frame Redis write path (configurable enable/cadence/TTL)
- API health and DB read endpoints
- Shared SQLAlchemy models and Alembic baseline migration

In progress:

- Live streaming results channel to frontend (WebSocket/SSE)
- Richer live stats and per-source queue lag metrics

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

7. Optional producer parallel-source mode
- Tradeoff: increases per-source throughput and realism, but removes global cross-source ordering guarantees.

8. Redis as live-frame transport/cache (not DB blob storage)
- Decision: worker writes latest annotated JPEG + metadata to Redis keys per source.
- Tradeoff: low-latency bootstrap and simple API integration, but frames are ephemeral and can expire between reads.

9. API `latest-frame` is Redis-first with DB fallback
- Decision: API checks live Redis metadata first, then falls back to latest DB result metadata.
- Tradeoff: better user experience when live data exists, but mixed data freshness semantics require clear frontend handling.

10. Keyset pagination for result/event reads
- Decision: `/results` and `/sources/{source_id}/events` use cursor-based keyset pagination.
- Tradeoff: stable and scalable under writes, but no random "jump to page N" behavior.

11. Shared config across services
- Decision: new live-frame controls are centralized in shared config (`WORKER_LIVE_*`, `API_SOURCE_ACTIVE_WINDOW_SECONDS`).
- Tradeoff: consistency and less duplication, but config changes can affect multiple services at once.

12. Layered live video transport
- Decision: keep simple latest-frame JPEG endpoint and add MJPEG stream endpoint.
- Tradeoff: fast path to browser demo and debugging, but MJPEG is bandwidth-heavy vs WebRTC-class transports.

## Future Features for a Complete Distributed System

1. Reliable delivery semantics
- Retry policy, dead-letter queue, visibility timeout/ack strategy, idempotency guarantees.

2. Reliable worker DB write semantics
- Retry/idempotency policy and robust job state transitions.

3. Realtime event fanout
- Worker publishes result events; API pushes via WebSocket/SSE.

4. Live video feed
- Bootstrap available now via `GET /sources/{source_id}/frame/latest.jpg`.
- Continuous MJPEG stream now available via `GET /streams/{source_id}.mjpeg`.
- Next: WebSocket events + later WebRTC for lower latency/better bandwidth.

4a. Realtime fanout transport
- Implement source-scoped WebSocket events and/or MJPEG stream endpoint for continuous updates.

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

## Frontend Design (Planned)

The frontend is planned as a live operations dashboard with these core components:

1. Source selector
- Select active camera/source context for all panels.

2. Live video feed
- Show annotated live frames for selected source.
- Current bootstrap: `GET /sources/{source_id}/latest-frame` + `/sources/{source_id}/frame/latest.jpg`.
- Current continuous transport: `GET /streams/{source_id}.mjpeg`.
- Later upgrade path: WebRTC for lower latency/better bandwidth efficiency.

3. Live dashboard
- Real-time events and operational metrics.
- Panels for throughput, latency, queue lag, errors, recent detections.

4. Chaos controls (later)
- Controlled fault injection (for example stop/restart worker) behind admin controls.

### SQL + WebSocket Data Model for Frontend

The frontend should use both SQL-backed APIs and realtime streaming:

1. SQL-backed REST (history/filters/source of truth)
- On load: hydrate from DB-backed endpoints (`/sources`, `/results`).
- Use for filtering, pagination, historical charts, and reconnect recovery.

2. WebSocket stream (live updates)
- Subscribe to `/ws/sources/{source_id}` for low-latency source-scoped updates.
- Append live events to UI while keeping a bounded in-memory window.

3. Reconciliation
- On WS reconnect/tab resume, re-fetch recent DB results (for example `since_us`) to fill gaps.

This pattern ensures:
- durable correctness via SQL
- responsive UX via streaming

### Metrics Strategy

Use two metric layers:

1. App-level metrics (short-term)
- Derived from result events and DB queries.
- Useful for immediate operational UI feedback.

2. Observability stack metrics (long-term)
- Prometheus/Grafana metrics from service `/metrics` endpoints.
- Used for infrastructure/SRE-level monitoring and alerting.

## Next Steps (Execution Order)

1. Validate end-to-end live-frame bootstrap path.
- Run stack, verify worker live-frame writes, and confirm:
  - `GET /sources/{source_id}/latest-frame`
  - `GET /sources/{source_id}/frame/latest.jpg`
  - `GET /streams/{source_id}.mjpeg`

2. Add API endpoints for frontend hydration.
- `GET /sources` (implemented)
- `GET /sources/{source_id}` (implemented)
- `GET /results` (implemented, keyset pagination + filters)
- `GET /sources/{source_id}/events` (implemented)

3. Add realtime result stream.
- Worker publishes post-commit result events (Redis Pub/Sub or equivalent).
- API exposes `WS /ws/sources/{source_id}` and fanout to clients.

4. Add live annotated video endpoint.
- `GET /sources/{source_id}/frame/latest.jpg` is implemented for bootstrap/latest frame reads.
- `GET /streams/{source_id}.mjpeg` is implemented for continuous playback.
- Next: optimize stream backpressure and evaluate WebRTC upgrade.

5. Build frontend v1.
- Source selector
- Live video panel
- Live events/metrics dashboard
- Historical results table fed by SQL-backed API

6. Add reconnect and gap-recovery behavior.
- WS reconnect with backoff
- DB catch-up query on reconnect

7. Expand metrics and dashboards.
- Introduce Prometheus metrics endpoints and Grafana dashboards.

8. Define and implement job/result reliability semantics.
- Duplicate policy
- Retry/idempotency behavior
- Optional dead-letter workflow

9. Add guarded chaos tooling.
- Admin-only controls to simulate worker failures/restarts.

10. Prepare demo and ops checklist.
- Health checks, startup order, migration command, smoke tests.
