# Producer Service

Publishes video frames into Redis for downstream worker inference.

## Responsibilities

1. Load shared configuration from `.env`.
2. Read frames from one of two source modes:
- `sample_files`
- `livestream`
3. Encode frames (JPEG currently).
4. Store frame bytes in Redis with TTL.
5. Push `QueueJob` payloads to the Redis queue.
6. Emit periodic and final summary logs.

## Input Modes

1. `sample_files`
- Scans configured camera directories.
- Parses capture timestamps from filenames.
- Merges frames into a timestamp-sorted timeline.

2. `livestream`
- Reads continuously from stream URL/device via OpenCV.
- Generates capture timestamps at ingest time.

## Queue Payload

Producer publishes shared schema `QueueJob`:

- `schema_version`
- `job_id`
- `frame_id`
- `source_id`
- `capture_ts_us`
- `enqueued_at_us`
- `frame_key`
- `attempt`

## Redis Usage

1. Frame blob key: `<PRODUCER_FRAME_KEY_PREFIX>:<source_id>:<frame_id>`
2. Queue list: `QUEUE_NAME`
3. Blob TTL: `PRODUCER_FRAME_TTL_SECONDS`

## Key Files

- `src/main.py`: main publish loop and mode dispatch
- `src/frame_discovery.py`: file discovery/timestamp parsing
- `Dockerfile`
- `requirements.txt`

## Configuration (Producer-specific)

- `PRODUCER_SOURCE_MODE`: `sample_files` or `livestream`
- `PRODUCER_SAMPLE_ROOT`
- `PRODUCER_CAMERA_DIRS`
- `PRODUCER_FILE_GLOB`
- `PRODUCER_REPLAY_MODE`: `realtime|fixed|max`
- `PRODUCER_FIXED_FPS`
- `PRODUCER_JPEG_QUALITY`
- `PRODUCER_FRAME_TTL_SECONDS`
- `PRODUCER_FRAME_KEY_PREFIX`
- `PRODUCER_STREAM_URL`
- `PRODUCER_STREAM_SOURCE_ID`

Also requires shared Redis/logging settings.

## Run

From project root:

```bash
docker compose up --build producer redis
```

## Current Tradeoffs

1. Stores full frame bytes in Redis for simplicity.
- Tradeoff: easy decoupling, but memory pressure grows with frame size/rate.

2. Uses Redis list queue semantics.
- Tradeoff: simple local setup, but stronger delivery guarantees require additional logic.

3. Single queue for all sources.
- Tradeoff: straightforward now, may need partitioning for scale.

## Near-term Improvements

1. Queue partitioning by source/camera.
2. Adaptive backpressure based on queue depth.
3. Better reconnect/retry behavior for livestream interruptions.
