# Benchmark Suite

This directory is the new home for the platform benchmark suite.

The goal is to replace ad hoc audit scripts with a repeatable, versioned
benchmark contract that can be compared over time as the platform changes.

## Scope

This directory now includes:

1. A top-level benchmark CLI entry point
2. A canonical benchmark result schema
3. Executable baseline and paced step-test profiles
4. A compare command for before/after analysis
5. Basic preflight checks for common benchmark setup issues

The remaining future work is to add `soak` and richer report/classification
logic on top of the same contract.

## Design Principles

- Benchmark results must be comparable over time.
- Stable capacity tests and destructive stress tests are different profiles.
- A benchmark artifact must capture inputs, thresholds, environment, and
  summary metrics in one document.
- Invalid runs must be represented explicitly, not silently accepted.

## Directory Layout

```text
benchmarks/
  __main__.py
  cli.py
  schema.py
  SCHEMA.md
  schemas/
    benchmark_run.schema.json
```

## CLI

Use the package entry point from the repo root:

```bash
python -m benchmarks --help
```

Current commands:

- `plan`
  - Creates a benchmark manifest in `audits/benchmarks/`.
  - Use this when defining an upcoming benchmark run and capturing its inputs.
- `baseline`
  - Runs a low-load baseline against the API and Prometheus endpoints.
- `worker-step`
  - Runs a paced worker-capacity sweep by injecting Redis jobs at configured FPS levels.
- `api-step`
  - Runs a paced API concurrency sweep across read-only endpoints.
- `compare`
  - Compares two benchmark result files and prints shared metric deltas.
- `preflight`
  - Runs basic environment checks without executing a benchmark.
- `validate`
  - Validates an existing benchmark JSON document against the canonical schema.
- `schema`
  - Prints schema metadata or the JSON schema itself.

### Examples

Create a baseline benchmark manifest:

```bash
python -m benchmarks plan --profile baseline --label current-baseline
```

Run a real baseline benchmark:

```bash
python -m benchmarks baseline --label current-baseline
```

Run a worker step test:

```bash
python -m benchmarks worker-step --label worker-sweep --threads 2 --fps-steps 10,20,30,40 --step-duration 60 --stop-on-saturation
```

Note:
`worker-step` injects jobs directly into Redis and therefore requires the
Python `redis` package to be installed in the environment running the CLI.

Run an API step test:

```bash
python -m benchmarks api-step --label api-sweep --concurrency-steps 1,2,4,8,12,16 --step-duration 60 --stop-on-saturation
```

Compare two results:

```bash
python -m benchmarks compare old.json new.json
```

Run preflight checks before a worker benchmark:

```bash
python -m benchmarks preflight --profile worker-step
```

Create a worker step-test manifest with explicit thresholds:

```bash
python -m benchmarks plan --profile worker-step --label worker-capacity-sweep --max-failure-rate 0.01 --max-queue-depth-growth 0 --max-latency-p95-ms 75
```

Validate a benchmark result document:

```bash
python -m benchmarks validate audits/benchmarks/bench_123456789abc.json
```

Show the JSON schema:

```bash
python -m benchmarks schema --format json
```

## Benchmark Profiles

These profile names are reserved in schema version 1:

- `baseline`
- `worker-step`
- `api-step`
- `soak`
- `stress-destructive`

The intent is:

- `baseline`: low-load snapshot
- `worker-step`: paced worker-capacity sweep
- `api-step`: paced API concurrency sweep
- `soak`: long-running stability check below the saturation point
- `stress-destructive`: intentionally overload the system to observe failure mode

Currently implemented:

- `baseline`
- `worker-step`
- `api-step`

## Interpreting `worker-step`

`worker-step` is a step-based benchmark, not a precise capacity estimator.

That means:

- `best_stable_total_input_fps` is the highest tested step that stayed within
  thresholds
- it is not an interpolated maximum
- small run-to-run variation near the edge can move this value by a full step

Example:

- one run may report `30 fps`
- another may report `40 fps`

Even if actual observed worker throughput only changed slightly.

For worker-step comparisons, read these metrics together:

- `highest_stable_tested_input_fps`
- `first_saturated_tested_input_fps`
- `final_worker_processed_rate`
- `final_queue_growth`

This gives a better picture than treating `best_stable_total_input_fps` as a
single exact performance number.

## Preflight Checks

The suite now runs a few basic automatic preflight checks before real benchmark
execution.

Current checks:

- `baseline`
  - API `/health/ready`
  - Prometheus `/-/ready`
- `api-step`
  - API `/health/ready`
  - Prometheus `/-/ready`
- `worker-step`
  - Prometheus `/-/ready`
  - Redis connectivity
  - Redis queue length for the target queue must be `0`
  - existing `frame:bench:*` keys must be absent

If preflight fails:

- the benchmark writes an artifact
- the run is classified as `invalid`
- the preflight errors are recorded in the result document

Use the standalone command when you want to validate the environment before
starting a full run:

```bash
python -m benchmarks preflight --profile worker-step --queue-name video_stream
```

## Result Lifecycle

The canonical benchmark document includes a `status` field:

- `planned`
- `running`
- `completed`
- `failed`
- `aborted`

And a separate summary classification:

- `unknown`
- `stable`
- `degrading`
- `saturated`
- `invalid`

That separation is deliberate. A run can complete successfully as a process but
still be classified as `invalid` or `saturated`.

## Future Implementation Work

The next steps for this package are:

1. Implement `soak`
2. Add richer compare/report views
3. Refine threshold-based automatic classification logic
4. Add optional chart/table output for step benchmarks
