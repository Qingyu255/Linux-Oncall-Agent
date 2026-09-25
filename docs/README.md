# Design documentation

Status: Day 4 substrate evaluation implemented and verified, 2026-09-24. All 15 repeated AWS
substrate trials passed, two live-model reports were scored, and the cloud resources were destroyed.
Repeated live-model report scoring remains open.

## Read in this order

| Document | Purpose |
|---|---|
| [Architecture tour](architecture-tour.md) | Start here: progressive system explanation, document graph, and source-code map |
| [Day 4 results](day-4-results.md) | Core review fixes, evaluator, repeated AWS results, limitations and teardown |
| [Requirements verification](requirements-verification.md) | Fresh command outputs, scenario setup, P0 matrix and teardown proof |
| [MVP release hardening](release-hardening-plan.md) | Prioritized release gaps, implementation stages and final acceptance gates |
| [Pre-matrix architecture review](pre-matrix-architecture-review.md) | Code findings and focused Terra OOM/filesystem plus 15-trial execution plan |
| [Day 3 status](day-3-status.md) | Implemented collectors, loss-aware data path, fault acceptance and teardown proof |
| [Day 2 status](day-2-status.md) | AWS resources, remote acceptance evidence, defects found and teardown proof |
| [Day 1 status](day-1-status.md) | Implemented scope, measured acceptance evidence and remaining limitation |
| [Execution manifest](execution-manifest.md) | Exact Day 1 versions, image IDs, runtime policy and acceptance artifacts |
| [Requirements](requirements.md) | Product scope, acceptance criteria, full diagnostic backlog |
| [Architecture](architecture.md) | Deployment, trust boundaries, request flow, investigation lifecycle |
| [Python design](python-design.md) | Package structure, class responsibilities, contracts, concurrency and storage |
| [AWS and Terraform](aws-terraform.md) | Resources, IAM, networking, bootstrap, cost and teardown |
| [Capabilities and evidence](capabilities-and-evidence.md) | Probe catalog, limits, Linux semantics, evidence schemas |
| [Security and reliability](security-and-reliability.md) | Enforced policy, sandboxing, failure behavior, adversarial tests |
| [Implementation plan](implementation-plan.md) | Four-day core MVP, optional days, dependency gates and task checklist |
| [Evaluation and demo](evaluation-and-demo.md) | Fault lab, scoring, comparison design and presentation walkthrough |
| [Decisions](decisions.md) | Architecture decisions and when to revisit them |
| [Source review](source-review.md) | Conversation coverage, changes in direction, verified references and open questions |

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
hidden unless the operator enables `/verbose` or passes `--verbose`. Assistant text and reasoning,
prompts, unrestricted arguments and results, raw evidence, and credentials never cross this terminal
channel. Complete admitted evidence remains in the final JSON and Markdown reports.

Four focused days is an aggressive target, approximately 24–32 engineering hours, assuming AWS access and a working model credential. Reserve two more days for integration surprises and presentation preparation. Scope should shrink before the execution boundary or evidence quality does.

## Assumptions to validate in Phase 0

- One developer, one AWS account, one target and one active investigation.
- Proposed region: `ap-southeast-1`; configurable, not inferred account configuration.
- Ubuntu 24.04 LTS x86_64, systemd, cgroup v2; record exact AMI and kernel after validation.
- Local workstation runs the trusted controller and an isolated Linux agent container; EC2 runs only probes and lab workloads.
- Model/provider is configuration, chosen from credentials actually available. No claim that any harness/model is superior before evaluation.
- Synthetic lab data only. The MVP is a research/demo tool, not a production-certified agent.

The full source conversation was paginated to its beginning, but the reader truncates three responses and excludes attachments. [Source review](source-review.md) records the precise limitation. The implementation plan is useful without those missing materials; literal completeness of the original conversation cannot be claimed.
