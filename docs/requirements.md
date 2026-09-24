# Product requirements

## Product outcome

An engineer asks why a Linux host or service is unhealthy without supplying application source code. Linux OnCall Agent gathers bounded OS observations, tracks competing explanations, and produces a diagnosis whose material claims cite evidence. It clearly distinguishes observed conditions, likely causes, and unresolved questions.

The product contribution is the operational contract around a replaceable reasoning runtime: capability boundaries, risk-aware observation, evidence provenance, investigation state, resource budgets, and a reproducible fault lab.

## Users and primary flow

The primary user is an infrastructure or on-call engineer investigating one host. A developer unfamiliar with Linux is a secondary user. The MVP is a CLI with readable progress and JSON/Markdown exports.

1. Operator provisions the disposable target and installs the probe package.
2. Operator starts an investigation with a target alias and symptom.
3. Controller checks target identity, capabilities, authentication, and policy.
4. Harness gathers a cheap baseline, forms hypotheses, and selects additional probes.
5. Each probe passes enforced policy and returns structured evidence and artifact references.
6. A validated report presents findings, supporting and contradicting observations, uncertainty, and suggested next actions.
7. Operator can inspect evidence and cancel. Suggested remediation is text, never executed.

Proposed interface:

```text
oncall doctor
oncall investigate lab-01 --symptom "Requests are slow"
oncall status <investigation-id>
oncall evidence <investigation-id> <evidence-id>
oncall report <investigation-id> --format markdown
oncall cancel <investigation-id>
```

Foreground execution is the first release. `--background`, `attach`, and restartable worker ownership are a later phase; they are retained from the original request rather than silently discarded.

## Requirements and acceptance

The table is the release contract. Day 4 verifies the P0 substrate in 15 repeated trials, adds the
scored bundle format, and retains two real DSH/OpenAI tool-and-report runs for R05: one passed human
review and one exposed an unsupported causal claim. R16 still requires the repeated same-model report
matrix. P1 is a follow-on and P2 is research scope.

| ID | Priority | Requirement | Acceptance evidence |
|---|---|---|---|
| R01 | P0 | Diagnose using OS state without source code | Three lab incidents investigated without repository access |
| R02 | P0 | Agent and target are separate execution environments | Shell inspection shows only sandbox data; direct target access attempts fail |
| R03 | P0 | Typed capabilities; no target command-string API | Invalid names, extra arguments, traversal and shell payloads rejected |
| R04 | P0 | Python owns Linux logic, policy, evidence and application state | Domain tests run without AWS, MCP or harness SDK imports |
| R05 | P0 | DeepSeek Harness integration behind an adapter | One real model run uses tools and exports a report; runtime/version recorded |
| R06 | P0 | Agent can compute over artifacts locally | `grep`/Python analysis succeeds on exported evidence inside sandbox |
| R07 | P0 | Deterministic parsers and explicit observation quality | Fixtures cover valid, malformed, missing, partial and unsupported results |
| R08 | P0 | Persist evidence separately from interpretation | Every factual report claim resolves to evidence and collection provenance |
| R09 | P0 | Large outputs remain artifacts | Bounded tool response plus paginated/readable sanitized artifact |
| R10 | P0 | Track hypotheses, contradictions and unknowns | State survives harness context reset and can reconstruct a report |
| R11 | P0 | Enforce multidimensional probe policy | Deadlines, privilege, sensitivity, output and concurrency checked outside LLM |
| R12 | P0 | Budget and cancellation | Run ends within configured deadline; child probes terminate and audit records remain |
| R13 | P0 | Explicit failure and insufficient-evidence results | Unreachable host and permission denial never become healthy measurements |
| R14 | P0 | Reproducible AWS deployment | Clean Terraform plan/apply, bootstrap readiness, replace and destroy exercised |
| R15 | P0 | Deterministic, bounded fault lab | Injection readiness, reset, cleanup and TTL verified for each scenario |
| R16 | P0 | Honest evaluation | Repeat runs, versioned manifests and raw results; no illustrative numbers presented as results |
| R17 | P0 | Clean, explainable code | Small classes, parser functions, dependency boundaries and tested error paths |
| R18 | P0 | Presentable documentation | Mermaid architecture, ADRs, demo script, limitations and measured results |
| R19 | P1 | Codex plus identical capabilities baseline | Same lab states, budgets, evidence interface and rubric |
| R20 | P1 | Detached operation | Terminal exit does not stop worker; status/attach/cancel work; stale workers detected |
| R21 | P1 | Approval-gated intrusive probes | Operator approval bound to exact target, process identity, arguments and expiry |
| R22 | P1 | Native harness context/policy extensions | A measured failure motivates each extension; before/after evaluation |
| R23 | P2 | Custom loop or evidence-aware compaction | Demonstrable improvement over stock loop without weakened boundaries |
| R24 | P2 | Multiple models and direct-shell baselines | Controlled experiments disclose confounders and separate privileged baselines |
| R25 | P1 | Explicit incident continuation without hidden memory | Child run links an immutable parent, labels historical evidence and requires fresh evidence for current claims |

