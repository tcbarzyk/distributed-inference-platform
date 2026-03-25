# Worker Performance Improvement Areas

Date: 2026-03-25

This document summarizes the worker throughput investigation, what was
implemented, what the benchmark data showed, and the design decisions made
during the first optimization pass.

## Benchmark Baseline

Key benchmark artifacts:

- baseline reference: [`v0/bench_94eb060b872f.json`](/C:/Users/tcbar/Desktop/ds/platform/audits/benchmarks/worker/v0/bench_94eb060b872f.json)
  - `final_worker_processed_rate = 39.44066625450049`
- later improved run: [`v1/bench_7e64c6bd8dc0.json`](/C:/Users/tcbar/Desktop/ds/platform/audits/benchmarks/worker/v1/bench_7e64c6bd8dc0.json)
  - `final_worker_processed_rate = 42.67660696426948`
- later improved run: [`v1/bench_e91aacd3a3da.json`](/C:/Users/tcbar/Desktop/ds/platform/audits/benchmarks/worker/v1/bench_e91aacd3a3da.json)
  - `final_worker_processed_rate = 44.9305404029384`
- latest session run: [`v1/bench_fc895e68df71.json`](/C:/Users/tcbar/Desktop/ds/platform/audits/benchmarks/worker/v1/bench_fc895e68df71.json)
  - `final_worker_processed_rate = 49.65996290504419`

Observed benchmark picture:

- `20 fps` total input is stable
- `30 fps` total input is stable
- `40 fps` total input is stable
- `50 fps` total input saturates due to queue growth
- worker failures are near zero in the synthetic benchmark path

Interpretation:

- correctness failure is not the primary limiter
- the main ceiling is serial throughput
- queue growth appears before failure rates spike

## Initial Improvement Hypotheses

1. Reduce per-frame hot-path overhead
- trim avoidable object creation, logging work, and redundant round trips
- expected payoff: low to moderate

2. Overlap I/O with inference
- prefetch or pipeline next-frame work while current inference/publish runs
- expected payoff: moderate to high

3. Decouple result publishing / DB writes
- move durable publish work off the main inference loop
- expected payoff: moderate to high

4. Batch inference
- only after simpler structural wins are measured
- expected payoff: potentially high, but higher complexity/risk

5. Improve queue / TTL robustness near saturation
- mostly reliability and benchmark quality near the limit

Recommended order:

1. reduce obvious hot-path overhead
2. overlap or decouple blocking serial work
3. revisit TTL/backlog behavior
4. consider batching only after simpler wins are exhausted

## Prometheus Queries

Use these in Prometheus at `http://localhost:9090`.

List active worker stages:

```promql
dip_worker_stage_duration_ms_count
```

Average per-stage duration:

```promql
sum by (stage) (rate(dip_worker_stage_duration_ms_sum[1m]))
/
sum by (stage) (rate(dip_worker_stage_duration_ms_count[1m]))
```

Per-stage p50:

```promql
histogram_quantile(0.50, sum by (le, stage) (rate(dip_worker_stage_duration_ms_bucket[5m])))
```

Per-stage p95:

```promql
histogram_quantile(0.95, sum by (le, stage) (rate(dip_worker_stage_duration_ms_bucket[5m])))
```

Reference worker queries:

```promql
histogram_quantile(0.95, rate(dip_worker_inference_duration_ms_bucket[5m]))
```

```promql
histogram_quantile(0.95, rate(dip_worker_queue_latency_ms_bucket[5m]))
```

```promql
dip_worker_queue_depth
```

Interpretation notes:

- if `publish_result` is materially above fetch/decode/delete, durable result
  persistence is the first non-inference target
- if `publish_live_frame` and `publish_mjpeg_internal` are both non-trivial,
  JPEG drawing/encoding is probably duplicated
- if queue latency rises while stage timings stay flat, the worker is saturating
  from aggregate serial throughput rather than a new failure mode

## Implemented In This Session

### 1. Instrumentation

Added `dip_worker_stage_duration_ms` plus stage-level timing around:

- Redis frame fetch
- image decode
- result-model build
- queue-depth sampling
- live-frame plan build
- live-frame write
- MJPEG publish
- Redis frame delete
- publish enqueue wait
- shared JPEG encode
- internal JSONL / Postgres publish sub-stages

This made it possible to separate DB/file work from the coarse `publish_result`
bucket.

### 2. Hot-Path Cleanup

Implemented:

- removed per-frame `LLEN`; queue depth is now sampled periodically
- gated per-frame debug log construction behind debug-enabled checks
- reused one detection-dict payload across result persistence, live-frame
  rendering, and MJPEG rendering
