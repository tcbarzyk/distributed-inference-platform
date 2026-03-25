# Benchmark Result Schema

Schema version: `1`

Canonical JSON schema file:

- `benchmarks/schemas/benchmark_run.schema.json`

This document explains the intent of each field in the benchmark result
contract.

## Top-Level Fields

### `schema_version`

Integer schema version for the benchmark document. This must be incremented
when the document contract changes incompatibly.

### `run_id`

Unique identifier for a benchmark run or benchmark manifest. The value is
intended to be stable for the life of the run artifact.

### `profile`

Benchmark profile name. Version 1 reserves:

- `baseline`
- `worker-step`
- `api-step`
- `soak`
- `stress-destructive`

### `status`

Execution status of the run:

- `planned`
- `running`
- `completed`
- `failed`
- `aborted`

### `started_at_utc`

UTC ISO 8601 timestamp for when the run document began.

### `finished_at_utc`

UTC ISO 8601 timestamp for when the run completed or terminated. This is
optional for planned documents.

### `label`

Optional human-friendly label such as `baseline-before-indexes`.

## Metadata Blocks

### `git`

Repository metadata captured with the benchmark:

- `commit`
- `branch`
- `dirty`

This allows comparison tooling to connect benchmark outcomes to source state.

### `environment`

Runtime metadata for the host that executed the benchmark:

- `hostname`
- `platform`
- `python_version`
- `cwd`

## Run Definition Blocks

### `inputs`

Profile-specific inputs. These are intentionally flexible and may include:

- service URLs
- Redis host/port
- target FPS
- concurrency
- durations

Inputs should describe what the benchmark tried to do, not only what it
observed.

### `thresholds`

Optional benchmark limits used for classification. Version 1 includes:

- `max_failure_rate`
- `max_queue_depth_growth`
- `max_latency_p95_ms`
- `max_latency_p99_ms`

Thresholds encode the boundary between `stable` and `degrading` behavior.

## Outcome Blocks

### `summary`

Human-oriented summary of the benchmark. Fields:

- `duration_s`
- `classification`
- `success`
- `headline`
- `primary_metrics`

`primary_metrics` is a compact summary dictionary intended for dashboards and
quick comparisons.

For `worker-step` runs, `best_stable_total_input_fps` is intentionally
step-based. It means "highest tested step that remained stable", not "precise
estimated maximum worker capacity".

### `measurements`

Detailed raw or derived benchmark measurements. This is left open-ended on
purpose so later profiles can store:

- interval samples
- Prometheus query results
- endpoint latency tables
- queue slope calculations
- throughput deltas

### `verdicts`

Short machine-friendly or operator-friendly conclusions about the run, for
example:

- `queue_depth_stable`
- `failure_rate_exceeded`
- `api_tail_latency_exceeded`

### `errors`

Fatal or significant errors encountered during the run.

### `notes`

Free-form operator notes. This is where known caveats belong, such as:

- producer intentionally disabled
- worker-dev profile used
- MJPEG tests skipped

### `artifacts`

Paths to related files:

- `raw_outputs`
- `related_audits`
- `notes_path`

### `comparisons`

Optional comparison data to a prior baseline or peer run.

This field is reserved for later compare/report tooling.

The current `compare` command reads this document format and computes deltas
from `summary.primary_metrics`. That means benchmark profiles should always put
their headline comparison numbers into `summary.primary_metrics`, even when
they also store richer data under `measurements`.

## Classification Semantics

Version 1 uses these classifications:

- `unknown`
  - The run has not yet been classified.
- `stable`
  - The system stayed within thresholds.
- `degrading`
  - The system was still functioning but crossed warning thresholds.
- `saturated`
  - The system hit a benchmark limit and is no longer at useful steady state.
- `invalid`
  - The benchmark artifact exists, but the run is not trustworthy.

## Validation Approach

There are two validation layers in phase 1:

1. The canonical JSON schema document
2. A lightweight Python validator in `benchmarks/schema.py`

The Python validator is intentionally dependency-free so the benchmark tooling
can run in the existing environment without bringing in a schema-validation
package.
