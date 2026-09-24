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


def observation(request, target_id="test-target", boot_id="boot-1"):
    return Observation(
        target_id=target_id,
        boot_id=boot_id,
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
    target_id: str = "test-target"
    boot_id: str = "boot-1"

    async def collect(self, request):
        self.count += 1
        await asyncio.sleep(self.delay)
        return observation(request, self.target_id, self.boot_id)


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


async def test_continuation_exposes_historical_context_and_scopes_claims(tmp_path):
    store = EvidenceStore(tmp_path)
    service = InvestigationService(Target(), store)
    parent = service.begin("test")
    prior = await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    service.submit(
        Report(
            outcome="completed",
            summary="Original diagnosis",
            claims=(
                Claim(
                    text="CPU was sampled in the original run",
                    evidence_ids=(prior.evidence_id,),
                    fact_fields=("host_busy_pct",),
                ),
            ),
            alternatives=("Expected load",),
            limitations=("Short sample",),
            next_steps=("Follow up explicitly",),
        )
    )

    child = service.begin("test", parent_id=parent)
    state = service.state()
    context = state["continuation"]
    assert state["parent_investigation_id"] == parent
    assert context["target_relationship"] == "unverified"
    assert context["historical_evidence"][0]["evidence_id"] == prior.evidence_id
    assert context["historical_evidence"][0]["evidence_scope"] == "historical"
    assert context["historical_evidence"][0]["age_seconds"] >= 0

    accepted = service.submit(
        Report(
            outcome="completed",
            summary="Retrospective answer",
            claims=(
                Claim(
                    text="The original run sampled host CPU",
                    evidence_ids=(prior.evidence_id,),
                    fact_fields=("host_busy_pct",),
                    evidence_scope="historical",
                ),
            ),
            alternatives=("The old sample was brief",),
            limitations=("This does not describe current conditions",),
            next_steps=("Collect a new sample for current conditions",),
        )
    )
    assert accepted["investigation_id"] == child
    assert not store.evidence(child)
    store.close()


async def test_current_continuation_claim_requires_fresh_child_evidence(tmp_path):
    store = EvidenceStore(tmp_path)
    target = Target()
    service = InvestigationService(target, store)
    parent = service.begin("test")
    prior = await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    service.submit(report(prior.evidence_id))
    child = service.begin("test", parent_id=parent)

    with pytest.raises(PolicyError, match="Current claim"):
        service.submit(report(prior.evidence_id))

    current = await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    assert service.state()["continuation"]["target_relationship"] == "same_boot"
    service.submit(report(current.evidence_id))
    assert service.store.state(child)["status"] == "inconclusive"
    store.close()


async def test_continuation_rejects_target_change_and_labels_reboot(tmp_path):
    store = EvidenceStore(tmp_path)
    target = Target()
    service = InvestigationService(target, store)
    parent = service.begin("test")
    prior = await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    service.submit(report(prior.evidence_id))

    service.begin("test", parent_id=parent)
    target.target_id = "another-target"
    with pytest.raises(PolicyError, match="target identity"):
        await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    assert not service.store.evidence(service.run_id)
    service.close_run(service.run_id)

    service.begin("test", parent_id=parent)
    target.target_id = "test-target"
    target.boot_id = "boot-2"
    await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    assert service.state()["continuation"]["target_relationship"] == "rebooted"
    events = store.events(service.run_id)
    assert any(
        event["kind"] == "target_identity_checked"
        and event["payload"]["relationship"] == "rebooted"
        for event in events
    )
    store.close()


async def test_continuation_can_page_historical_artifact_but_not_foreign_artifact(tmp_path):
    store = EvidenceStore(tmp_path)
    service = InvestigationService(Target(), store)
    parent = service.begin("test")
    prior = await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    service.submit(report(prior.evidence_id))
    service.begin("test", parent_id=parent)

    page = service.read_artifact(prior.artifact_id, 0, 100)
    assert page["text"] == "fixture raw observation"
    assert page["evidence_scope"] == "historical"

    foreign = store.create_run("test")
    other = store.add(foreign, observation(ProbeRequest(name="sample_cpu_pressure")))
    with pytest.raises(ValueError, match="lineage"):
        service.read_artifact(other.artifact_id, 0, 100)
    store.close()


async def test_close_is_idempotent_for_terminal_report(tmp_path):
    store = EvidenceStore(tmp_path)
    service = InvestigationService(Target(), store)
    run = service.begin("test")
    assert service.close_run(run)["status"] == "closed"
    assert service.close_run(run)["status"] == "closed"
    with pytest.raises(ValueError, match="terminal"):
        store.finish(run, "cancelled")
    store.close()


async def test_continuation_ttl_rejects_stale_parent(tmp_path):
    store = EvidenceStore(tmp_path)
    service = InvestigationService(Target(), store, continuation_ttl_seconds=60)
    parent = service.begin("test")
    prior = await service.probe(ProbeRequest(name="sample_cpu_pressure"))
    service.submit(report(prior.evidence_id))
    with store.lock, store.db:
        store.db.execute(
            "UPDATE runs SET finished='2000-01-01T00:00:00+00:00' WHERE id=?", (parent,)
        )

    with pytest.raises(PolicyError, match="TTL"):
        service.begin("test", parent_id=parent)
    store.close()
