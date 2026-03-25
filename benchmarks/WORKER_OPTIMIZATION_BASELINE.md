# Worker Optimization Baseline

This document defines the benchmark protocol for worker-focused optimization
work.

The immediate goal is to optimize synthetic worker behavior first. That means
we are intentionally measuring the worker under benchmark-injected load rather
than full end-to-end producer realism.

## Scope

This baseline is for:

- worker throughput tuning
- worker reliability tuning
- worker queue/backlog behavior
- synthetic Redis-injected frame workloads

This baseline is not for:

- producer performance
- realistic camera replay behavior
- API throughput optimization
- full-system end-to-end benchmark comparisons

## Standardized Benchmark Profile

The benchmark command should stay fixed while worker optimizations are in
progress.

Recommended default profile:

```powershell
python -m benchmarks worker-step --label worker-sweep-mid --threads 2 --fps-steps 10,15,20,25 --step-duration 60 --stop-on-saturation
```

This yields total input rates of:

- `20 fps`
- `30 fps`
- `40 fps`
- `50 fps`

This is a good default because it:

- already proved useful in the current environment
- spans the current stable range and the likely saturation boundary
- is short enough to run frequently during iteration

### Recommended Profiles

Use one primary profile and keep the others as secondary validation tools.

#### Profile A: Primary Regression Benchmark

Use this for most changes.

```powershell
python -m benchmarks worker-step --label worker-sweep-mid --threads 2 --fps-steps 10,15,20,25 --step-duration 60 --stop-on-saturation
```

Purpose:

- fast repeatable benchmark
- detects shifts in stable capacity around the current limit

#### Profile B: Conservative Sanity Benchmark

Use this when debugging reliability issues or checking a fragile change.

```powershell
python -m benchmarks worker-step --label worker-sweep-low --threads 2 --fps-steps 5,10,15,20 --step-duration 60 --stop-on-saturation
```

Purpose:

- confirms low-to-mid load behavior
- useful when there is a risk of early saturation or setup issues

#### Profile C: Edge-Finding Benchmark

Use this after a clear improvement has already been proven by Profile A.

```powershell
python -m benchmarks worker-step --label worker-sweep-high --threads 2 --fps-steps 15,20,25,30 --step-duration 60 --stop-on-saturation
```

Purpose:

- pushes beyond the current edge
- helps measure whether the sustainable limit moved upward

## Standardized Environment

These environment details should stay fixed during synthetic worker
optimization.

### Worker Variant

Pick one and keep it fixed:

- recommended: `worker-dev`

Reason:

- faster iteration for code changes
- avoids mixing results across different container/runtime configurations

Do not switch between `worker` and `worker-dev` in the middle of a comparison
series.

### Running Services

Before each worker benchmark, the expected service set is:

- `redis`
- `worker-dev`
- `postgres`
- `api`
- `prometheus`

The producer should be:

- off

Reason:

- synthetic worker benchmarks should isolate worker behavior
- producer traffic adds a second workload and makes attribution weaker

### Worker Count

Only one worker service instance should be running.

Required:

- exactly one worker process consuming the queue

Reason:

- avoids mixing single-worker and multi-worker capacity numbers
- keeps benchmark results directly comparable across commits

### Redis Reset

Redis must be reset before every worker benchmark.

Recommended command:

```powershell
docker compose exec redis redis-cli FLUSHDB
```

Minimum checks before starting a run:

```powershell
docker compose exec redis redis-cli LLEN video_stream
```

Expected result:

- `0`

### Suggested Pre-Run Sequence

```powershell
docker compose stop producer
docker compose stop worker
docker compose stop worker-dev
docker compose exec redis redis-cli FLUSHDB
docker compose up -d worker-dev redis postgres api prometheus
```

Then verify:

- only `worker-dev` is running
- queue length is `0`
- Prometheus is up
- the benchmark Python environment is the intended one

## Metrics We Want To Improve

Synthetic worker optimization should focus on these metrics.

### Primary Metrics

These are the headline metrics for improvement.

- `best_stable_total_input_fps`
  - highest benchmark input rate that remains stable
  - this is step-based and should be treated as a tested threshold, not an exact capacity estimate
- `first_saturated_tested_input_fps`
  - first tested step that clearly crosses the benchmark stability threshold
- `worker_processed_rate`
  - recent successful processing rate from Prometheus
- `interval_throughput_fps`
  - recent worker log throughput over the summary interval
- `worker_queue_depth`
  - should remain bounded during stable steps

### Reliability Metrics

These should stay at or near zero.

- `worker_failures_rate`
- `blob_misses`
- `decode_failures`
- `inference_failures`
- `publish_failures`
- `redis_errors`

### Latency Metrics

These help detect regressions even when throughput improves.

- `worker_inference_p95_ms`
- `worker_pipeline_p95_ms`
- average queue latency in worker logs

### Secondary Operational Metrics

These are useful supporting signals.

- enqueue rate vs processed rate
- queue depth growth over the step
- MJPEG/live-frame counters, if relevant to a change

## Improvement Criteria

A worker change counts as an improvement when it increases sustainable worker
performance without reducing reliability.

Preferred outcomes:

- higher stable total input fps
- higher processed fps at the same benchmark load
- lower queue growth at the same benchmark load
- same or lower worker latency
- no increase in failure/blob-miss rates

Non-goals:

- increasing raw enqueue rate without increasing processed rate
- improving throughput at the cost of large queue buildup
- improving throughput while introducing failures

## Comparison Rules

When comparing two worker changes:

- use the same benchmark profile
- use the same worker variant
- use the same services running
- reset Redis before every run
- keep producer off
- do not compare runs from mixed environments

When reading worker-step comparisons:

- do not over-interpret a one-step change in `best_stable_total_input_fps`
- always compare it alongside `final_worker_processed_rate` and `final_queue_growth`
- near the performance edge, a small throughput change can flip a whole step outcome

## Current Baseline Understanding

As of the current benchmark series:

- `20 fps` total input is stable
- `30 fps` total input is stable
- `40 fps` total input is stable
- `50 fps` total input saturates due to queue growth

That means the current synthetic worker edge is approximately between:

- `40 fps`
- `50 fps`

This document should be updated only when the benchmark protocol changes
deliberately, not every time the worker improves.
