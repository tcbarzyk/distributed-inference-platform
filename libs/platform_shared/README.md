# platform_shared

Shared Python package used by `producer`, `worker`, and `api` for contracts, configuration, and database primitives.

## What This Package Provides

### 1) Shared runtime config
- Module: `platform_shared.config`
- Loads `.env` once per service and validates required/optional settings.
- Provides a typed `ServiceConfig` dataclass so all services consume the same config contract.
- Centralizes validation for Redis, producer mode, worker mode, live frame settings, and API activity window.

### 2) Shared queue/result contracts
- Module: `platform_shared.schemas`
- Defines:
  - `QueueJob` (producer -> worker payload)
  - `Detection` (normalized detection record)
  - `InferenceResult` (worker -> sink/API payload shape)
- Includes strict serializers/deserializers with explicit validation errors to prevent schema drift.

### 3) Shared database base/models
- Modules: `platform_shared.db_base`, `platform_shared.models`
- Defines SQLAlchemy declarative `Base` and ORM models:
  - `SourceModel`
  - `JobModel`
  - `ResultModel`
- Keeps source/job/result relationships and field names consistent across services and migrations.

### 4) Shared DB connection utilities
- Module: `platform_shared.db_utils`
- Builds Postgres URL from environment variables.
- Exposes reusable SQLAlchemy engine/session factory helpers.

## Why It Exists

- Prevents duplicated schema/config code in each service.
- Reduces cross-service contract drift.
- Gives one place to evolve shared operational conventions (logging/metrics/tracing metadata).

## JSON Logging Design (Shared)

Goal: all services emit structured JSON logs with consistent keys so logs are queryable across producer/worker/api.

### Log envelope (required keys)
- `ts`: RFC3339 UTC timestamp
- `level`: `DEBUG|INFO|WARNING|ERROR|CRITICAL`
- `service`: `producer|worker|api`
- `event`: stable machine-readable event name (example: `worker.inference.completed`)
- `msg`: short human-readable message
- `env`: environment name (example: `dev`, `staging`, `prod`)

### Correlation keys (when available)
- `trace_id`: distributed trace id
- `span_id`: trace span id
- `request_id`: HTTP request correlation id (API)
- `job_id`: queue job identifier
- `source_id`: source/camera identifier
- `frame_id`: frame index/id

### Error keys (on failures)
- `error.type`: exception class
- `error.message`: exception message
- `error.stack`: stack trace (enabled for error logs)
- `error.retryable`: boolean

### Metric-like keys in logs (optional for quick debugging)
- `latency_ms.queue`
- `latency_ms.inference`
- `latency_ms.pipeline`
- `redis.queue_depth`
- `throughput_fps`

### Event taxonomy
- `producer.frame.published`
- `producer.frame.dropped`
- `worker.job.received`
- `worker.inference.completed`
- `worker.inference.failed`
- `worker.result.published`
- `api.request.completed`
- `api.dependency.readiness`

### Implementation notes
- Add a shared logger helper in `platform_shared` (for example `platform_shared.observability.logging`) that:
  - configures JSON formatter
  - injects baseline fields (`service`, `env`)
  - supports context binding (`job_id`, `source_id`, `request_id`)
- Keep event names stable. Dashboards and alerts should key off `event`, not free-text `msg`.

## Shared Metrics Design

Goal: unify service metrics naming/labels so baseline and improvement comparisons are straightforward.

### Naming convention
- Prefix: `dip_` (distributed inference platform)
- Pattern: `dip_<service>_<signal>_<unit>`
- Examples:
  - `dip_worker_jobs_processed_total`
  - `dip_worker_queue_latency_ms`
  - `dip_api_request_duration_ms`

### Core metric set

#### Producer
- Counter: `dip_producer_frames_published_total`
- Counter: `dip_producer_frames_dropped_total`
- Counter: `dip_producer_failures_total`
  - labels: `stage=encode|redis_set|queue_push|read`
- Histogram: `dip_producer_publish_latency_ms`
- Gauge: `dip_producer_last_capture_ts_us`

#### Worker
- Counter: `dip_worker_jobs_popped_total`
- Counter: `dip_worker_jobs_processed_total`
- Counter: `dip_worker_failures_total`
  - labels: `stage=decode|inference|publish|redis`
- Histogram: `dip_worker_queue_latency_ms`
- Histogram: `dip_worker_inference_duration_ms`
- Histogram: `dip_worker_pipeline_duration_ms`
- Gauge: `dip_worker_queue_depth`

#### API
- Counter: `dip_api_requests_total`
  - labels: `route`, `method`, `status_code`
- Histogram: `dip_api_request_duration_ms`
  - labels: `route`, `method`
- Counter: `dip_api_dependency_checks_total`
  - labels: `dependency=redis|postgres`, `status=ok|fail`
- Gauge: `dip_api_sources_active`

### Shared label policy
- Keep cardinality low.
- Allowed high-value labels:
  - `service`
  - `stage`
  - `route` (templated route only, never raw URL)
  - `method`
  - `status_code`
  - `result=ok|error`
- Avoid unbounded labels (`job_id`, `frame_id`, raw exception text).

### Histogram buckets (starting point)
- Queue/inference/pipeline latency ms: `[1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000]`
- API request duration ms: `[1, 5, 10, 25, 50, 100, 250, 500, 1000]`

### Recommended shared module additions
- `platform_shared.observability.metrics`
  - metric registry helpers
  - common metric names/constants
  - helper functions for safe label normalization
- `platform_shared.observability.logging`
  - JSON formatter + contextual logger wrappers

## Suggested Next Step

Implement `platform_shared.observability` with:
1. `init_json_logging(service_name: str, env: str, level: str)`.
2. `get_logger(name: str, **context)`.
3. shared Prometheus metric constructors and naming constants.

Then migrate each service to use these shared observability primitives.


## Future Improvements
Improve log sampling strategy - e.g. log every Nth frame at INFO, log all errors always. Without this, Loki/any log store gets hammered and the useful signal drowns in noise.