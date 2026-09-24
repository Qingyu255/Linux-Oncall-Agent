# Architecture decision records

ADRs 001–012 shape the implemented Day 4 substrate. Revisit decisions using evidence and update this file
when choices change.

## ADR-001 — Keep reasoning off the target

**Context:** the target may already be resource-constrained; arbitrary target shell undermines typed policy.

**Decision:** isolated agent environment, trusted broker, small deterministic target service.

**Alternatives:** install the harness directly; constrained SSH wrapper; remote probe service. Choose the service for a clear auditable boundary. Cost: packaging, authentication and network failure handling. Revisit only if measured deployment cost outweighs the boundary's value; preserve isolation in any alternative.

## ADR-002 — Python domain, replaceable harness

**Decision:** Python owns probes, parsers, policy, evidence, CLI and evaluation. DeepSeek Harness is the intended runtime, with a bounded integration spike. Codex is a baseline/fallback, not a rejected technology. No ADK/LangGraph dependency or custom loop in the core.

**Reason:** this matches the source's final direction and keeps authored code explainable. DSH exposes composable capabilities but remains developer preview ([official project page](https://deepseek.com/harness/en/)). Pin a tested version. Native TypeScript is appropriate only for a demonstrated lifecycle gap; it does not replace target authorization.

## ADR-003 — Separate MCP from target protocol

**Decision:** harness-facing MCP adapter, versioned HTTPS target API over SSM. Domain does not depend on either protocol.

**Tradeoff:** two small adapters versus carrying harness protocol concerns onto the target. Direct MCP on target is viable later if it reduces implementation complexity without weakening limits or privilege separation.

## ADR-004 — Local control plane and one EC2 target

**Decision:** provision only the Linux target for the core; local trusted controller and agent container. SSM tunnel avoids inbound ports and SSH keys. Public-subnet outbound connectivity avoids NAT cost for a short-lived lab.

**Tradeoff:** local Docker/network setup and a trusted broker with AWS credentials. Private endpoints, a remote controller and managed secret rotation are production-oriented follow-ons, not hidden MVP requirements.

## ADR-005 — Three incident classes first

**Decision:** CPU saturation, cgroup OOM and filesystem capacity exhaustion, with healthy/unavailable controls.

**Reason:** they exercise temporal samples, process attribution, kernel resource isolation, filesystem semantics and honest failure handling. Defer swap, host OOM, tracing and PMU work until the initial evaluation is reproducible. Keep the full backlog in requirements.

## ADR-006 — Immutable evidence, mutable interpretations

**Decision:** SQLite plus filesystem artifacts; observations never overwritten by model reasoning. Reports and hypotheses refer to stable evidence IDs.

**Alternatives:** harness transcript only loses domain invariants; PostgreSQL/S3 add unnecessary core deployment. Revisit storage when multiple writers or remote users actually appear.

## ADR-007 — Stock loop before custom behavior

**Decision:** let the model choose probes inside deterministic policy and budget limits. Maintain explicit state and validate reports in Python. Add a native context/completion hook only after a repeatable evaluation failure.

**Tradeoff:** generic behavior may be imperfect initially. This yields an honest experiment and avoids hardcoding a diagnostic workflow that merely appears agentic.

## ADR-008 — Operator-only mutations

**Decision:** lab setup/reset, bootstrap and Terraform are separate operator workflows. Diagnostic tools never remediate. Intrusive probes are absent in core rather than exposed with an unimplemented approval promise.

**Tradeoff:** narrower functionality, simpler enforceable contract. Future approvals require exact action binding and independent operator identity.

## ADR-009 — Prove the boundary locally before AWS

**Context:** the implementation can validate Linux `/proc`, cgroup behavior, DSH/MCP integration,
credential separation, persistence, cancellation and reporting without cloud cost or an API key.

**Decision:** Day 1 uses three Docker roles: target, trusted broker, and untrusted harness. The agent
joins only an internal broker network. The target joins only a separate broker network. A deterministic
OpenAI-wire fixture exercises the real harness loop without claiming to evaluate model reasoning.

**Tradeoff:** Docker shares the VM kernel and does not validate SSM, EC2 bootstrapping, IAM, or live
model interpretation. Move the same typed contracts to AWS in Day 2; do not redesign the domain around
the cloud transport.

## ADR-010 — Keep compact facts in context and artifacts by reference

**Context:** procfs snapshots and journals can exceed useful model context, but truncating after an
unbounded read risks target memory and silently discarding the source prevents later review.

**Decision:** cap while collecting, sanitize before leaving the target, gzip target responses, and
persist the sanitized raw capture once in the trusted store. Agent tools return typed facts and opaque
artifact IDs. The agent requests UTF-8-safe 16 KiB pages with byte/line offsets, total size and digest.

**Tradeoff:** paging adds tool calls and the local store remains part of the trusted computing base.
It retains provenance and makes network/context use proportional to the portion actually inspected.
Move artifacts to object storage only when remote/multi-user operation creates that requirement.

## ADR-011 — Model fault lifecycle as operator strategies

**Context:** reproducible CPU, OOM and filesystem failures require mutation, while the diagnostic agent
must remain read-only and a failed reset can poison later trials.

**Decision:** an operator-only `FaultController` selects explicit `FaultScenario` strategies through an
`OperatorExecutor`. It requires disposable inventory, grants a 30–120 second lease, checks readiness,
runs exact cleanup, and marks the target dirty when cleanup cannot be verified. Diagnostic MCP tools
have no route to this facade.

**Tradeoff:** fault definitions contain scenario-specific setup and verification. This duplication is
preferable to a generic remote command API that would erase the authority boundary.

## ADR-012 — Separate substrate gates from diagnostic judgment

**Context:** parser correctness, evidence identity, citation resolution, cleanup, and budgets are
mechanically checkable. Whether a model identified the cause and scope without unsupported claims is
a semantic judgment. Combining them would let a fixture pass as model quality or let a plausible
answer conceal broken provenance.

**Decision:** write a versioned manifest, evaluator-only scenario truth, evidence, ordered events,
report, and score for each run. `TrialScorer` computes mechanical gates. A separate `HumanReview`
records diagnostic success and unsupported claims. Fixture diagnostic quality is `not_applicable`,
and unavailable controls may validly contain no observation if their report is inconclusive.

**Tradeoff:** final evaluation needs human review and cannot collapse to one automatic number. That
cost keeps claims auditable and makes missing live-model trials visible instead of treating substrate
reliability as reasoning success.

## ADR-013 — Continue incidents through explicit child runs

**Context:** operators need to ask follow-up questions, but silently reusing a harness conversation can
mix stale measurements, different target boots, and unrelated incidents. Reopening an accepted report
would also weaken reproducibility and auditability.

**Decision:** `investigate` always creates a fresh root run. `continue <run-id>` creates a new child run
within a 24-hour TTL and a five-generation lineage bound. The child reconstructs context from trusted
reports, hypotheses, and evidence. Prior evidence is labeled historical with its age. Retrospective
claims cite historical evidence; claims about current conditions require fresh child evidence. The
first new observation must match the parent target and records same-boot versus rebooted status.

**Tradeoff:** continuation spends a new model session and does not reproduce every hidden token from
the old transcript. In return, the operational context is explicit, bounded, inspectable, and tied to
the evidence that actually supports the answer.
