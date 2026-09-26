---
name: linux-incident-triage
description: Plan and close evidence-driven Linux CPU, memory/OOM, and filesystem investigations through the oncall MCP tools. Use for incident symptoms, ambiguous resource failures, follow-ups, or target-unavailable cases; do not use for product help or general conversation.
---

# Linux incident triage

Reach the narrowest conclusion supported by current, target-scoped evidence. Prefer a short
discriminating investigation over collecting every available signal.

## Frame the question

Identify four things before interpreting measurements:

- the reported symptom and affected service or scope;
- whether the question is current or retrospective;
- the target and boot relationship; and
- the observation that would distinguish the leading explanations.

For a continuation, call `get_investigation_state` first. Treat prior evidence as historical and use
it only for retrospective claims. Collect fresh evidence before claiming the target still has a
condition. If the first new observation reports a reboot relationship, do not compare cumulative
counters across the boot boundary.

Load the relevant resource skill before interpreting its evidence:

- `linux-cpu-diagnosis` for latency, load, saturation, throttling, or hot processes;
- `linux-memory-oom` for memory pressure, allocation failure, exit 137, or OOM events; and
- `linux-filesystem-diagnosis` for write failure, ENOSPC, inode, read-only, or mount capacity.

Load another resource skill only when the symptom or collected evidence makes that alternative
credible.

## Investigation loop

1. Start with the cheapest probe that can separate the leading scopes or causes.
2. Check observation status, interval, scope, target, boot, and limitations before using facts.
3. Maintain a primary explanation and at least one credible alternative. Use `update_hypothesis` when
   evidence materially supports, weakens, or rejects one; do not update it merely to create activity.
4. Collect the next probe only when its possible outcomes would change the report.
5. Stop when the primary scope and condition are supported, important alternatives are bounded, and
   further available probes would only repeat the same short-window evidence.

Do not turn an unavailable, partial, denied, truncated, or historical observation into a current
healthy value. Evidence IDs establish provenance; they do not by themselves establish causality.

## Failure and retry discipline

- After one transport failure, retry only when a transient recovery is plausible and the next attempt
  remains within the deadline.
- After two equivalent consecutive transport failures, stop probing that target, call
  `get_investigation_state`, and submit an inconclusive report.
- Do not retry `unsupported` or `denied` without a changed capability or permission state. Preserve it
  as a limitation and choose another evidence source only if one can answer the same question.
- If an artifact page is sufficient, do not read the rest. Preserve its byte and line bounds.
- If report submission is rejected, correct the named schema, scope, or fact-field defect. Do not
  invent new evidence or rewrite unrelated findings.

## Report contract

Use `completed` only when current evidence supports the primary finding and its scope. Every completed
claim must cite evidence IDs and exact top-level fields under `facts`; omit prefixes such as `facts.` or
the fact kind.

Use `inconclusive` when observations are unavailable, contradictory, or too weak for the requested
conclusion. If no observation succeeded, submit zero claims rather than manufacturing a finding.
Explain the failure in limitations and recommend the smallest action that would restore observability
or distinguish the remaining alternatives.

Every report includes alternatives, limitations, and next steps. Separate the measured resource
condition from the unobserved application or business cause.
