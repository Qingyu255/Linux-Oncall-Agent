# Security and reliability

## Threat model

Assume model instructions and target logs can be malicious, the harness can invoke any exposed tool, and targets may be degraded. Trust the operator, broker implementation, target probe implementation and their operating systems. The MVP does not defend against a root-compromised target fabricating measurements or a controller administrator modifying the database.

Policy tracks mutation, sensitivity, perturbation, privilege, target scope and duration independently. Read-only is not equivalent to low risk.

| Class | Core behavior | Examples |
|---|---|---|
| Cheap observation | Automatic within budgets | CPU/memory snapshots |
| Sensitive bounded observation | Only configured scope, sanitize before exposure | Selected service journal |
| Intrusive | Unavailable in core; later operator approval | strace, perf, expensive filesystem scan |
| Mutation | No diagnostic capability | kill, restart, sysctl, file deletion |

Fault injection and cleanup are operator tools with a different authority path. The diagnostic model never receives them, even in the lab.

## Enforced controls

- Agent container: non-root, read-only image, writable bounded workspace/tmpfs, dropped capabilities, no privilege escalation, memory/CPU/PID limits, no host PID namespace, host filesystem, SSH material or Docker socket.
- Network: agent reaches only authenticated broker and fixed provider relay on an internal network. Test denial of target IP, SSM endpoints, metadata addresses, workstation services and arbitrary internet destinations. DNS and forwarding must not provide an alternate route.
- Credentials: target/provider/AWS secrets stay outside the agent. Scope its token to a single investigation and fixed target; revoke at completion. Relay enforces provider path, request size, duration and token budget rather than accepting arbitrary URLs.
- Target: in the local lab, no host port and access only from the broker's private target network;
  bearer authentication, schema validation, a six-capability allowlist, independent duration/output
  limits, and rejection of unknown fields. Day 2 replaces this Docker boundary with loopback HTTPS,
  certificate verification, and SSM transport.
- Execution: fixed executable/argv templates, no shell evaluation, no arbitrary file reader. Resolve mount/cgroup IDs server-side; guard traversal and symlink escapes. Reject stale PID identity.
- Privilege: probe service is unprivileged; access to logs is explicitly granted and tested. Do not add unrestricted sudo to make an unavailable probe pass. Return unavailable data faithfully.
- Storage: broker owns authoritative evidence; sanitized copies only in sandbox. Redaction is a practical filter, not a proof that arbitrary production logs are secret-free. Core lab uses synthetic data.
- Prompt injection: logs/artifacts are data, never instructions. Forged tool requests inside logs cannot grant capabilities. Do not run artifact-provided scripts as part of trusted collection.
- Progress channel: parse bounded harness JSON, then project only allowlisted parameters and validated
  domain facts into the versioned JSONL protocol. The CLI validates this projection again and escapes
  terminal markup. Never forward assistant text, reasoning, raw artifacts, arbitrary error bodies, or
  unknown tool names; map report failures to a closed set of policy reasons.

Future approvals bind the exact capability, target/boot ID, PID/start ticks, arguments, maximum duration, nonce and expiry. One approval authorizes one bounded action. A boolean `approved=true` from the model is not authorization. Approval policy remains outside harness plugins.

## Failure behavior

| Failure | Required response |
|---|---|
| Target unreachable / tunnel lost | Bounded reconnect; preserve evidence; conclude unavailable/inconclusive |
| Probe timeout | Terminate process group and reap; mark partial capture explicitly |
| Controller dies | Target deadline still stops work; persisted run becomes interrupted |
| Harness/model fails or rate-limits | Bounded retry within run budget; export available evidence without fabricated diagnosis |
| Malformed model request | Validation error; no subprocess launched |
| Process disappears or PID reused | Stale/partial result; do not attribute to new process |
| Target reboots | New boot identity; invalidate comparisons across boots |
| Artifact/SQLite disk full | Stop admitting probes; report storage failure rather than losing provenance |
| Journal permission denied | Return unavailable and explain missing evidence |
| Fault cleanup fails | Mark target dirty; prohibit next evaluation; replace target if needed |

Use UTC timestamps for correlation and monotonic clocks for deadlines. Record observed target/controller clock skew. Log request IDs, policy decisions, durations, bytes, error codes and cleanup outcomes; omit secrets and unrestricted raw logs.

## Required adversarial tests

1. Shell metacharacters, executable names and extra JSON fields cannot produce arbitrary target execution.
2. Path traversal, symlink paths, stale IDs and cross-investigation artifact access are rejected.
3. Expired/wrong session credentials fail at both broker and target boundaries.
4. Excessive duration, output floods and parallel requests remain within hard limits.
5. Cancellation and client disconnect leave no long-running collectors.
6. A log line instructing the model to fetch credentials cannot cross the configured boundary.
7. Agent shell cannot read credentials, alter authoritative evidence, contact target or reach metadata endpoints.
8. Report submission with fabricated citations is rejected; valid citations with unsupported reasoning are caught by evaluation.

Passing these tests supports a specific lab claim. It does not establish production readiness or eliminate container/kernel vulnerabilities.
