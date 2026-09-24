# Capabilities and evidence contract

## Implemented core catalog

Limits below are proposed defaults and hard maxima must be configured on the target. Every result includes the collection interval and quality status.

| Capability | Inputs and bound | Output / source |
|---|---|---|
| `sample_cpu_pressure` | 1–5 seconds; default 2 | `/proc/stat` deltas, load and probe-process cgroup CPU use/throttling |
| `inspect_memory_pressure` | Fixed procfs sources | MemAvailable, swap totals, bounded vmstat counters, optional memory PSI |
| `rank_processes` | CPU/RSS enum, top 1–20, bounded sample and scan | PID/start-time identity, executable name, sampled CPU/RSS |
| `inspect_cgroup_memory` | Opaque `self` or `lab` scope | memory current/max/swap and before/after events; no arbitrary path |
| `inspect_filesystem` | Approved `root` or `lab` mount | Total/available blocks and inodes; read-only flag scoped to the probe namespace |
| `query_service_journal` | Approved unit; max 15-minute window, 500 lines, 256 KiB | Journal artifact and parsed events; no global unbounded log query |
| `read_artifact` | Investigation-owned ID; max 16 KiB per call | Sanitized text with byte/line bounds and digest |
| `get_investigation_state` | Current session | Evidence index, hypotheses, unknowns and remaining budget |
| `update_hypothesis` | Validated claim and evidence IDs | Versioned interpretation; cannot write evidence facts |
| `submit_report` | Structured report with citations | Validated report or actionable schema/reference errors |

Kernel event queries are optional when permissions permit; cgroup counters and controlled service logs must suffice for the core OOM scenario. Report denied access explicitly. Tracing, profiling, arbitrary directory scans, process environment and memory reads are not registered in the core release.

## Linux interpretation rules

- CPU utilization needs deltas over a known interval. Do not double-count guest CPU accounting. Process CPU may exceed 100% when expressed per logical core; state the denominator. CPU steal/credit limits can confound EC2 experiments.
- Load average includes more than CPU demand. Low I/O wait does not prove storage is healthy. Explain observed saturation separately from the application's business-level cause.
- `MemAvailable` differs from free memory. Missing swap means swapping cannot be demonstrated. Counter deltas require consistent boot/process identity.
- Cgroup memory events are cumulative; correlate an increase with the incident interval. A killed process can leave low current memory, so a post-event usage snapshot does not disprove OOM. Preserve the test cgroup until evidence is collected. Hierarchical versus local event scope must be recorded. [Kernel cgroup v2 reference](https://docs.kernel.org/admin-guide/cgroup-v2.html).
- PSI describes time tasks stall for resources, not a root cause by itself. Missing PSI is unsupported, not zero. [Kernel PSI reference](https://docs.kernel.org/accounting/psi.html).
- Exit status 137 alone does not establish OOM. A segmentation fault does not establish stack overflow. Cache/TLB diagnoses require suitable counters and privileges.
- Use both block and inode availability; distinguish the lab mount from root. Filesystem capacity alone does not prove a specific application failed to write.
- A PID can exit or be reused during collection. Pair it with boot ID/start ticks, and return stale identity or partial data instead of attaching observations to a different process.

## Protocol and errors

Target protocol v3 uses authenticated `GET /health` and `POST /v1/probes/{registered_name}`. The
broker selects the target from trusted inventory; agent requests cannot supply arbitrary hosts or URLs.

Request: schema version, request ID, investigation ID, registered probe name, typed arguments, remaining deadline, policy version. Server applies its own lower limits. Deduplicate a repeated request ID with identical arguments during a bounded retention period; reject reuse with different arguments. Do not automatically retry an ambiguous expensive operation. A new observation uses a new ID and timestamp.

Observation status currently includes `ok`, `partial`, `unsupported`, `denied`, and
`output_limited`; transport/policy failures remain typed exceptions outside observations. Include a
machine-readable error code and safe limitation. A successful HTTP exchange may carry unavailable
observation data. Never convert an error into empty successful metrics.

## Evidence and report schema

An evidence record contains:

```text
schema_version, evidence_id, investigation_id, request_id
target_id, boot_id, process_identity (when applicable)
probe_name, probe_version, parser_version, validated_arguments
started_at_utc, completed_at_utc, monotonic_duration_ms
typed_facts, units, scope, status, limitations
artifact_refs, sensitivity, redaction_version, policy_decision_id
```

Artifact metadata: opaque ID, owner investigation, media type/encoding, captured bytes, line count if textual, SHA-256, truncation flag, capture interval, retention deadline, raw-versus-sanitized classification. A capped capture is not a complete raw log: mark it truncated and explain which part was retained.

Raw data stays in the trusted store with restrictive permissions. Sanitization precedes model context admission and sandbox export. Store a separate digest for the sanitized version and map citations to that version. An analysis output generated by the agent is derived material, never automatically promoted to an authoritative observation.

A hypothesis has ID, claim, qualitative status (`open`, `supported`, `weakened`, `rejected`), supporting/contradicting evidence IDs and unresolved questions. Numeric confidence is deferred until calibrated. An evidence reference proves provenance, not causality.

Final report fields: target/interval, outcome, primary finding, supporting citations, alternative explanations, limitations, next recommended probes/actions, and run metrics. Findings cite evidence ID plus field or sanitized artifact line range. Unsupported reference IDs and cross-investigation citations fail validation. Semantic support is evaluated separately by the rubric.

## Retention and limits

Core defaults: 10 MiB captured data per investigation, 1 MiB per observation, 256 KiB journal stdout,
2 MiB decoded target response and 16 KiB artifact page. Enforce limits while streaming; collecting an
unlimited output and truncating afterward is insufficient. Target JSON is gzip-compressed, while
compact typed facts and artifact references enter the agent context. Each page includes byte and line
bounds, total size and SHA-256. Do not upload incident artifacts to S3 in the MVP; the release bucket
contains software, not investigations.
