# Worker Service

Consumes queued frame jobs, runs YOLO inference, and publishes structured results.

## Responsibilities

1. Load shared configuration.
2. Connect to Redis queue and preload model.
3. Decode `QueueJob` payloads.
4. Fetch frame bytes from Redis and decode to image.
5. Run preprocessing + inference + detection postprocessing.
6. Publish `InferenceResult` via configured results mode.
7. Optionally write latest annotated frame + metadata to Redis for API live bootstrap.
8. Delete frame blob from Redis after processing.
9. Emit periodic/final metrics logs.

## Processing Pipeline

1. `BRPOP` queue item.
2. Parse `QueueJob` using shared schema helpers.
3. `GET` frame blob by `frame_key`.
4. Decode image with OpenCV.
5. Run YOLO model.
6. Build shared `InferenceResult`.
7. Publish result:
- `local_jsonl` (implemented)
- `postgres` (implemented)
8. (Optional) Publish latest annotated frame and metadata to Redis:
- frame key: `<WORKER_LIVE_FRAME_KEY_PREFIX>:<source_id>`
- metadata key: `<WORKER_LIVE_META_KEY_PREFIX>:<source_id>`

## Results Modes

1. `local_jsonl`
- Appends one JSON object per line to daily file in `services/worker/output`.
- Optional annotated image snapshots.

2. `postgres`
- Intended to persist to shared DB tables:
  - `sources`
  - `jobs`
  - `results`
- Implemented with transactional source/job upsert + result insert.

## Live Frame Redis Publish

When enabled, worker writes the latest annotated JPEG and metadata per source to Redis.

- Enable toggle: `WORKER_LIVE_FRAMES_ENABLED=true`
- Cadence: `WORKER_LIVE_FRAMES_EVERY_N`
- Key prefixes:
  - `WORKER_LIVE_FRAME_KEY_PREFIX`
  - `WORKER_LIVE_META_KEY_PREFIX`
- TTL: `WORKER_LIVE_FRAME_TTL_SECONDS`
- JPEG quality: `WORKER_LIVE_FRAMES_JPEG_QUALITY`

This path is intended for live bootstrap/display and is not a durable storage system.

## MJPEG Pub/Sub (Worker Side)

When enabled, worker also publishes JPEG bytes to Redis Pub/Sub channels for
continuous streaming consumers:

- Toggle: `WORKER_MJPEG_PUBLISH_ENABLED`
- Per-source cap: `WORKER_MJPEG_MAX_FPS`
- Channel format: `<WORKER_MJPEG_CHANNEL_PREFIX>.<source_id>`

This is realtime fanout (ephemeral, best-effort), not a durable queue.

## Key Files

- `src/main.py`: consume loop/orchestration/metrics
- `src/inference.py`: model load and inference functions
- `src/results.py`: result publishing logic
- `src/db.py`: worker DB engine/session setup
- `Dockerfile`
- `requirements.txt`

## Configuration (Worker-specific)

- `WORKER_RESULTS_MODE`: `local_jsonl|postgres`
- `WORKER_RESULTS_DIR`
- `WORKER_SAVE_ANNOTATED`
- `WORKER_ANNOTATED_EVERY_N`
- `WORKER_LIVE_FRAMES_ENABLED`
- `WORKER_LIVE_FRAMES_EVERY_N`
- `WORKER_LIVE_FRAME_KEY_PREFIX`
- `WORKER_LIVE_META_KEY_PREFIX`
- `WORKER_LIVE_FRAME_TTL_SECONDS`
- `WORKER_LIVE_FRAMES_JPEG_QUALITY`
- `WORKER_MJPEG_PUBLISH_ENABLED`
- `WORKER_MJPEG_MAX_FPS`
- `WORKER_MJPEG_CHANNEL_PREFIX`
- `WORKER_MODEL_DEVICE`
- `WORKER_SUMMARY_LOG_INTERVAL_SECONDS`

Plus shared Redis and Postgres settings.

## Run

From project root:

```bash
docker compose up --build worker redis
```

## Fast Dev Mode

For faster iteration, use `worker-dev` (profile `dev`) from
`docker-compose.override.yml`.

It bind-mounts:

- `services/worker/src`
- `libs/platform_shared/src`

So source changes do not require rebuilding the worker image.

Start:

```bash
docker compose --profile dev up -d worker-dev redis postgres
```

After code edits:

```bash
docker compose restart worker-dev
```

Scale:

```bash
docker compose --profile dev up -d --scale worker-dev=3 worker-dev
```

Rebuild is still required when changing:

- `services/worker/requirements.txt`
- `services/worker/Dockerfile`
- base system package dependencies

## Current Tradeoffs

1. Single-process worker loop.
- Tradeoff: easy to reason about; lower throughput than parallel/batched workers.

2. Per-frame synchronous inference path.
- Tradeoff: simple correctness, less efficient at high volume.

3. Redis frame blob cleanup after publish step.
- Tradeoff: reduces memory use, but retry/recovery semantics need explicit policy.

4. Dual output modes (`jsonl`, `postgres`) during transition.
- Tradeoff: safer migration path, temporary complexity increase.

## Near-term Improvements

1. Add robust retry/idempotency policy for DB write failures.
2. Add source-scoped WebSocket event publish path for non-image live metrics/events.
3. Add richer job lifecycle transitions (`queued|processing|done|failed`).
