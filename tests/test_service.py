import asyncio
import concurrent.futures
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from oncall.domain import (
    Claim,
    CpuFacts,
    FilesystemFacts,
    Observation,
    PolicyError,
    ProbeRequest,
    Report,
    utcnow,
)
from oncall.service import InvestigationService
from oncall.storage import EvidenceStore


def observation(request):
    return Observation(
        target_id="test-target",
        boot_id="boot-1",
        request=request,
        started_at=utcnow(),
        completed_at=utcnow(),
        duration_ms=1000,
        facts=CpuFacts(
            logical_cpus=2,
            host_busy_pct=50,
            host_iowait_pct=0,
            host_steal_pct=0,
            probe_cgroup_cpu_cores_used=1,
            probe_cgroup_quota_cores=1,
            probe_cgroup_throttled_usec_delta=10,
            load1=1,
        ),
        raw="fixture raw observation",
        raw_bytes=len("fixture raw observation"),
    )


@dataclass
class Target:
    delay: float = 0
    count: int = 0

    async def collect(self, request):
        self.count += 1
        await asyncio.sleep(self.delay)
        return observation(request)


@pytest.fixture
def service(tmp_path):
    store = EvidenceStore(tmp_path)
    value = InvestigationService(Target(), store, max_calls=2)
    value.begin("test")
    yield value
    store.close()


def report(ref):
    return Report(
        outcome="inconclusive",
        summary="A measured condition",
        claims=(Claim(text="CPU sampled", evidence_ids=(ref,)),),
        alternatives=("Expected work",),
        limitations=("Short sample",),
        next_steps=("Resample",),
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "shell", "command": "rm -rf /"},
        {"name": "sample_cpu_pressure", "duration_seconds": 1000},
        {"name": "sample_cpu_pressure", "duration_seconds": True},
        {"name": "rank_processes", "path": "../../etc/passwd"},
    ],
)
def test_typed_boundary_rejects_invalid_requests(payload):
    with pytest.raises(ValidationError):
        ProbeRequest.model_validate(payload)


async def test_citations_and_artifacts_are_scoped(service):
    ev = await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    with pytest.raises(PolicyError):
        service.submit(report("made-up"))
    assert (
        service.store.artifact(service.run_id, ev.artifact_id, 0, 100) == "fixture raw observation"
    )
    foreign = service.store.create_run("foreign")
    with pytest.raises(ValueError):
        service.store.artifact(foreign, ev.artifact_id, 0, 100)
    with pytest.raises(ValueError):
        service.store.artifact(service.run_id, "../../etc/passwd", 0, 100)
    service.submit(report(ev.evidence_id))
    assert service.state()["status"] == "inconclusive"
    with pytest.raises(PolicyError):
        await service.probe(ProbeRequest(name="sample_cpu_pressure"))


async def test_completed_claim_can_cite_fields_across_multiple_evidence_items(service):
    cpu = await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    fs_request = ProbeRequest(name="inspect_filesystem", mount_id="root")
    fs_observation = Observation(
        target_id="test-target",
        boot_id="boot-1",
        request=fs_request,
        started_at=utcnow(),
        completed_at=utcnow(),
        duration_ms=1,
        facts=FilesystemFacts(
            mount_id="root",
            filesystem_type="ext4",
            total_bytes=100,
            available_bytes=50,
            used_percent=50,
            total_inodes=10,
            available_inodes=5,
            probe_view_readonly=False,
        ),
        raw="filesystem",
        raw_bytes=10,
    )
    fs = service.store.add(service.run_id, fs_observation)
    accepted = service.submit(
        Report(
            outcome="completed",
            summary="Two scoped observations",
            claims=(
                Claim(
                    text="CPU and filesystem facts were observed",
                    evidence_ids=(cpu.evidence_id, fs.evidence_id),
                    fact_fields=("host_busy_pct", "available_bytes"),
                ),
            ),
            alternatives=("Expected load",),
            limitations=("Short interval",),
            next_steps=("Repeat",),
        )
    )
    assert accepted["outcome"] == "completed"


async def test_parallel_calls_cannot_overspend(service):
    results = await asyncio.gather(
        *(service.probe(ProbeRequest(name="sample_cpu_pressure")) for _ in range(4)),
        return_exceptions=True,
    )
    assert sum(isinstance(x, PolicyError) for x in results) == 2
    assert service.target.count == 2


async def test_cancel_stops_inflight_collection(service):
    service.target.delay = 30
    task = asyncio.create_task(service.probe(ProbeRequest(name="sample_cpu_pressure")))
    await asyncio.sleep(0.01)
    service.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert service.state()["status"] == "cancelled"
    assert not service.store.evidence(service.run_id)


async def test_expired_deadline_is_not_healthy(service):
    service.deadline = 0
    with pytest.raises(PolicyError, match="deadline"):
        await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    assert service.state()["status"] == "inconclusive"


async def test_corruption_detected(service):
    ev = await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    (service.store.artifacts / ev.artifact_id).write_text("changed")
    with pytest.raises(ValueError, match="integrity"):
        service.store.artifact(service.run_id, ev.artifact_id, 0, 100)


def test_recovery_marks_runs_interrupted(tmp_path):
    store = EvidenceStore(tmp_path)
    run = store.create_run("test")
    store.close()
    recovered = EvidenceStore(tmp_path)
    assert recovered.state(run)["status"] == "interrupted"
    recovered.close()


def test_store_supports_fastapi_worker_threads(tmp_path):
    store = EvidenceStore(tmp_path)
    run = store.create_run("test")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        states = list(executor.map(lambda _: store.state(run), range(4)))
    assert {state["status"] for state in states} == {"running"}
    store.close()
