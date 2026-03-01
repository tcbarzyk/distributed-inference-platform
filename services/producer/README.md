# Producer Service

The producer reads video frames from a configured source and publishes them to Redis for downstream worker processing.

## How It Works

1. **Load configuration** from environment variables (via `.env` or Docker Compose).
2. **Connect to Redis.**
3. **Discover or capture frames** depending on source mode:
   - **`sample_files`** — scans disk directories for `.bmp` frames, parses capture timestamps from filenames, and merges all cameras into a single sorted timeline.
   - **`livestream`** — reads frames continuously from a live video source (RTSP, RTMP, webcam device, etc.).
4. **For each frame:**
   - JPEG-encode the image.
   - `SET` the encoded bytes into Redis with a configurable TTL.
   - `LPUSH` a lightweight JSON metadata job onto the work queue.
5. **Log periodic and final run summaries** (throughput, error counts, latency).

## Source Layout

```
services/producer/
├── src/
│   ├── main.py              # Entry point — orchestrates the publish loop
│   └── frame_discovery.py   # Disk-frame scanning and timestamp parsing
├── sample-video/            # Sample dataset (mounted read-only in Docker)
├── Dockerfile
└── requirements.txt
```

## Configuration

All settings are loaded by `platform_shared.config`. Required variables are marked with **\***.

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST` **\*** | — | Redis hostname |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis database index |
| `REDIS_PASSWORD` | `None` | Redis password (optional) |
| `QUEUE_NAME` **\*** | — | Redis list used as the job queue |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `PRODUCER_SOURCE_MODE` | `sample_files` | `sample_files` or `livestream` |
| `PRODUCER_SAMPLE_ROOT` | `services/producer/sample-video/…` | Root directory containing camera subdirectories |
| `PRODUCER_CAMERA_DIRS` | `KAB_SK_1_undist,KAB_SK_4_undist` | Comma-separated camera directory names |
| `PRODUCER_FILE_GLOB` | `*.bmp` | Glob pattern for frame files |
| `PRODUCER_REPLAY_MODE` | `realtime` | `realtime`, `fixed`, or `max` |
| `PRODUCER_FIXED_FPS` | `25` | Target FPS when replay mode is `fixed` |
| `PRODUCER_FRAME_ENCODING` | `jpeg` | Encoding format for frame bytes |
| `PRODUCER_JPEG_QUALITY` | `80` | JPEG quality (1–100) |
| `PRODUCER_FRAME_TTL_SECONDS` | `60` | TTL for frame blobs stored in Redis |
| `PRODUCER_FRAME_KEY_PREFIX` | `frame` | Key prefix for Redis frame entries |
| `PRODUCER_STREAM_URL` | `None` | Video source URL (required for `livestream` mode) |
| `PRODUCER_STREAM_SOURCE_ID` | `stream-1` | Source identifier for livestream frames |
| `PRODUCER_SUMMARY_LOG_INTERVAL_SECONDS` | `30` | Seconds between periodic summary logs |

## Running

### Locally (with a virtual environment)

```bash
pip install -r requirements.txt
pip install -e ../../libs/platform_shared
python src/main.py
```

### With Docker Compose (from the `platform/` root)

```bash
docker compose up --build producer
```

The sample-video directory is mounted read-only into the container at `/app/services/producer/sample-video`.