- skipped unnecessary live-frame metadata work when the feature is disabled

Result:

- this cleanup improved measurement clarity but produced little to no measurable
  throughput gain by itself

### 3. Duplicate JPEG Removal

Findings:

- `publish_live_frame` and `publish_mjpeg_internal` were both spending time on
  drawing/encoding the same annotated JPEG

Implemented:

- added one shared per-frame JPEG cache for the foreground path

Measured effect:

- `publish_live_frame` p50 dropped from roughly `3.0 ms` to roughly `0.5 ms`
- `publish_mjpeg_internal` p50 dropped from roughly `3.0 ms` to roughly `0.5 ms`
- `publish_result` p50 dropped from roughly `10.9 ms` to roughly `7.9 ms`

### 4. DB Lookup / Update Reduction

Implemented in Postgres mode:

- worker-local `source_id` cache
- replaced ORM read-before-write with Postgres upserts
- reduced `jobs` persistence to minimal `ON CONFLICT DO NOTHING` inserts

Important schema constraint:

- `results.job_id` still has a foreign key to `jobs.job_id`
- because of that, `jobs` writes could be minimized but not removed entirely
  without a schema change

Measured effect:

- `publish_db_upsert_source_ms` dropped from roughly `3.0 ms` to roughly `0.5 ms`
- `publish_db_upsert_job_ms` remained about `3.0 ms`
- `publish_db_commit_ms` remained about `3.0 ms`

Interpretation:

- the main remaining DB cost was transaction / commit path, not Python-side
  query overhead alone

### 5. Concurrent Durable Result Writes

Implemented:

- one bounded in-process publish queue
- one background publish thread inside the existing worker process
- main thread now enqueues JSONL / Postgres result persistence work and
  continues processing frames
- live-frame Redis writes and MJPEG Pub/Sub remain on the foreground path
- graceful shutdown drains the publish queue before exit

Measured effect:

- this produced the largest single improvement in the session, reported at
  roughly `+5 fps`
- relative to the baseline reference run, total improvement reached roughly
  `+10.22 fps` using:
  - baseline: `39.44066625450049`
  - latest: `49.65996290504419`

Interpretation:

- small serial cleanup was not the primary lever
- duplicate image work was real overhead
- asynchronous durable publishing was the first clearly significant structural
  gain

## Current Worker Structure

Foreground thread:

1. `BRPOP` queue job
2. fetch frame bytes from Redis
3. decode image
4. run inference
5. build `InferenceResult`
6. enqueue durable publish task
7. write latest live-frame Redis keys when enabled
8. publish MJPEG bytes when enabled
9. delete frame blob

Background publish thread:

1. receive queued publish task
2. write JSONL or Postgres result
3. record publish sub-stage timings
4. log and track async publish failures

## Design Decisions And Tradeoffs

1. Keep concurrency inside the existing worker process
- chosen: one extra thread plus one internal queue
- not chosen: separate DB-writer service
- rationale: lower operational complexity, easier rollback, faster iteration

2. Decouple durable publishing before changing live streaming architecture
- chosen: move JSONL / Postgres writes off the main thread first
- kept: Redis latest-frame writes and MJPEG Pub/Sub on the foreground path
- rationale: benchmark data showed durable result persistence as the stronger
  bottleneck, and live streaming may change later

3. Do not over-invest in the current live streaming mechanism
- treated live-frame Redis keys and MJPEG Pub/Sub as separate from durable
  result persistence
- avoided building a larger streaming abstraction at this stage
- rationale: preserve flexibility for a future streaming redesign

4. Preserve schema compatibility while trimming DB overhead
- chosen: keep minimal `jobs` inserts to satisfy FK integrity
- not chosen: remove `jobs` writes entirely
- rationale: low-risk optimization without a migration

5. Instrument first, then restructure
- chosen: add stage timing before making larger design changes
- rationale: reduce guesswork and verify each change against actual bottlenecks

## Follow-Up Hardening Items

Deferred intentionally:

- add explicit metrics for internal publish queue depth / saturation
- add clearer backpressure reporting when publish enqueue blocks
- add dedicated tests for async publish queue and shutdown/drain behavior
- revisit whether live-frame or MJPEG should also move off the foreground path

## Non-Immediate Targets

These remain valid future improvements, but they were not the best first moves
for this benchmark path:

- Redis Streams migration
- async MJPEG streaming redesign
- `capture_ts_us` DB persistence
- dead letter queue / idempotent result writes
- broader platform redesign
