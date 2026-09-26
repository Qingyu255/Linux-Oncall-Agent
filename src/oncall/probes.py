"""Bounded Linux observations selected through an explicit probe registry."""

import asyncio
import json
import os
import re
import signal
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from oncall.config import DEFAULT_RUNTIME_CONFIG
from oncall.domain import (
    CgroupMemoryFacts,
    CpuFacts,
    Facts,
    FilesystemFacts,
    JournalFacts,
    MemoryFacts,
    Observation,
    ObservationStatus,
    ProbeName,
    ProbeRequest,
    ProcessFacts,
    ProcessRanking,
    utcnow,
)
from oncall.parsers import (
    ProcessTicks,
    cpu_percentages,
    parse_cgroup_stat,
    parse_cpu_quota,
    parse_cpu_stat,
    parse_limit,
    parse_meminfo,
    parse_memory_events,
    parse_mountinfo,
    parse_pressure,
    parse_process_stat,
    parse_self_cgroup,
    parse_vmstat,
)

MAX_RAW_BYTES = 1024 * 1024


def read_bounded(path: Path, maximum: int = 128 * 1024) -> str:
    with path.open("rb") as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("Observation exceeds byte limit")
    return value.decode("utf-8", errors="replace")


class UnsupportedProbe(RuntimeError):
    pass


@dataclass(frozen=True)
class Collected:
    facts: Facts
    raw: str
    limitations: tuple[str, ...] = ()
    status: ObservationStatus = "ok"
    truncated: bool = False


class LinuxProbe(ABC):
    """Template Method for identity, timing, failure quality, and output bounds."""

    name: ProbeName

    def __init__(self, target_id: str, proc: Path = Path("/proc")) -> None:
        self.target_id = target_id
        self.proc = proc

    async def collect(self, request: ProbeRequest) -> Observation:
        if request.name != self.name:
            raise ValueError("Probe received a request for a different capability")
        started, monotonic = utcnow(), time.monotonic()
        boot = read_bounded(self.proc / "sys/kernel/random/boot_id").strip()
        status: ObservationStatus = "ok"
        error_code = None
        try:
            collected = await self._collect(request)
        except PermissionError:
            status, error_code = "denied", "permission_denied"
            collected = None
        except (FileNotFoundError, UnsupportedProbe):
            status, error_code = "unsupported", "source_unavailable"
            collected = None
        completed = utcnow()
        if read_bounded(self.proc / "sys/kernel/random/boot_id").strip() != boot:
            raise ValueError("Target rebooted during observation")
        if collected is None:
            raw = json.dumps({"error": error_code})
            return Observation(
                target_id=self.target_id,
                boot_id=boot,
                request=request,
                started_at=started,
                completed_at=completed,
                duration_ms=(time.monotonic() - monotonic) * 1000,
                status=status,
                error_code=error_code,
                facts=None,
                limitations=("Requested source could not be observed",),
                raw=raw,
                raw_bytes=len(raw.encode()),
            )
        encoded = collected.raw.encode()
        raw = collected.raw
        truncated = collected.truncated
        limitations = list(collected.limitations)
        if len(encoded) > MAX_RAW_BYTES:
            raw = encoded[:MAX_RAW_BYTES].decode("utf-8", errors="ignore")
            truncated = True
            limitations.append("Raw artifact capped at 1 MiB")
        return Observation(
            target_id=self.target_id,
            boot_id=boot,
            request=request,
            started_at=started,
            completed_at=completed,
            duration_ms=(time.monotonic() - monotonic) * 1000,
            status="output_limited" if truncated and collected.status == "ok" else collected.status,
            facts=collected.facts,
            limitations=tuple(limitations),
            raw=raw,
            raw_bytes=len(raw.encode()),
            raw_truncated=truncated,
        )

    @abstractmethod
    async def _collect(self, request: ProbeRequest) -> Collected: ...


class CgroupReader:
    def __init__(self, path: Path) -> None:
        self.path = path

    def cpu(self) -> dict[str, int] | None:
        try:
            return parse_cgroup_stat(read_bounded(self.path / "cpu.stat"))
        except (FileNotFoundError, PermissionError):
            return None


