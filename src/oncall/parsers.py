"""Pure parsers for procfs formats; callers own file access and sample timing."""

from dataclasses import dataclass

from oncall.domain import CgroupMemoryEvents, PressureFacts, VmstatFacts


@dataclass(frozen=True)
class CpuTicks:
    total: int
    idle: int
    iowait: int
    steal: int
    logical_cpus: int


def parse_cpu_stat(text: str) -> CpuTicks:
    lines = text.splitlines()
    values = [int(x) for x in lines[0].split()[1:9]]
    if not lines[0].startswith("cpu ") or len(values) != 8 or min(values) < 0:
        raise ValueError("Malformed aggregate CPU counters")
    # guest and guest_nice are already included in user/nice, not additional time.
    cpus = sum(line.split()[0][3:].isdigit() for line in lines if line.startswith("cpu"))
    if cpus < 1:
        raise ValueError("Missing per-CPU counters")
    return CpuTicks(sum(values), values[3], values[4], values[7], cpus)


def cpu_percentages(before: CpuTicks, after: CpuTicks) -> tuple[float, float, float]:
    delta = after.total - before.total
    differences = [
        after.idle - before.idle,
        after.iowait - before.iowait,
        after.steal - before.steal,
    ]
    if delta <= 0 or min(differences) < 0 or sum(differences) > delta:
        raise ValueError("CPU counters reset or interval has no usable samples")
    idle, wait, steal = differences
    return (100 * (delta - idle - wait - steal) / delta, 100 * wait / delta, 100 * steal / delta)


@dataclass(frozen=True)
class ProcessTicks:
    pid: int
    name: str
    ticks: int
    start_ticks: int
    rss_pages: int


def parse_process_stat(text: str) -> ProcessTicks:
    left, right = text.index("("), text.rindex(")")
    fields = text[right + 1 :].split()  # Starts at field 3 (state).
    name = "".join(c if c.isprintable() else "?" for c in text[left + 1 : right])[:128]
    return ProcessTicks(
        int(text[:left].strip()),
        name,
        int(fields[11]) + int(fields[12]),
        int(fields[19]),
        max(0, int(fields[21])),
    )


def parse_cgroup_stat(text: str) -> dict[str, int]:
    return {key: int(value) for key, value in (line.split() for line in text.splitlines())}


def parse_cpu_quota(text: str) -> float | None:
    quota, period = text.split()
    if quota == "max":
        return None
    if int(period) <= 0 or int(quota) <= 0:
        raise ValueError("Invalid cgroup CPU quota")
    return int(quota) / int(period)


def parse_self_cgroup(text: str) -> str:
    """Return the process's unified cgroup-v2 path."""
    for line in text.splitlines():
        hierarchy, separator, remainder = line.partition(":")
        controllers, separator2, path = remainder.partition(":")
        if separator and separator2 and hierarchy == "0" and controllers == "":
            if not path.startswith("/") or "\x00" in path:
                raise ValueError("Malformed unified cgroup path")
            return path
    raise ValueError("Unified cgroup-v2 membership is unavailable")


def parse_limit(text: str) -> int | None:
    value = text.strip()
    if value == "max":
        return None
    parsed = int(value)
    if parsed < 0:
        raise ValueError("Negative resource limit")
    return parsed


def parse_meminfo(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, raw = line.partition(":")
        if not separator:
            raise ValueError("Malformed meminfo line")
        fields = raw.split()
        if not fields or (len(fields) == 2 and fields[1] != "kB") or len(fields) > 2:
            raise ValueError("Unsupported meminfo unit")
        value = int(fields[0]) * (1024 if len(fields) == 2 else 1)
        if value < 0:
            raise ValueError("Negative meminfo value")
        values[key] = value
    for required in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
        if required not in values:
            raise ValueError(f"Missing meminfo field: {required}")
    return values


def parse_vmstat(text: str) -> VmstatFacts:
    values = {key: int(value) for key, value in (line.split() for line in text.splitlines())}
    if any(value < 0 for value in values.values()):
        raise ValueError("Negative vmstat counter")
    return VmstatFacts(
        pgfault=values.get("pgfault"),
        pgmajfault=values.get("pgmajfault"),
        pswpin=values.get("pswpin"),
        pswpout=values.get("pswpout"),
        oom_kill=values.get("oom_kill"),
    )


def parse_pressure(text: str) -> dict[str, PressureFacts]:
    result: dict[str, PressureFacts] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 5 or fields[0] not in {"some", "full"}:
            raise ValueError("Malformed PSI record")
        values = dict(field.split("=", 1) for field in fields[1:])
        result[fields[0]] = PressureFacts(
            avg10=float(values["avg10"]),
            avg60=float(values["avg60"]),
            avg300=float(values["avg300"]),
            total_usec=int(values["total"]),
        )
    return result


def parse_memory_events(text: str) -> CgroupMemoryEvents:
    values = {key: int(value) for key, value in (line.split() for line in text.splitlines())}
    if any(value < 0 for value in values.values()):
        raise ValueError("Negative cgroup memory event")
    return CgroupMemoryEvents(
        low=values.get("low"),
        high=values.get("high"),
        max=values.get("max"),
        oom=values.get("oom"),
        oom_kill=values.get("oom_kill"),
    )


@dataclass(frozen=True)
class MountInfo:
    mount_point: str
    filesystem_type: str
    options: frozenset[str]


def parse_mountinfo(text: str, path: str) -> MountInfo:
    candidates: list[MountInfo] = []
    for line in text.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            raise ValueError("Malformed mountinfo record")
        left, right = before.split(), after.split()
        if len(left) < 6 or len(right) < 1:
            raise ValueError("Short mountinfo record")
        mount = left[4].replace("\\040", " ")
        if path == mount or path.startswith(mount.rstrip("/") + "/"):
            candidates.append(MountInfo(mount, right[0], frozenset(left[5].split(","))))
    if not candidates:
        raise ValueError("Configured path is not on a visible mount")
    return max(candidates, key=lambda item: len(item.mount_point))
