# Source review and open questions

## Conversation coverage

Source: [Linux Ops Project Planning](chatgpt-conversation://6ab2808a-2028-83ec-8e96-2404e381a259).

The reader returned all 15 turns in two pages, ending with `hasMore=false`. This reaches the beginning of the conversation. Three assistant messages were capped at 20,000 characters: the comprehensive PRD, the earlier harness comparison, and the original interview-preparation response. Three referenced uploaded files were not included in the returned content. Consequently this is a synthesis of all accessible turns, not a claim that unavailable tails or attachments were read.

The missing attachments would be needed to verify their exact interview guidance or additional harness constraints. They are not required to draft the engineering baseline. No personal interview history is reproduced in the public-facing project requirements.

## Evolution of the plan

| Source discussion | Treatment here |
|---|---|
| Early service supervisor/rolling deployment idea | Superseded by user's Linux diagnosis idea; not part of this product |
| Journaling and generic connector ideas | Alternatives considered, outside scope |
| Diagnose OS pressure without application source | Core product requirement |
| Prefer Python, later accept some TypeScript | Python domain; small native hooks only when justified |
| Move from framework discussion to existing harnesses | Reuse a stock runtime; no framework/loop rewrite first |
| DeepSeek primary, Codex comparison | Preserved, with compatibility gate and documented fallback |
| Initial suggestion to remove shell entirely | Superseded by later clarification: allow local sandbox computation, forbid unrestricted target shell |
| Operational substrate as selling point | Typed capabilities, policy, evidence, state and low target footprint |
| Broad list of Linux incidents | All retained in prioritized diagnostic backlog; three in core |
| Background CLI/fork idea | Retained as follow-on with explicit process ownership requirements |
| Many proposed harness plugins | Consolidate policy/evidence in Python first; native hooks need evaluation evidence |
| Names Triage/HostScope then LinuxOnCallAgent | Use repository-aligned Linux OnCall Agent; planned CLI `oncall` |
| AWS EC2/Terraform | New requirement in current request; concrete topology proposed here |

## Verified primary references

Checked during planning on 2026-09-22/23. These are moving documentation pages, not pinned runtime guarantees.

- [DeepSeek Harness official overview](https://deepseek.com/harness/en/): composable plugin architecture and developer-preview status.
- [DeepSeek official Python SDK guide](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/python-sdk.md): Python integration, isolated runtime home, minimal-profile caveats. Pin actual SDK/profile versions in Phase 0.
- [OpenAI Codex MCP documentation](https://developers.openai.com/codex/mcp): shared capability interface for the baseline; exact SDK choice deferred to adapter spike.
- [AWS Session Manager port forwarding](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html): transport prerequisites and mechanics.
- [AWS Session Manager overview](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html): port-forward logging limitation.
- [AWS Systems Manager VPC endpoints](https://docs.aws.amazon.com/systems-manager/latest/userguide/setup-create-vpc.html): private-network alternative.
- [Terraform S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3): backend locking and state permissions.
- [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) and [PSI](https://docs.kernel.org/accounting/psi.html): signal semantics.

The source conversation's rankings of harness quality and example benchmark scores are opinions/illustrations, not measured evidence. This plan makes no superiority claim and carries no fabricated benchmark results forward.

## Open questions and chosen defaults

| Question | Working default | When it must be resolved |
|---|---|---|
| AWS account, region and spending ceiling? | One disposable target, proposed Singapore region; no assumed budget | Before first apply |
| Available model/provider credentials? | Configurable provider through DSH adapter | Phase 0 |
| Supported SDK/profile and relay behavior? | Pin after real tool-call/cancellation test | Phase 0 |
| Exact instance type and AMI? | Ubuntu 24.04 x86_64, at least 2 vCPU/4 GiB; prefer stable CPU for evals | Before provisioning |
| Existing Linux container environment? | Local isolated agent plus trusted broker | Phase 0 |
| Time available over next few days? | Four focused days plus optional two | At implementation start |
| Need detached CLI in first demo? | Foreground core, background in follow-on | After first vertical slice |
| Native plugin essential to presentation? | Only add for a measured behavioral gap | After baseline evaluation |
| Production logs/targets? | Synthetic disposable lab only | Separate future design review |

These defaults allow implementation to proceed without guessing credentials or provisioning preferences. They are design choices, not claims that the user has supplied account configuration.
