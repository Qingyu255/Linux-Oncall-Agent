# Day 1 execution status

Updated 2026-09-23. This records observed results, not intended architecture.

## Implemented

- Python 3.12 project with pinned dependency lock, Ruff, mypy, pytest, packaging and CI.
- Immutable typed CPU/process observations and evidence records.
- Deterministic `/proc` parsers with counter-reset, PID reuse and malformed-input handling.
- CPU interval sampling that separates shared-kernel counters from target-cgroup usage/quota.
- Bounded process ranking scoped to the target PID namespace.
- Investigation policy with deadline, call, concurrency and artifact-byte budgets.
- SQLite evidence repository, atomic hashed artifacts, scoped retrieval and crash recovery.
- Report validation for evidence ownership and boot identity.
- Authenticated target API, trusted broker, MCP tool server and operator CLI.
- Separate target, broker and DSH Docker images/networks with dropped capabilities, read-only
  filesystems, no Docker socket, no target shell API and no AWS credentials.
- DeepSeek Harness `0.1.5rc1` custom profile with the OpenAI-compatible provider adapter,
  MCP client, local sandbox shell and DeepSeek session-upload plugins disabled.
- Keyless OpenAI-wire fixture used only to test the real harness/tool lifecycle. It is explicitly
  not an LLM diagnosis or model evaluation.

## Verified

- 16 unit/security tests pass on macOS, including SQLite access from FastAPI worker threads.
- Ruff and strict mypy checks pass for the domain/application core.
- Local wheel and source distribution build successfully without isolated dependency resolution.
- `docker compose config` validates the target/broker/agent topology.
- Target and broker health checks pass in Docker Compose.
- DSH initializes its custom provider and MCP plugins, performs CPU and process calls, invokes its
  local sandbox shell, and submits a broker-validated report.
- The bounded fault run `bc005286bf8e41dc9ec58528d21b1708` measured 1.007 cgroup CPU cores
  against a 1.0-core quota, 2,549,414 microseconds of throttling over the interval, and two worker
  PIDs at about 49% of one core each. Its rendered report is retained locally under `.local/reports/`.
- Active-run cancellation persists `cancelled`; the policy test also proves an in-flight target task
  receives `CancelledError` and admits no evidence afterward.
- From the agent container, broker DNS/connectivity works while target DNS and public socket access
  fail. The agent has no Docker socket, AWS environment variables, OpenAI key, target token, or admin
  token; its only mounted secret is the investigation relay token.
- No OpenAI key was configured during verification. The keyless fixture validates protocol and
  policy behavior while making no autonomous-diagnosis or model-quality claim.

## Day 1 gate

The local engineering vertical slice is complete: a real Linux CPU fault produces measured,
tamper-evident evidence and a cited, validated report through DSH and the typed interface. Live model
interpretation is ready behind the broker relay but remains unverified until an OpenAI key is supplied.
AWS remains untouched; the local lab intentionally replaces cloud provisioning for this phase.

During integration, Docker Desktop's VM reached 100% capacity because of 20.63 GB of unused build
cache. Only unused build cache was pruned. No images, containers, volumes, or source files were removed.
The failed SQLite WAL was backed up under ignored `.local/recovery/` before the disposable test database
was reset.