class CpuPressureProbe(LinuxProbe):
    name: ProbeName = "sample_cpu_pressure"

    def __init__(self, target_id: str, proc: Path, cgroup: Path) -> None:
        super().__init__(target_id, proc)
        self.cgroup = cgroup
        self.reader = CgroupReader(cgroup)

    async def _collect(self, request: ProbeRequest) -> Collected:
        limitations: list[str] = []
        raw1 = read_bounded(self.proc / "stat")
        cg1 = self.reader.cpu()
        monotonic = time.monotonic()
        await asyncio.sleep(request.duration_seconds)
        elapsed = time.monotonic() - monotonic
        raw2 = read_bounded(self.proc / "stat")
        cg2 = self.reader.cpu()
        before, after = parse_cpu_stat(raw1), parse_cpu_stat(raw2)
        busy, wait, steal = cpu_percentages(before, after)
        try:
            quota = parse_cpu_quota(read_bounded(self.cgroup / "cpu.max"))
        except (FileNotFoundError, PermissionError):
            quota = None
            limitations.append("CPU quota unavailable")
        cores, throttled = None, None
        if cg1 is not None and cg2 is not None:
            delta = cg2["usage_usec"] - cg1["usage_usec"]
            if delta < 0:
                raise ValueError("Cgroup CPU counters reset")
            cores = delta / 1_000_000 / elapsed
            if "throttled_usec" in cg1 and "throttled_usec" in cg2:
                throttled = cg2["throttled_usec"] - cg1["throttled_usec"]
        else:
            limitations.append("Cgroup CPU usage unavailable")
        facts = CpuFacts(
            logical_cpus=after.logical_cpus,
            host_busy_pct=busy,
            host_iowait_pct=wait,
            host_steal_pct=steal,
            probe_cgroup_cpu_cores_used=cores,
            probe_cgroup_quota_cores=quota,
            probe_cgroup_throttled_usec_delta=throttled,
            load1=float(read_bounded(self.proc / "loadavg").split()[0]),
        )
        return Collected(
            facts,
            json.dumps(
                {
                    "proc_stat_before": raw1,
                    "proc_stat_after": raw2,
                    "cgroup_before": cg1,
                    "cgroup_after": cg2,
                }
            ),
            tuple(limitations),
        )


class ProcessRankingProbe(LinuxProbe):
    name: ProbeName = "rank_processes"

    def _processes(self) -> tuple[dict[int, ProcessTicks], bool]:
        result: dict[int, ProcessTicks] = {}
        count = 0
        for path in self.proc.iterdir():
            if not path.name.isdecimal():
                continue
            count += 1
            if count > 2048:
                return result, True
            try:
                item = parse_process_stat(read_bounded(path / "stat", 8192))
                result[item.pid] = item
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        return result, False

    async def _collect(self, request: ProbeRequest) -> Collected:
        first, limited1 = self._processes()
        monotonic = time.monotonic()
        await asyncio.sleep(request.duration_seconds)
        elapsed = time.monotonic() - monotonic
        second, limited2 = self._processes()
        hz, page_size = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
        ranking = []
        for pid, item in second.items():
            old = first.get(pid)
            if old is None or old.start_ticks != item.start_ticks or item.ticks < old.ticks:
                continue
            ranking.append(
                ProcessFacts(
                    pid=pid,
                    start_ticks=item.start_ticks,
                    name=item.name,
                    cpu_pct_one_core=100 * (item.ticks - old.ticks) / hz / elapsed,
                    rss_bytes=item.rss_pages * page_size,
                )
            )
        ranking.sort(key=lambda item: (-item.cpu_pct_one_core, item.pid))
        disappeared = sum(
            pid not in second or second[pid].start_ticks != old.start_ticks
            for pid, old in first.items()
        )
        facts = ProcessRanking(
            processes=tuple(ranking[: request.limit]),
            scanned=len(first),
            disappeared=disappeared,
            scan_limited=limited1 or limited2,
        )
        raw = json.dumps(
            {
                "before": {str(pid): vars(item) for pid, item in first.items()},
                "after": {str(pid): vars(item) for pid, item in second.items()},
            }
        )
        limitations = ("Process scan capped at 2048 entries",) if limited1 or limited2 else ()
        return Collected(facts, raw, limitations)


class MemoryPressureProbe(LinuxProbe):
    name: ProbeName = "inspect_memory_pressure"

    async def _collect(self, request: ProbeRequest) -> Collected:
        mem_raw = read_bounded(self.proc / "meminfo")
        vm_raw = read_bounded(self.proc / "vmstat")
        values = parse_meminfo(mem_raw)
        limitations: list[str] = []
        pressure_raw = None
        pressure = {}
        try:
            pressure_raw = read_bounded(self.proc / "pressure/memory")
            pressure = parse_pressure(pressure_raw)
        except FileNotFoundError:
            limitations.append("Memory PSI unavailable")
        facts = MemoryFacts(
            mem_total_bytes=values["MemTotal"],
            mem_available_bytes=values["MemAvailable"],
            swap_total_bytes=values["SwapTotal"],
            swap_free_bytes=values["SwapFree"],
            anonymous_bytes=values.get("AnonPages"),
            file_cache_bytes=values.get("Cached"),
            vmstat=parse_vmstat(vm_raw),
            pressure_some=pressure.get("some"),
            pressure_full=pressure.get("full"),
        )
        return Collected(
            facts,
            json.dumps({"meminfo": mem_raw, "vmstat": vm_raw, "pressure": pressure_raw}),
            tuple(limitations),
        )


