import asyncio

import httpx
import pytest
from pydantic import ValidationError

import oncall.target as target_module
from oncall.domain import CpuFacts, Observation, ProbeRequest, utcnow


class CountingRegistry:
    capabilities = ("sample_cpu_pressure",)

    def __init__(self):
        self.calls = 0
        self.active = 0
        self.maximum_active = 0

    async def collect(self, request):
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.03)
            raw = "bounded"
            return Observation(
                target_id="target",
                boot_id="boot",
                request=request,
                started_at=utcnow(),
                completed_at=utcnow(),
                duration_ms=1,
                facts=CpuFacts(
                    logical_cpus=1,
                    host_busy_pct=1,
                    host_iowait_pct=0,
                    host_steal_pct=0,
                    probe_cgroup_cpu_cores_used=0,
                    probe_cgroup_quota_cores=1,
                    probe_cgroup_throttled_usec_delta=0,
                    load1=0,
                ),
                raw=raw,
                raw_bytes=len(raw),
            )
        finally:
            self.active -= 1


@pytest.fixture
def target_app(monkeypatch):
    registry = CountingRegistry()
    monkeypatch.setattr(target_module, "default_registry", lambda _: registry)
    monkeypatch.setattr(target_module, "secret_file", lambda _: "x" * 32)
    return target_module.create_app(), registry


async def test_concurrent_idempotent_requests_execute_only_once(target_app):
    app, registry = target_app
    headers = {"Authorization": f"Bearer {'x' * 32}", "Idempotency-Key": "a" * 32}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://target"
    ) as client:
        first, second = await asyncio.gather(
            client.post("/v1/probe", json={"name": "sample_cpu_pressure"}, headers=headers),
            client.post("/v1/probe", json={"name": "sample_cpu_pressure"}, headers=headers),
        )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert registry.calls == 1


async def test_conflicting_inflight_id_is_rejected_and_parallelism_is_capped(target_app):
    app, registry = target_app
    authorization = {"Authorization": f"Bearer {'x' * 32}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://target"
    ) as client:
        first = asyncio.create_task(
            client.post(
                "/v1/probe",
                json={"name": "sample_cpu_pressure", "duration_seconds": 1},
                headers={**authorization, "Idempotency-Key": "b" * 32},
            )
        )
        await asyncio.sleep(0.005)
        conflict = await client.post(
            "/v1/probe",
            json={"name": "sample_cpu_pressure", "duration_seconds": 2},
            headers={**authorization, "Idempotency-Key": "b" * 32},
        )
        assert conflict.status_code == 409
        await first
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/probe",
                    json={"name": "sample_cpu_pressure"},
                    headers={**authorization, "Idempotency-Key": f"{number:032x}"},
                )
                for number in range(10, 14)
            )
        )
        denied = await client.post(
            "/v1/probe",
            json={"name": "sample_cpu_pressure"},
            headers={"Authorization": "Bearer wrong", "Idempotency-Key": "f" * 32},
        )
    assert all(response.status_code == 200 for response in responses)
    assert registry.maximum_active == 2
    assert denied.status_code == 401


async def test_client_cancellation_does_not_cancel_bounded_target_work(target_app):
    app, registry = target_app
    headers = {"Authorization": f"Bearer {'x' * 32}", "Idempotency-Key": "e" * 32}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://target"
    ) as client:
        request = asyncio.create_task(
            client.post("/v1/probe", json={"name": "sample_cpu_pressure"}, headers=headers)
        )
        await asyncio.sleep(0.005)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await asyncio.sleep(0.04)
        replay = await client.post(
            "/v1/probe", json={"name": "sample_cpu_pressure"}, headers=headers
        )
    assert replay.status_code == 200
    assert registry.calls == 1


def test_observation_rejects_untrusted_byte_count_and_wrong_fact_kind():
    request = ProbeRequest(name="sample_cpu_pressure")
    facts = CpuFacts(
        logical_cpus=1,
        host_busy_pct=1,
        host_iowait_pct=0,
        host_steal_pct=0,
        probe_cgroup_cpu_cores_used=0,
        probe_cgroup_quota_cores=1,
        probe_cgroup_throttled_usec_delta=0,
        load1=0,
    )
    with pytest.raises(ValidationError, match="byte count"):
        Observation(
            target_id="target",
            boot_id="boot",
            request=request,
            started_at=utcnow(),
            completed_at=utcnow(),
            duration_ms=1,
            facts=facts,
            raw="four",
            raw_bytes=1,
        )
