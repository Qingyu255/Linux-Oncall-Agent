# Design documentation

Status: the core diagnostic substrate and interactive CLI are implemented. The retained 2026-09-24
AWS evaluation passed all 15 substrate trials, scored two live-model reports, and verified teardown.
The same-model report reliability matrix remains open.

## Read in this order

| Document | Purpose |
|---|---|
| [Architecture tour](architecture-tour.md) | Start here: progressive system explanation, document graph, and source-code map |
| [Requirements](requirements.md) | Product scope, acceptance criteria, full diagnostic backlog |
| [Architecture](architecture.md) | Deployment, trust boundaries, request flow, investigation lifecycle |
| [Production reference architecture](production-architecture.md) | Hosted control plane, fleet connectivity, identity, storage, scaling, and the path from the MVP |
| [Python design](python-design.md) | Package structure, class responsibilities, contracts, concurrency and storage |
| [Harness skills and tools](harness-skills-and-tools.md) | Skill discovery, progressive disclosure, tool semantics, enforcement boundaries, and validation |
| [Capabilities and evidence](capabilities-and-evidence.md) | Probe catalog, limits, Linux semantics, evidence schemas |
| [Security and reliability](security-and-reliability.md) | Enforced policy, sandboxing, failure behavior, adversarial tests |
| [AWS and Terraform](aws-terraform.md) | Resources, IAM, networking, bootstrap, cost and teardown |
| [Evaluation and demo](evaluation-and-demo.md) | Fault lab, scoring, comparison design and presentation walkthrough |
| [Requirements verification](requirements-verification.md) | Current checks, retained measurements, P0 matrix and teardown proof |
| [Implementation plan](implementation-plan.md) | Consolidated phase history, optional work and task checklist |
| [MVP release hardening](release-hardening-plan.md) | Prioritized release gaps, implementation stages and final acceptance gates |
| [Pre-matrix architecture review](pre-matrix-architecture-review.md) | Code findings and focused Terra OOM/filesystem plus 15-trial execution plan |
| [Decisions](decisions.md) | Architecture decisions and when to revisit them |
| [Source review](source-review.md) | Conversation coverage, changes in direction, verified references and open questions |

The implementation plan is the compact chronology of the four build phases. Requirements verification
is the single durable record for measured acceptance results; dated per-day status files and the
machine-specific execution manifest were removed after their useful content was consolidated.

## Current and recommended scope

The three core signal families now share the same target limits, provenance, isolation,
cancellation, unavailable-source handling, and persisted evidence contract. Three repetitions of
each fault and both controls passed on one clean EC2 deployment, and probe CPU overhead was recorded.
The live DSH/API path is verified with accepted `gpt-4.1-mini` and `gpt-5.6-terra` reports. Both passed
deterministic evidence gates; the mini report failed causal review and the Terra report passed. The
next gate is to run and human-score the full matrix; substrate passes are not diagnostic-quality passes.

Running `oncall` opens an interactive incident session. The normal terminal view turns safe harness
events into short operator statements about the observation being made and the typed fact it returned.
Harness lifecycle events, model counters, timings, byte counts, IDs, and empty transport details stay
hidden unless the operator enables `/verbose` or passes `--verbose`. Intermediate assistant text,
reasoning, prompts, unrestricted arguments and results, raw evidence, and credentials never cross this
terminal channel. A bounded final assistant response may cross only for a turn that used no tools and
created no diagnostic state. Complete admitted evidence remains in the final JSON and Markdown reports.

Four focused days is an aggressive target, approximately 24–32 engineering hours, assuming AWS access and a working model credential. Reserve two more days for integration surprises and presentation preparation. Scope should shrink before the execution boundary or evidence quality does.

## Assumptions to validate in Phase 0

- One developer, one AWS account, one target and one active investigation.
- Proposed region: `ap-southeast-1`; configurable, not inferred account configuration.
- Ubuntu 24.04 LTS x86_64, systemd, cgroup v2; record exact AMI and kernel after validation.
- Local workstation runs the trusted controller and an isolated Linux agent container; EC2 runs only probes and lab workloads.
- Model/provider is configuration, chosen from credentials actually available. No claim that any harness/model is superior before evaluation.
- Synthetic lab data only. The MVP is a research/demo tool, not a production-certified agent.

The full source conversation was paginated to its beginning, but the reader truncates three responses and excludes attachments. [Source review](source-review.md) records the precise limitation. The implementation plan is useful without those missing materials; literal completeness of the original conversation cannot be claimed.