class CgroupMemoryProbe(LinuxProbe):
    name: ProbeName = "inspect_cgroup_memory"

    def __init__(self, target_id: str, proc: Path, scopes: dict[str, Path]) -> None:
        super().__init__(target_id, proc)
        self.scopes = scopes

    @staticmethod
    def _optional(path: Path) -> int | None:
        try:
            return parse_limit(read_bounded(path, 4096))
        except FileNotFoundError:
            return None

    async def _collect(self, request: ProbeRequest) -> Collected:
        assert request.scope_id is not None
        path = self.scopes[request.scope_id]
        before_raw = read_bounded(path / "memory.events", 16 * 1024)
        before = parse_memory_events(before_raw)
        await asyncio.sleep(request.duration_seconds)
        after_raw = read_bounded(path / "memory.events", 16 * 1024)
        after = parse_memory_events(after_raw)

        def delta(old: int | None, new: int | None) -> int | None:
            if old is None or new is None:
                return None
            if new < old:
                raise ValueError("Cgroup memory events reset")
            return new - old

        facts = CgroupMemoryFacts(
            scope_id=request.scope_id,
            current_bytes=int(read_bounded(path / "memory.current", 4096)),
            max_bytes=self._optional(path / "memory.max"),
            high_bytes=self._optional(path / "memory.high"),
            swap_current_bytes=self._optional(path / "memory.swap.current"),
            swap_max_bytes=self._optional(path / "memory.swap.max"),
            events_before=before,
            events_after=after,
            oom_delta=delta(before.oom, after.oom),
            oom_kill_delta=delta(before.oom_kill, after.oom_kill),
        )
        return Collected(
            facts,
            json.dumps(
                {
                    "scope_id": request.scope_id,
                    "events_before": before_raw,
                    "events_after": after_raw,
                }
            ),
        )


class FilesystemProbe(LinuxProbe):
    name: ProbeName = "inspect_filesystem"

    def __init__(self, target_id: str, proc: Path, mounts: dict[str, Path]) -> None:
        super().__init__(target_id, proc)
        self.mounts = mounts

    async def _collect(self, request: ProbeRequest) -> Collected:
        assert request.mount_id is not None
        path = self.mounts[request.mount_id]
        stats = os.statvfs(path)
        mount_raw = read_bounded(self.proc / "self/mountinfo", 512 * 1024)
        mount = parse_mountinfo(mount_raw, str(path.resolve()))
        total = stats.f_blocks * stats.f_frsize
        available = stats.f_bavail * stats.f_frsize
        used = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
        denominator = used + available
        facts = FilesystemFacts(
            mount_id=request.mount_id,
            filesystem_type=mount.filesystem_type,
            total_bytes=total,
            available_bytes=available,
            used_percent=100 * used / denominator if denominator else 0,
            total_inodes=stats.f_files,
            available_inodes=stats.f_favail,
            probe_view_readonly="ro" in mount.options,
        )
        return Collected(
            facts,
            json.dumps(
                {
                    "mount_id": request.mount_id,
                    "mount_point": mount.mount_point,
                    "filesystem_type": mount.filesystem_type,
                    "options": sorted(mount.options),
                    "statvfs": list(stats),
                }
            ),
        )


@dataclass(frozen=True)
class CommandCapture:
    stdout: bytes
    stderr: bytes
    truncated: bool


class JournalReader(ABC):
    @abstractmethod
    async def read(self, unit: str, since_seconds: int, limit: int) -> CommandCapture: ...


class SystemdJournalReader(JournalReader):
    """Fixed-command adapter that caps pipes while the child is still running."""

    executable = Path("/usr/bin/journalctl")

    async def read(self, unit: str, since_seconds: int, limit: int) -> CommandCapture:
        if not self.executable.exists():
            raise UnsupportedProbe("journalctl is unavailable")
        process = await asyncio.create_subprocess_exec(
            str(self.executable),
            "--unit",
            unit,
            "--since",
            f"{since_seconds} seconds ago",
            "--lines",
            str(limit),
            "--output",
            "json",
            "--no-pager",
            "--boot",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        truncated = False

        async def drain(stream: asyncio.StreamReader, maximum: int) -> bytes:
            nonlocal truncated
            result = bytearray()
            while chunk := await stream.read(8192):
                remaining = maximum - len(result)
                result.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    break
            return bytes(result)

        stdout_task = asyncio.create_task(drain(process.stdout, 256 * 1024))
        stderr_task = asyncio.create_task(drain(process.stderr, 16 * 1024))
        try:
            async with asyncio.timeout(6):
                stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
                returncode = await process.wait()
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise
        if returncode not in {0, -signal.SIGTERM}:
            raise UnsupportedProbe(stderr.decode(errors="replace")[:200])
        return CommandCapture(stdout, stderr, truncated)


class Sanitizer(ABC):
    @abstractmethod
    def sanitize(self, text: str) -> str: ...


class RegexSanitizer(Sanitizer):
    patterns = (
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer)\s+\S+"),
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        re.compile(r"(?i)\b(password|token|secret|api[_-]?key)\s*[:=]\s*\S+"),
    )

    def sanitize(self, text: str) -> str:
        value = "".join(character if character.isprintable() else "?" for character in text)
        for pattern in self.patterns:
            value = pattern.sub(
                lambda match: f"{match.group(1)}=[REDACTED]" if match.lastindex else "[REDACTED]",
                value,
            )
        return value[:4096]


