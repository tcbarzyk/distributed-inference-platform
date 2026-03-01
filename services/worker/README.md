# Worker Service

The worker consumes frame jobs from a Redis queue, runs YOLOv8 object detection on each frame, and publishes structured results.

## How It Works

1. **Load configuration** from environment variables (via `.env` or Docker Compose).
2. **Connect to Redis** and **preload the YOLOv8n model** to avoid first-frame latency.
3. **Block-pop jobs** from the Redis queue (`BRPOP`).
4. **For each job:**
   - Parse the JSON metadata (frame ID, source ID, frame key, timestamps).
   - `GET` the JPEG-encoded frame bytes from Redis.
   - Decode the image and compute queue/source latency.
   - **Preprocess** → resize to 640×640, convert grayscale/BGRA to BGR.
   - **Infer** → run YOLOv8n forward pass.
   - **Postprocess** → extract bounding boxes, class labels, and confidence scores.
   - **Publish results** → append a JSON line to a daily results file; optionally save an annotated image with drawn bounding boxes.
   - **Delete** the frame blob from Redis to free memory.
5. **Log periodic and final run summaries** (throughput, latency, error counts).

## Source Layout

```
services/worker/
├── src/
│   ├── main.py         # Entry point — job loop, decode, orchestration
│   ├── inference.py    # Model loading, preprocessing, YOLOv8 inference, postprocessing
│   └── results.py      # JSONL result writing and annotated image saving
├── output/             # Default results directory (mounted as a volume in Docker)
│   ├── results-YYYYMMDD.jsonl
│   └── annotated/      # Optional annotated frame images
├── yolov8n.pt          # Pretrained model weights
├── Dockerfile
└── requirements.txt
```

## Result Format

Each processed frame appends one JSON line to `output/results-YYYYMMDD.jsonl`:

```json
{
  "processed_at_us": 1740000000000000,
  "frame_id": 42,
  "source_id": "KAB_SK_1_undist",
  "status": "ok",
  "model": "yolov8n.pt",
  "inference_ms": 12.34,
  "pipeline_ms": 18.56,
  "detections": [
    {
      "label": "car",
      "class_id": 2,
      "confidence": 0.91,
      "bbox_xyxy": [120.5, 80.0, 340.25, 260.75]
    }
  ]
}
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
| `WORKER_RESULTS_MODE` | `local_jsonl` | Output mode (currently only `local_jsonl`) |
| `WORKER_RESULTS_DIR` | `services/worker/output` | Directory for result files |
| `WORKER_SAVE_ANNOTATED` | `false` | Save images with drawn bounding boxes |
| `WORKER_ANNOTATED_EVERY_N` | `30` | Save an annotated image every N frames |
| `WORKER_SUMMARY_LOG_INTERVAL_SECONDS` | `30` | Seconds between periodic summary logs |

## Running

### Locally (with a virtual environment)

```bash
pip install -r requirements.txt
pip install -e ../../libs/platform_shared
python src/main.py
```

### With Docker Compose (from the `platform/` root)

```bash
docker compose up --build worker
```

The output directory is mounted as a volume so results persist on the host. The Ultralytics model cache is also mounted to avoid re-downloading weights on each container restart.