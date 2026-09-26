"""Immutable wire/domain values. No network, harness, or operating-system effects."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


ProbeName = Literal[
    "sample_cpu_pressure",
    "rank_processes",
    "inspect_memory_pressure",
    "inspect_cgroup_memory",
    "inspect_filesystem",
    "query_service_journal",
]


class ProbeRequest(Value):
    """One closed request schema; conditional fields prevent hidden authority."""

    name: ProbeName
    duration_seconds: Annotated[int, Field(strict=True, ge=1, le=5)] = 2
    limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 5
    scope_id: Literal["self", "lab"] | None = None
    mount_id: Literal["root", "lab"] | None = None
    unit: Literal["oncall-target.service", "oncall-lab-workload.service"] | None = None
    since_seconds: Annotated[int, Field(strict=True, ge=1, le=900)] = 300

    @model_validator(mode="after")
    def fields_match_probe(self) -> "ProbeRequest":
        if self.name == "rank_processes" and self.limit > 20:
            raise ValueError("Process ranking is limited to 20 entries")
        if self.name == "inspect_cgroup_memory" and self.scope_id is None:
            raise ValueError("Cgroup scope_id is required")
        if self.name == "inspect_filesystem" and self.mount_id is None:
            raise ValueError("Filesystem mount_id is required")
        if self.name == "query_service_journal" and self.unit is None:
            raise ValueError("Journal unit is required")
        if self.name != "inspect_cgroup_memory" and self.scope_id is not None:
            raise ValueError("scope_id is not accepted by this probe")
        if self.name != "inspect_filesystem" and self.mount_id is not None:
            raise ValueError("mount_id is not accepted by this probe")
        if self.name != "query_service_journal" and self.unit is not None:
            raise ValueError("unit is not accepted by this probe")
        return self


class PressureFacts(Value):
    avg10: float
    avg60: float
    avg300: float
    total_usec: int


class CpuFacts(Value):
    kind: Literal["cpu"] = "cpu"
    logical_cpus: int
    host_busy_pct: float
    host_iowait_pct: float
    host_steal_pct: float
    probe_cgroup_cpu_cores_used: float | None
    probe_cgroup_quota_cores: float | None
    probe_cgroup_throttled_usec_delta: int | None
    load1: float
    scope: str = (
        "host CPU counters describe the shared kernel; cgroup counters describe the probe process "
        "and any co-located processes in that cgroup"
    )


class ProcessFacts(Value):
    pid: int
    start_ticks: int
    name: str
    cpu_pct_one_core: float
    rss_bytes: int


class ProcessRanking(Value):
    kind: Literal["processes"] = "processes"
    processes: tuple[ProcessFacts, ...]
    scanned: int
    disappeared: int
    scan_limited: bool


class VmstatFacts(Value):
    pgfault: int | None
    pgmajfault: int | None
    pswpin: int | None
    pswpout: int | None
    oom_kill: int | None


class MemoryFacts(Value):
    kind: Literal["memory"] = "memory"
    mem_total_bytes: int
    mem_available_bytes: int
    swap_total_bytes: int
    swap_free_bytes: int
    anonymous_bytes: int | None
    file_cache_bytes: int | None
    vmstat: VmstatFacts
    pressure_some: PressureFacts | None
    pressure_full: PressureFacts | None
    scope: str = "host memory and pressure counters describe the shared kernel"


class CgroupMemoryEvents(Value):
    low: int | None = None
    high: int | None = None
    max: int | None = None
    oom: int | None = None
    oom_kill: int | None = None


class CgroupMemoryFacts(Value):
    kind: Literal["cgroup_memory"] = "cgroup_memory"
    scope_id: Literal["self", "lab"]
    current_bytes: int
    max_bytes: int | None
    high_bytes: int | None
    swap_current_bytes: int | None
    swap_max_bytes: int | None
    events_before: CgroupMemoryEvents
    events_after: CgroupMemoryEvents
    oom_delta: int | None
    oom_kill_delta: int | None
    scope: str = "cgroup v2 memory counters for the configured opaque scope"


class FilesystemFacts(Value):
    kind: Literal["filesystem"] = "filesystem"
    mount_id: Literal["root", "lab"]
    filesystem_type: str
    total_bytes: int
    available_bytes: int
    used_percent: float
    total_inodes: int
    available_inodes: int
    probe_view_readonly: bool
    scope: str = "statvfs values use space available to the target service user"


class JournalFacts(Value):
    kind: Literal["journal"] = "journal"
    unit: str
    entries: int
    priority_counts: dict[str, int]
    first_realtime_usec: int | None
    last_realtime_usec: int | None
    captured_bytes: int
    truncated: bool
    scope: str = "bounded, boot-scoped, sanitized systemd journal entries"


Facts = Annotated[
    CpuFacts | ProcessRanking | MemoryFacts | CgroupMemoryFacts | FilesystemFacts | JournalFacts,
    Field(discriminator="kind"),
]
ObservationStatus = Literal["ok", "partial", "unsupported", "denied", "output_limited"]


class Observation(Value):
    target_id: str
    boot_id: str
    request: ProbeRequest
    started_at: datetime
    completed_at: datetime
    duration_ms: Annotated[float, Field(ge=0)]
    status: ObservationStatus = "ok"
    error_code: str | None = None
    facts: Facts | None
    limitations: tuple[str, ...] = ()
    raw: str
    raw_bytes: Annotated[int, Field(ge=0, le=1024 * 1024)]
    raw_truncated: bool = False

    @model_validator(mode="after")
    def quality_matches_payload(self) -> "Observation":
        if self.completed_at < self.started_at:
            raise ValueError("Observation completion precedes its start")
        if self.raw_bytes != len(self.raw.encode("utf-8")):
            raise ValueError("Observation raw byte count does not match its payload")
        if self.status == "ok" and self.facts is None:
            raise ValueError("Successful observations require typed facts")
        expected_kind = {
            "sample_cpu_pressure": "cpu",
            "rank_processes": "processes",
            "inspect_memory_pressure": "memory",
            "inspect_cgroup_memory": "cgroup_memory",
            "inspect_filesystem": "filesystem",
            "query_service_journal": "journal",
        }[self.request.name]
        if self.facts is not None and self.facts.kind != expected_kind:
            raise ValueError("Observation facts do not match the requested capability")
        if self.status in {"unsupported", "denied"}:
            if self.facts is not None or self.error_code is None:
                raise ValueError("Unavailable observations require an error and no facts")
        if self.raw_truncated and self.status not in {"partial", "output_limited"}:
            raise ValueError("Truncated observations require a limited quality status")
        return self


class Evidence(Value):
    schema_version: int = 3
    evidence_id: str
    investigation_id: str
    target_id: str
    boot_id: str
    probe_version: str = "3"
    parser_version: str = "3"
    request: ProbeRequest
    started_at: datetime
    completed_at: datetime
    duration_ms: float
    status: ObservationStatus
    error_code: str | None
    facts: Facts | None
    limitations: tuple[str, ...]
    artifact_id: str
    artifact_sha256: str
    artifact_bytes: int
    artifact_truncated: bool


class Hypothesis(Value):
    hypothesis_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    version: int = Field(default=1, ge=1)
    claim: str = Field(min_length=1, max_length=2000)
    status: Literal["open", "supported", "weakened", "rejected"]
    supporting_evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)
    contradicting_evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)
    unresolved_questions: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def evidence_roles_are_consistent(self) -> "Hypothesis":
        supporting = set(self.supporting_evidence_ids)
        contradicting = set(self.contradicting_evidence_ids)
        if len(supporting) != len(self.supporting_evidence_ids) or len(contradicting) != len(
            self.contradicting_evidence_ids
        ):
            raise ValueError("Hypothesis evidence references must be unique")
        if supporting & contradicting:
            raise ValueError("Evidence cannot both support and contradict one hypothesis version")
        if self.status == "supported" and not supporting:
            raise ValueError("Supported hypotheses require supporting evidence")
        if self.status == "rejected" and not contradicting:
            raise ValueError("Rejected hypotheses require contradicting evidence")
        return self


class Claim(Value):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    fact_fields: tuple[str, ...] = Field(default=(), max_length=20)
    evidence_scope: Literal["current", "historical"] = "current"


class Report(Value):
    outcome: Literal["completed", "inconclusive"]
    summary: str = Field(min_length=1, max_length=2000)
    claims: tuple[Claim, ...] = Field(default=(), max_length=10)
    alternatives: tuple[str, ...] = Field(min_length=1, max_length=10)
    limitations: tuple[str, ...] = Field(min_length=1, max_length=10)
    next_steps: tuple[str, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def completed_reports_have_findings(self) -> Self:
        if self.outcome == "completed" and not self.claims:
            raise ValueError("Completed reports require at least one evidence-backed claim")
        return self


class PolicyError(ValueError):
    """An action was rejected before granting authority."""
