# Distributed Inference Platform — Audit & Development Plan

Comprehensive system audit, development roadmap, and feature proposals.

**Audit date:** 2026-03-02  
**Scope:** All services (producer, worker, API), shared library, infrastructure, Docker orchestration.

---

## Table of Contents

1. [Architecture Summary](#architecture-summary)  
2. [Bugs and Correctness Issues](#bugs-and-correctness-issues)  
3. [Scalability Bottlenecks](#scalability-bottlenecks)  
4. [Reliability Gaps](#reliability-gaps)  
5. [Missing Features](#missing-features)  
6. [Development Roadmap](#development-roadmap)  
7. [Creative Feature Ideas](#creative-feature-ideas)

---

## Architecture Summary

The platform is a three-service pipeline orchestrated by Docker Compose:

```
┌───────────┐       Redis LIST       ┌───────────┐       PostgreSQL       ┌──────────┐
│  Producer  │─── frame blobs + ────▶│  Worker   │─── results rows ─────▶│   API    │
│            │   QueueJob metadata    │  (YOLO)   │                       │ (FastAPI)│
└───────────┘                        └───────────┘                       └──────────┘
      │                                    │                                   │
      │         Redis SET (TTL)            │   Redis SET (live frame)          │
      └───────────────────────────────────▶│──────────────────────────────────▶│
                                           │   Redis PUB/SUB (MJPEG)          │
                                           └──────────────────────────────────▶│
```

**Current strengths:**
- Clean service separation with shared schemas/models via `platform_shared`.
- Dual output modes (JSONL + Postgres) for safe migration.
- Keyset pagination on read endpoints.
- MJPEG live stream via Redis Pub/Sub.
- Configurable replay modes (realtime, fixed FPS, max speed).
- Health endpoints split into liveness vs readiness.
- Docker-first workflow with dev-mode bind mounts.

---

## Bugs and Correctness Issues

### B1. Worker missing `POSTGRES_HOST` in Docker Compose

**Severity:** High (breaks Postgres mode in Docker)  
**Location:** `docker-compose.yml`, worker service

The worker service environment only sets `REDIS_HOST: redis` but does **not** set `POSTGRES_HOST: postgres`. When `WORKER_RESULTS_MODE=postgres`, the worker resolves `POSTGRES_HOST` from the `.env` file, which may contain `localhost` — unreachable from within the Docker network.

The `docker-compose.override.yml` worker-dev service correctly sets both, confirming this is an oversight in the main compose file.

**Fix:** Add `POSTGRES_HOST: postgres` to the worker service environment block.

### FIXED

---

### B2. Redis connection leak in API service

**Severity:** High (resource exhaustion under load)  
**Location:** `services/api/src/main.py` — `_get_live_meta_for_source()`, `_get_live_frame_bytes_for_key()`, `_check_redis()`, `get_source_stats()`

Every API request that touches Redis creates a **new `redis.Redis()` connection**, uses it once, and closes it. Under concurrent load (multiple MJPEG viewers, stats polling, latest-frame checks), this creates a storm of TCP connect/disconnect cycles. Redis itself has a connection limit (default 10,000), and the overhead of establishing new connections is significant.

**Fix:** Create a single module-level `redis.ConnectionPool` (or `redis.Redis` with pool) at startup and share it across all request handlers. This is a one-line change in architecture but touches many call sites.

---

### B3. MJPEG stream blocks the async event loop

**Severity:** Medium (degrades API under concurrent streams)  
**Location:** `services/api/src/main.py` — `_iter_mjpeg_stream()`, `stream_source_mjpeg()`

`_iter_mjpeg_stream()` is a **synchronous generator** that performs blocking Redis Pub/Sub reads (`pubsub.get_message(timeout=1.0)`). When passed to `StreamingResponse`, FastAPI runs it in a thread pool worker. With the default thread pool size (typically 40), each active MJPEG viewer consumes one thread indefinitely. At ~40 concurrent viewers, the API can no longer serve any other requests.

**Fix:** Rewrite as an `async def` generator using `aioredis` or `redis.asyncio` for non-blocking Pub/Sub, or increase thread pool size and document the limit.

---

### B4. No backoff on Redis BRPOP failure in worker

**Severity:** Medium (CPU spin under Redis outage)  
**Location:** `services/worker/src/main.py` — main `while True` loop

When `r.brpop()` raises a `RedisError`, the worker logs the error and immediately `continue`s back to the top of the loop. If Redis is temporarily down, this creates a **tight hot-retry loop** consuming 100% CPU while spamming logs.

**Fix:** Add exponential backoff with jitter (e.g. start at 1s, cap at 30s, reset on success).

---

### B5. MJPEG publish gated by live-frame plan

**Severity:** Low-Medium (reduces MJPEG effective frame rate)  
**Location:** `services/worker/src/main.py` — `_publish_live_frame_safe()`

The MJPEG Pub/Sub publish is nested **inside** the live-frame write function and only executes when the live frame plan's `should_publish` is true. This means MJPEG frame rate is capped by `WORKER_LIVE_FRAMES_EVERY_N`, not just `WORKER_MJPEG_MAX_FPS`. If `WORKER_LIVE_FRAMES_EVERY_N=5`, the MJPEG stream sees at most 1/5th of processed frames regardless of the `WORKER_MJPEG_MAX_FPS` setting.

**Fix:** Decouple MJPEG publishing from the live-frame write path. Evaluate MJPEG publish independently in the main worker loop after building the result model.

---

### B6. No graceful shutdown (SIGTERM handling)

**Severity:** Medium (data loss on container stop)  
**Location:** All services

Docker sends `SIGTERM` on `docker compose down`. All three services only catch `KeyboardInterrupt` (SIGINT). On SIGTERM, the process is killed after the grace period without flushing metrics, closing DB sessions cleanly, or finishing in-progress frames.

**Fix:** Register a `signal.signal(signal.SIGTERM, ...)` handler that sets a shutdown flag checked by the main loop.

---

### B7. `capture_ts_us` not persisted in results table

**Severity:** Low (data loss for analytics)  
**Location:** `platform_shared/models.py` — `ResultModel`

The `QueueJob` carries `capture_ts_us` but the `ResultModel` only stores `processed_at_us`. For time-series analysis, latency computation, and forensic replay, the original capture timestamp is essential but lost after inference.

**Fix:** Add `capture_ts_us` column to the results table via Alembic migration. Populate it from `QueueJob.capture_ts_us` in the worker publish path.

---

### B8. Producer livestream has no reconnection logic

**Severity:** Medium (single failure kills the service)  
**Location:** `services/producer/src/main.py` — `_run_livestream_mode()`

When `cap.read()` returns `(False, None)`, the producer logs a warning and exits the loop permanently. For any real-world stream (RTSP cameras, network streams), transient disconnects are the norm.

**Fix:** Wrap the capture loop in a retry-with-backoff outer loop. Re-open `cv2.VideoCapture` on failure. Log reconnection attempts and count consecutive failures.

---

## Scalability Bottlenecks

### S1. Single Redis LIST queue for all sources

All frames from all source cameras are pushed to one Redis LIST. Workers pop from this single queue. This creates:
- **Head-of-line blocking:** A burst from one high-FPS source starves other sources.
- **No priority:** Cannot prioritize live streams over replay.
- **No partitioning:** Cannot route specific sources to specific worker pools.

**Recommendation:** Migrate to **Redis Streams** (`XADD`/`XREADGROUP`). Streams provide:
- Consumer groups with per-consumer acknowledgment.
- Message replay/redelivery on consumer failure.
- Built-in max-length trimming for backpressure.
- Per-stream partitioning by source.

---

### S2. Full frame bytes stored in Redis

At 640×480 JPEG quality 80, each frame is ~30–80 KB. At 25 FPS:
- **1 source:** ~1–2 MB/s into Redis
- **10 sources:** ~10–20 MB/s into Redis

Redis is an in-memory store. This can exhaust memory quickly with multiple sources, especially if workers fall behind and TTL hasn't expired old frames yet.

**Recommendation:** For scale, move frame blobs to an object store (MinIO/S3) and pass only a reference URL in the queue job. Redis then only carries lightweight metadata. For the current learning scope, consider reducing TTL aggressively and adding a queue-depth backpressure check.

---

### S3. No backpressure mechanism

The producer publishes as fast as it can (in `max` mode) or at stream rate (in `realtime`/`fixed` mode) regardless of how backed up the queue is. If workers are slow or down, the queue grows unboundedly.

**Recommendation:**
- Producer checks `LLEN` (or `XLEN` after migration) before each publish.
- If queue depth exceeds a configurable threshold, the producer drops frames or sleeps.
- Emit a metric/log when backpressure activates.

---

### S4. Single-threaded, single-frame worker inference

The worker processes exactly one frame at a time: fetch → decode → preprocess → infer → postprocess → publish. GPU utilization is low because:
- Most time is spent on I/O (Redis fetch, DB write), not inference.
- No batch inference (YOLO supports batch > 1).

**Recommendation (phased):**
1. **Phase 1:** Use `asyncio` or threading to overlap I/O with inference. Fetch the next frame while the current one is being inferred.
2. **Phase 2:** Batch inference. Accumulate N frames, run the YOLO model with `batch=N`, and publish all results.
3. **Phase 3:** Multi-process workers with GPU memory partitioning.

---

### S5. Per-frame PostgreSQL transactions

Every processed frame triggers a transaction containing:
- Source upsert (SELECT + conditional INSERT)
- Job upsert (SELECT + conditional INSERT/UPDATE)
- Result INSERT

At 25 FPS per source, this is 25 transactions/second per source, each with 3+ round trips.

**Recommendation:**
- **Batch writes:** Accumulate N results in memory and flush in a single transaction.
- **Background writer:** Decouple inference from DB writes using an internal queue. Worker infers at full speed; a background thread/coroutine batches DB writes.
- **Prepared statements:** Reuse compiled SQL statements to reduce per-query overhead.

---

### S6. No connection pooling in API Redis calls

Every Redis read in the API creates a new TCP connection. Under concurrent requests, this means dozens of simultaneous TCP handshakes.

**Recommendation:** Instantiate one `redis.ConnectionPool` at module level. Pass it to all `redis.Redis()` constructors. For the async path (MJPEG), use `redis.asyncio.ConnectionPool`.

---

### S7. One Pub/Sub subscription per MJPEG viewer

Each API client watching `/streams/{source_id}.mjpeg` creates its own Redis Pub/Sub subscription. With 100 viewers on one source, there are 100 subscriptions receiving identical data.

**Recommendation:** Implement a **server-side fan-out**. One subscription per source per API instance, broadcasting to all connected viewers via an in-process async queue or similar pattern.

---

## Reliability Gaps

### R1. No dead letter queue / retry mechanism

Failed jobs (inference error, DB write error, decode failure) are silently dropped. The only record is a log line and a metric counter. There is no way to:
- Retry a failed job.
- Inspect what failed and why.
- Measure failure rates over time without log scraping.

**Recommendation:**
- On failure, push the job (with failure reason and attempt count) to a separate Redis LIST/Stream (dead letter queue).
- Add a retry worker or manual retry endpoint in the API.
- Persist failure metadata in a `job_failures` table.

---

### R2. Frame blob TTL race condition

If workers fall behind (slow inference, many sources), frame blobs expire from Redis before being processed. The worker sees `blob_misses` and drops the job. This is already tracked in metrics but there's no mitigation.

**Recommendation:**
- Implement adaptive TTL: producer sets longer TTL when queue depth is high.
- Worker refreshes TTL on frame key when it pops the job (`EXPIRE` command).
- Consider a "frame already expired" fast path that skips the job without counting it as a processing failure.

---

### R3. No idempotency on result writes

If a worker crashes after committing to Postgres but before deleting the frame blob and acknowledging the job, a restarted worker could re-process the same frame and create a duplicate result row.

**Recommendation:**
- Add a unique constraint on `(job_id, schema_version)` or `(job_id, model)` in the results table.
- Use `INSERT ... ON CONFLICT DO NOTHING` for idempotent writes.

---

### R4. No health checks in Docker Compose

The `docker-compose.yml` has no `healthcheck` directives. Docker considers services "started" immediately, even if Redis/Postgres aren't ready. `depends_on` only waits for container start, not readiness.

**Recommendation:** Add healthchecks:
```yaml
redis:
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 5s
    timeout: 3s
    retries: 3

postgres:
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER"]
    interval: 5s
    timeout: 3s
    retries: 5

worker:
  depends_on:
    redis:
      condition: service_healthy
    postgres:
      condition: service_healthy
```

---

### R5. No structured logging

All services use plain-text `logging.basicConfig()` format. This makes log aggregation, parsing, and alerting difficult in any production-like environment.

**Recommendation:** Switch to JSON-structured logging (e.g., `python-json-logger`). Include service name, trace IDs, and key dimensions in every log line.

---

### R6. Monolithic config object

`ServiceConfig` contains **all** configuration for **all** services (50+ fields). The API loads producer settings, the producer loads worker settings, etc. While defaults prevent crashes, this creates confusion and makes it easy to miss a required variable.

**Recommendation:** Split into per-service config dataclasses (`ProducerConfig`, `WorkerConfig`, `ApiConfig`) that share a common `InfraConfig` base (Redis, Postgres, logging).

---

## Missing Features

### Core Platform

| Feature | Priority | Notes |
|---|---|---|
| WebSocket live channel (`WS /ws/sources/{source_id}`) | High | Stub exists, returns "Not implemented" |
| Frontend / web UI | High | No frontend exists; API is fully headless |
| Source management (CRUD) | Medium | Workers auto-create sources; no control-plane ownership |
| Job lifecycle state machine (queued → processing → done/failed) | Medium | Jobs jump directly to terminal status |
| Multi-model support / model registry | Medium | Hardcoded to yolov8n |
| Data retention / automatic cleanup | Medium | Results table grows forever |
| Input `capture_ts_us` persisted in DB | Medium | See bug B7 |
| Queue-per-source / source routing | Medium | See bottleneck S1 |

### Operations & DevOps

| Feature | Priority | Notes |
|---|---|---|
| Prometheus/OpenTelemetry metrics export | High | Only internal log-based metrics exist |
| CI/CD pipeline (tests, lint, build, deploy) | High | No automated testing at all |
| Unit and integration tests | High | Zero test coverage |
| Docker Compose healthchecks | Medium | See R4 |
| Reverse proxy / API gateway (nginx, Traefik) | Medium | API exposed directly |
| Secrets management | Medium | Plain `.env` file with passwords |
| Centralized logging (Loki/ELK) | Medium | Logs only in container stdout |
| Container image caching / multi-stage builds | Low | Current builds are functional but slow |

### Security

| Feature | Priority | Notes |
|---|---|---|
| API authentication (JWT / API key) | Medium | Completely open |
| CORS configuration | Medium | No CORS headers; frontend will be blocked |
| Rate limiting | Low | No request throttling |
| Input sanitization on source_id path params | Low | Used directly in Redis keys and file paths |

---

## Development Roadmap

### Phase 1: Foundation Fixes (Week 1–2)

Focus: fix bugs, improve reliability of the existing pipeline.

- [ ] **B1.** Add `POSTGRES_HOST: postgres` to worker service in `docker-compose.yml`.
- [ ] **B2.** Create a shared Redis connection pool in the API service.
- [ ] **B4.** Add exponential backoff with jitter on Redis errors in the worker loop.
- [ ] **B5.** Decouple MJPEG publish from live-frame write path in worker.
- [ ] **B6.** Add SIGTERM signal handler in producer, worker, and API.
- [ ] **B8.** Add reconnection loop for livestream producer mode.
- [ ] **R4.** Add Docker Compose healthchecks for Redis, Postgres, and application services.
- [ ] **R6.** Split `ServiceConfig` into per-service config classes.

### Phase 2: Observability & Testing (Week 3–4)

Focus: make the system inspectable and add a safety net for further changes.

- [ ] Add unit tests for shared schemas, config loading, and core functions.
- [ ] Add integration tests for API endpoints (use `TestClient`).
- [ ] Add integration tests for worker publish paths (mock Redis/DB).
- [ ] **R5.** Switch to JSON-structured logging across all services.
- [ ] Add Prometheus metrics endpoint to API (`/metrics`).
- [ ] Instrument worker with Prometheus counters/histograms (inference latency, queue latency, throughput).
- [ ] Add a simple CI pipeline (GitHub Actions: lint, type-check, test).

### Phase 3: Scalability Improvements (Week 5–7)

Focus: remove the biggest throughput ceilings.

- [ ] **S1.** Migrate from Redis LIST to Redis Streams with consumer groups.
- [ ] **S3.** Add backpressure: producer monitors queue depth before publishing.
- [ ] **S5.** Batch PostgreSQL writes in the worker (accumulate N results, flush in one transaction).
- [ ] **S4 Phase 1.** Overlap I/O and inference in worker (prefetch next frame during inference).
- [ ] **S6.** Use shared Redis connection pool in API.
- [ ] **B7.** Add `capture_ts_us` to results table via Alembic migration.
- [ ] **R1.** Implement dead letter queue for failed jobs.
- [ ] **R3.** Add idempotent result writes (`ON CONFLICT DO NOTHING`).

### Phase 4: Live Features & Frontend (Week 8–10)

Focus: complete the live data path and build a usable UI.

- [ ] **B3.** Rewrite MJPEG streaming to use async Redis (`redis.asyncio`).
- [ ] **S7.** Implement server-side fan-out for MJPEG (one subscription per source, broadcast to N viewers).
- [ ] Implement `WS /ws/sources/{source_id}` with result/metric/heartbeat event types.
- [ ] Add CORS middleware to API for frontend consumption.
- [ ] Build a minimal web frontend:
  - Source selector sidebar.
  - Live MJPEG video panel.
  - Detection event feed.
  - KPI stats cards (FPS, latency, queue depth).
  - System health banner.
- [ ] Add source management endpoints (register source, update metadata, enable/disable).

### Phase 5: Advanced Platform (Week 11+)

Focus: production-grade features and extended capabilities.

- [ ] Multi-model support: model registry, per-source model assignment.
- [ ] Job lifecycle state machine with DB-tracked transitions.
- [ ] Data retention policies (auto-archive/delete old results).
- [ ] Object storage (MinIO) for frame blobs instead of Redis.
- [ ] Batch inference (YOLO batch > 1) for GPU-efficient processing.
- [ ] API authentication (JWT or API key).
- [ ] Reverse proxy with TLS (Traefik or nginx).
- [ ] Horizontal worker scaling with consumer group partition assignment.

---

## Creative Feature Ideas

These are speculative features that would make the platform more interesting as a learning project and demonstrate advanced capabilities.

### 1. Real-Time Anomaly Detection & Alerting

Build an anomaly detector on top of the detection stream. Track per-source baselines (e.g., "this camera typically sees 2–5 cars") and fire alerts when counts deviate significantly. Implement via:
- Rolling statistics per source stored in Redis.
- An alert service that subscribes to results and evaluates rules.
- Webhook/email notifications on trigger.

**Learning value:** Time-series analysis, pub/sub event processing, alerting patterns.

### 2. Multi-Model A/B Testing

Run two different YOLO model variants (or YOLO vs. a different architecture) on the same frames and compare results side-by-side. Store results tagged by model version and build a comparison dashboard.

**Learning value:** Model evaluation, experiment tracking, dataset versioning concepts.

### 3. Cross-Camera Object Re-Identification

When the same object (e.g., a car) appears in multiple camera feeds, link detections across sources using appearance features (embeddings). Build a simple re-ID pipeline:
- Extract crop embeddings from detections.
- Compare embeddings across sources using cosine similarity.
- Display cross-camera tracks on a timeline.

**Learning value:** Embedding models, vector similarity, multi-source correlation.

### 4. Heatmap & Flow Analysis

Aggregate detection bounding box centroids over time to generate spatial heatmaps per source. Overlay on the camera frame to show high-activity zones. Extend with optical flow vectors to show movement direction patterns.

**Learning value:** Spatial aggregation, data visualization, OpenCV optical flow.

### 5. Historical Replay with New Models

Allow users to select a time range and re-run inference on historical frames using a different/updated model. Compare old vs new results for the same frames.

**Learning value:** Data pipeline replay, model versioning, idempotent processing.

### 6. Edge-Cloud Hybrid Deployment

Package the worker for Jetson Nano or Raspberry Pi. Edge workers handle inference locally and send only lightweight result metadata to the central API. Frames stay at the edge. The central API becomes a coordination/aggregation hub.

**Learning value:** Edge computing, bandwidth optimization, heterogeneous deployment.

### 7. Detection-Triggered Video Clip Extraction

When a high-confidence detection occurs, automatically extract a short video clip (e.g., 5 seconds before/after) from the buffered frames and save it as an MP4 file or push it to object storage.

**Learning value:** Ring buffer management, video encoding (FFmpeg), event-driven workflows.

### 8. Natural Language Query Interface

Add an endpoint that accepts natural language queries like "show me all cars detected on camera 1 in the last hour" and translates them to API filter parameters using an LLM or rule-based parser.

**Learning value:** LLM integration, query planning, API composition.

### 9. Interactive Annotation / Active Learning

Display uncertain detections (low confidence) to users via the frontend for manual labeling. Feed corrected labels back into a fine-tuning pipeline to improve the model on this specific deployment's domain.

**Learning value:** Active learning, human-in-the-loop ML, model fine-tuning.

### 10. Geo-Spatial Mapping Dashboard

If camera positions are known, project detections onto a 2D map (Leaflet/Mapbox). Show real-time dots for detected objects at their estimated GPS positions. Useful for traffic monitoring or security coverage visualization.

**Learning value:** Coordinate transforms, GIS integration, real-time map rendering.

---

## Summary

The current system is a well-structured foundation with clean service boundaries and good configuration hygiene. The main areas requiring attention are:

1. **Immediate bugs:** Worker missing Postgres host, API Redis connection leaks, MJPEG coupling issue.
2. **Reliability:** No retries, no dead letter queue, no graceful shutdown, no healthchecks.
3. **Scalability:** Single queue, full frames in Redis, no backpressure, single-threaded inference.
4. **Observability:** No metrics export, no structured logging, no test coverage.
5. **Missing features:** No frontend, no WebSocket, no auth, no CI/CD.

The phased roadmap above addresses these systematically, starting with correctness fixes and building toward a production-capable distributed inference platform. The creative feature ideas provide stretch goals that make this an excellent vehicle for learning distributed systems, ML deployment, and real-time data processing.
