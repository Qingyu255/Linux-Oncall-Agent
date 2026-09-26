---
name: linux-filesystem-diagnosis
description: Diagnose Linux filesystem capacity, inode exhaustion, read-only state, and ENOSPC correlation on approved mounts. Use for write failures, disk-full alerts, errno 28, inode pressure, or mount-specific capacity incidents.
---

# Linux filesystem diagnosis

Identify the constrained approved mount and distinguish capacity from inode exhaustion, read-only
state, service-visible permissions, and an application failure that merely happened nearby.

## Minimum evidence path

1. Call `inspect_filesystem` for the mount named or implied by the symptom. Use `lab` for the
   controlled service volume and `root` for the root filesystem; never treat them as interchangeable.
2. Inspect both `available_bytes` and `available_inodes`, plus filesystem type and
   `probe_view_readonly`. Availability reflects the probe service user's view, which may differ from
   raw free blocks because of reservations.
3. Query the relevant service journal only when the question asks whether a write failed or when the
   error class distinguishes capacity from another cause.
4. Inspect the other approved mount only when it is a credible alternative or needed to show the
   constraint is mount-local.

## Interpretation patterns

| Evidence pattern | Supported interpretation | Do not overstate |
| --- | --- | --- |
| Very low available bytes and a correlated `errno 28` service event on the same mount and interval | Block-capacity ENOSPC is supported | Which application behavior consumed the space |
| Available inodes exhausted with blocks remaining and a correlated ENOSPC event | Inode exhaustion is supported | Block exhaustion |
| Probe view is read-only | Writes through that view are unavailable | ENOSPC; read-only failures normally have a different error class |
| Lab mount constrained while root retains capacity | Mount-local constraint | Host-wide disk exhaustion |
| Capacity constrained without a service error | Resource risk is established | That a particular write failed because of it |

An empty journal result covers only the approved unit, boot, and bounded time window. It does not prove
the absence of a write failure elsewhere or earlier.

## Reporting boundary

Cite only present fields such as `mount_id`, `filesystem_type`, `available_bytes`, `used_percent`,
`available_inodes`, `probe_view_readonly`, `entries`, and `captured_bytes`. Correlate mount, target,
boot, and interval before linking capacity to an application error. Keep file ownership, write rate,
largest directories, storage latency, and cleanup safety unresolved because the diagnostic tools do
not observe them.