## Diagnostic scope

| Incident/signal from source | Phase | Required distinction |
|---|---|---|
| CPU saturation, dominant process, multithreaded CPU | Core | Sustained sampled utilization and attribution; load alone is insufficient |
| Cgroup memory exhaustion/OOM | Core | New cgroup OOM event versus historical count; host versus cgroup scope |
| Filesystem capacity exhaustion | Core | Correct mount, available blocks, inode availability and failed write evidence |
| Healthy host | Core control | No invented fault; absence of sampled pressure is not proof all applications are healthy |
| Unreachable target / missing permissions | Core control | Inconclusive or failed investigation, not a clean bill of health |
| Host memory pressure, reclaim, swapping, host OOM | P1 | Requires provisioned swap where applicable; do not infer from low free memory |
| Inode exhaustion, deleted-open files, I/O contention | P1 | Space versus inode limits; filesystem versus device signals |
| FD exhaustion, zombies, crash loops, blocked processes | P1 | Symptoms need attribution and temporal evidence |
| Cgroup CPU throttling, context switching | P1 | Quota and throttling versus host CPU saturation |
| Port conflict, absent listener, connection pressure, DNS/retransmits | P1/P2 | Bounded network probes and endpoint scope |
| `strace`, `perf`, stack overflow and TLB/cache behavior | P2 | Specific evidence and permissions; PMU availability on EC2 must be checked |

Host-level CPU/memory samples still appear in the core baseline even though their full incident families are deferred. `top`, `vmstat`, `pidstat`, `dmesg`, `journalctl`, `df`, `du`, `lsof`, and `ss` are potential instruments, not a requirement to wrap every executable immediately.

## Core release gates

- Each of the three injected incidents succeeds in at least two of three independent runs; report all nine results, including failures. This is a project acceptance target, not a statistical superiority claim.
- Healthy and unreachable controls return appropriate results in three runs each.
- All material claims have valid evidence references; manual review checks that cited evidence actually supports them.
- No successful policy bypass in the defined adversarial suite; no target mutation through diagnostic tools.
- Default investigation budget: 180 seconds, 20 remote probe calls, 2 concurrent low-risk probes, 10 MiB captured artifacts. Tunable by operator within target maxima.
- Record latency, token usage when exposed, probe count, target overhead and faults. Do not promise an unmeasured performance SLA.
- A second clean target can be provisioned, installed and used from documented instructions; teardown leaves only explicitly retained state storage.

## Non-goals

Autonomous remediation, unrestricted target shell, production fleet operation, Kubernetes, a web dashboard, distributed queues, an observability platform, a generic connector framework, and rewriting a model agent loop are outside the core release.