class JournalProbe(LinuxProbe):
    name: ProbeName = "query_service_journal"

    def __init__(
        self,
        target_id: str,
        proc: Path,
        reader: JournalReader,
        sanitizer: Sanitizer,
    ) -> None:
        super().__init__(target_id, proc)
        self.reader = reader
        self.sanitizer = sanitizer

    async def _collect(self, request: ProbeRequest) -> Collected:
        assert request.unit is not None
        capture = await self.reader.read(request.unit, request.since_seconds, request.limit)
        entries: list[dict[str, str]] = []
        skipped = 0
        for line in capture.stdout.decode("utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
                entries.append(
                    {
                        "__REALTIME_TIMESTAMP": str(item.get("__REALTIME_TIMESTAMP", "")),
                        "PRIORITY": str(item.get("PRIORITY", "")),
                        "_SYSTEMD_UNIT": str(item.get("_SYSTEMD_UNIT", "")),
                        "MESSAGE": self.sanitizer.sanitize(str(item.get("MESSAGE", ""))),
                    }
                )
            except (json.JSONDecodeError, TypeError):
                skipped += 1
        times = [
            int(item["__REALTIME_TIMESTAMP"])
            for item in entries
            if item["__REALTIME_TIMESTAMP"].isdigit()
        ]
        priorities: dict[str, int] = {}
        for item in entries:
            priority = item["PRIORITY"] or "unknown"
            priorities[priority] = priorities.get(priority, 0) + 1
        raw = "\n".join(json.dumps(item, separators=(",", ":")) for item in entries)
        limitations = []
        if skipped:
            limitations.append(f"Skipped {skipped} malformed journal entries")
        if capture.truncated:
            limitations.append("Journal capture capped at 256 KiB")
        facts = JournalFacts(
            unit=request.unit,
            entries=len(entries),
            priority_counts=priorities,
            first_realtime_usec=min(times) if times else None,
            last_realtime_usec=max(times) if times else None,
            captured_bytes=len(raw.encode()),
            truncated=capture.truncated,
        )
        return Collected(facts, raw, tuple(limitations), truncated=capture.truncated)


class ProbeRegistry:
    """Explicit Strategy registry; no dynamic loading or model-supplied code."""

    def __init__(self, probes: tuple[LinuxProbe, ...]) -> None:
        self._probes = {probe.name: probe for probe in probes}
        if len(self._probes) != len(probes):
            raise ValueError("Duplicate probe registration")

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._probes))

    async def collect(self, request: ProbeRequest) -> Observation:
        return await self._probes[request.name].collect(request)


def default_registry(
    target_id: str,
    proc: Path = Path("/proc"),
    cgroup: Path = Path("/sys/fs/cgroup"),
    lab_cgroup: Path = DEFAULT_RUNTIME_CONFIG.lab_cgroup,
    lab_mount: Path = DEFAULT_RUNTIME_CONFIG.lab_mount,
) -> ProbeRegistry:
    cgroup_root = cgroup.resolve()
    self_relative = parse_self_cgroup(read_bounded(proc / "self/cgroup", 16 * 1024))
    self_cgroup = (cgroup_root / self_relative.lstrip("/")).resolve()
    if not self_cgroup.is_relative_to(cgroup_root):
        raise ValueError("Self cgroup escaped the configured cgroup root")
    probes: tuple[LinuxProbe, ...] = (
        CpuPressureProbe(target_id, proc, self_cgroup),
        ProcessRankingProbe(target_id, proc),
        MemoryPressureProbe(target_id, proc),
        CgroupMemoryProbe(target_id, proc, {"self": self_cgroup, "lab": lab_cgroup}),
        FilesystemProbe(target_id, proc, {"root": Path("/"), "lab": lab_mount}),
        JournalProbe(target_id, proc, SystemdJournalReader(), RegexSanitizer()),
    )
    return ProbeRegistry(probes)
