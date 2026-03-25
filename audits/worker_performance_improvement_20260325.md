# Worker Performance Improvement Areas

Date: 2026-03-25

This note summarizes the most plausible worker performance improvement areas
based on the current synthetic `worker-step` benchmark results.

## Current Benchmark Picture

Recent worker-step benchmarks indicate:

- `20 fps` total input is stable
- `30 fps` total input is stable
- `40 fps` total input is stable
- `50 fps` total input saturates due to queue growth
- worker failures are near zero in the current synthetic benchmark path

Interpretation:

- the worker is not primarily failing correctness checks under load
- the current ceiling is more likely caused by serial throughput limits
- queue growth appears before failure rates spike

## Likely Improvement Areas

### 1. Reduce Per-Frame Hot-Path Overhead

The worker currently handles each frame in a strict serial path:

- pop job
- fetch frame bytes
- decode image
- run inference
- publish result
- delete frame key

This makes every per-frame overhead additive.

Potential improvements:

- reduce avoidable object creation in the hot path
- trim logging or expensive formatting in non-debug paths
- reduce repeated Redis/DB round trips where possible

Expected payoff:

- low to moderate
- likely easiest short-term win

### 2. Overlap I/O With Inference

The current worker loop is fundamentally single-frame and sequential.

Potential improvements:

- prefetch the next job while current inference is running
- fetch/decode the next frame before the current publish path completes
- decouple slower I/O from the main inference loop

Expected payoff:

- moderate to high
- likely one of the best first structural improvements

### 3. Decouple Result Publishing / DB Writes

If database writes or publish-side work are part of the bottleneck, they should
not fully block the next inference cycle.

Potential improvements:

- push publish work onto an internal queue
- use a background writer
- batch writes when safe

Expected payoff:

- moderate to high
- especially valuable if inference is not actually the dominant cost

### 4. Batch Inference

The worker currently processes one frame at a time.

Potential improvements:

- accumulate small batches
- run model inference with batch size greater than 1
- publish results after batch inference completes

Expected payoff:

- potentially high
- higher implementation risk and more complexity than the earlier items

### 5. Improve Queue/TTL Robustness Near Saturation

This is not the main throughput improvement area, but it matters near the edge.

Potential improvements:

- increase or adapt TTL under backlog
- refresh TTL when the worker pops a job
- detect and classify expired-frame conditions more explicitly

Expected payoff:

- mostly reliability and benchmark quality
- helps reduce misleading degradation near the saturation boundary

## Recommended Order

Suggested implementation order:

1. reduce obvious hot-path overhead
2. overlap I/O with inference
3. decouple or batch publish/DB work
4. revisit TTL/backlog handling
5. explore batch inference only after the simpler wins are measured

## Expected Improvement Range

Reasonable expectations from the current baseline:

- simple cleanup only: roughly `5-15%`
- a good first structural improvement: roughly `15-35%`

That suggests the current stable edge around `40 fps` may be movable into the
`45-55 fps` range without a major architecture rewrite, assuming the bottleneck
is mostly serial overhead and not purely model compute.

## Non-Immediate Targets

These are valid future improvements, but they are not the best first moves for
synthetic worker throughput:

- Redis Streams migration
- async MJPEG streaming
- `capture_ts_us` DB persistence
- dead letter queue / idempotent result writes
- broader platform redesign

The current benchmark data supports focusing on the worker hot path first.
