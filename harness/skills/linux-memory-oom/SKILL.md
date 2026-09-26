---
name: linux-memory-oom
description: Diagnose host memory pressure and cgroup OOM events using counter deltas, PSI, limits, and bounded journal evidence. Use for OOMKilled, exit 137, allocation failure, memory pressure, or suspected cgroup limits.
---

# Linux memory and cgroup OOM diagnosis

Separate current host pressure from a cgroup limit event. A process may be gone and current usage low
after an OOM, so post-event usage alone cannot rule one out.

## Minimum evidence path

1. Call `inspect_memory_pressure` to establish host availability, swap, VM counters, and optional PSI.
2. Call `inspect_cgroup_memory` for the relevant opaque scope: `self` for the probe service or `lab`
   for the controlled workload. Its before-and-after event delta is stronger than a single cumulative
   value.
3. Query `oncall-lab-workload.service` or `oncall-target.service` only when a bounded service event can
   establish timing or explain a process exit. Read artifact pages only when the typed journal summary
   is insufficient.

## Evidence hierarchy

| Evidence | Meaning |
| --- | --- |
| New `oom_kill_delta` in the relevant cgroup and stable target boot | Direct support for a cgroup OOM kill during the sample |
| New `oom_delta` without `oom_kill_delta` | Cgroup OOM handling occurred; a kill is not established |
| Low `mem_available_bytes` with memory PSI or major-fault activity | Host pressure is supported for the interval; cause remains open |
| Cgroup current near its finite maximum without a new event | Limit pressure is plausible; an OOM event is not established |
| Exit 137 or a service restart alone | Compatible with several causes and insufficient to claim OOM |

`memory.events` and VM counters are cumulative. Use deltas within a stable boot and name the cgroup
scope. Missing swap or PSI means unsupported or absent on that host, not zero pressure. PSI measures
stall time, not root cause.

## Competing explanations

Keep host pressure, cgroup limit enforcement, expected transient allocation, external termination, and
missing historical evidence separate. A cgroup OOM can occur while the host retains memory headroom;
host pressure does not prove which process or cgroup caused it.

## Reporting boundary

Cite only present fields such as `mem_available_bytes`, `swap_total_bytes`, `pressure_some`,
`pressure_full`, `current_bytes`, `max_bytes`, `oom_delta`, and `oom_kill_delta`. State whether the
finding is a current pressure snapshot, an interval event, or a historical journal record. Keep the
allocation site and application-level leak hypothesis unresolved without profiling evidence.
