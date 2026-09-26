---
name: linux-cpu-diagnosis
description: Diagnose Linux CPU demand, cgroup quota and throttling, and dominant process scope using interval evidence. Use when symptoms mention CPU, latency, load, throttling, saturation, or hot processes; do not use for memory- or filesystem-only incidents.
---

# Linux CPU diagnosis

Determine whether observed CPU pressure is host-wide, limited by a cgroup quota, attributable to a
dominant process, transient, or unsupported by the available sample.

## Minimum evidence path

1. Call `sample_cpu_pressure` for an interval measurement. Do not diagnose from load average alone.
2. Compare host busy, iowait, and steal with cgroup cores used, quota, and throttled time. These values
   describe different scopes and denominators.
3. Call `rank_processes` only when the CPU sample shows demand or when process attribution is part of
   the operator's question.
4. Repeat a sample only when persistence matters and the first interval is ambiguous. State that the
   result still represents short windows rather than a trend.

## Interpretation patterns

| Evidence pattern | Supported interpretation | Keep open |
| --- | --- | --- |
| Cgroup use reaches its quota and throttling increases while host busy retains headroom | Quota-local CPU contention during the interval | Whether the quota is mis-sized or the workload is inefficient |
| Host busy is high across the available logical CPUs without a local quota constraint | Host-wide CPU demand during the interval | Other runnable work and persistence beyond the sample |
| One or more processes account for most of the constrained scope's use | Dominant observed consumers | Command line, workload ownership, and business cause |
| Load is elevated but CPU busy is not | CPU saturation is not established | I/O wait, blocked tasks, and activity outside the sample |
| Steal is material | Hypervisor contention may contribute | Whether steal is persistent or sufficient to explain latency |

Process percentages use one logical core as 100%. Pair PID with start ticks; a disappeared process is
unknown after it vanishes, not idle. Do not sum host percentage and process one-core percentage as if
they shared a denominator.

## Reporting boundary

Cite fields such as `host_busy_pct`, `host_iowait_pct`, `host_steal_pct`,
`probe_cgroup_cpu_cores_used`, `probe_cgroup_quota_cores`,
`probe_cgroup_throttled_usec_delta`, and `processes` only when present in the cited evidence.

State the sampling interval and scope. Keep application intent, request latency, command line,
thread-level behavior, scheduler cause, and long-term persistence unresolved unless another admitted
observation establishes them.
