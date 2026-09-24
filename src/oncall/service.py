"""Application policy and budget ownership, independent of harness SDKs."""

import asyncio
import time
from typing import Any, Protocol

from oncall.domain import Evidence, Hypothesis, Observation, PolicyError, ProbeRequest, Report
from oncall.storage import EvidenceStore


class TargetClient(Protocol):
    async def collect(self, request: ProbeRequest) -> Observation: ...


class InvestigationService:
    def __init__(
        self, target: TargetClient, store: EvidenceStore, max_calls: int = 20, timeout: float = 180
    ) -> None:
        self.target, self.store = target, store
        self.max_calls, self.timeout = max_calls, timeout
        self.run_id: str | None = None
        self.deadline = 0.0
        self.calls, self.bytes = 0, 0
        self.tasks: set[asyncio.Task[Any]] = set()
        self.slots = asyncio.Semaphore(2)

    def begin(self, mode: str) -> str:
        if self.run_id and self.store.state(self.run_id)["status"] == "running":
            raise PolicyError("An investigation is already running")
        if self.tasks:
            raise PolicyError("Previous probes are still stopping")
        self.run_id = self.store.create_run(mode)
        self.calls, self.bytes = 0, 0
        self.deadline = time.monotonic() + self.timeout
        self.store.event(
            self.run_id, "started", {"max_calls": self.max_calls, "timeout_seconds": self.timeout}
        )
        return self.run_id

    def active(self) -> str:
        if not self.run_id or self.store.state(self.run_id)["status"] != "running":
            raise PolicyError("No active investigation")
        if time.monotonic() >= self.deadline:
            self.store.finish(self.run_id, "inconclusive")
            raise PolicyError("Investigation deadline exceeded")
        return self.run_id

    async def probe(self, request: ProbeRequest) -> Evidence:
        run = self.active()
        if self.calls >= self.max_calls:
            raise PolicyError("Probe call budget exceeded")
        self.calls += 1  # No await between check and reservation in this event loop.
        self.store.event(run, "probe_allowed", request.model_dump())
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            async with asyncio.timeout(min(8, max(0.01, self.deadline - time.monotonic()))):
                async with self.slots:
                    self.active()
                    observation = await self.target.collect(request)
            self.active()
            captured = len(observation.raw.encode("utf-8"))
            if self.bytes + captured > 10 * 1024 * 1024:
                raise PolicyError("Investigation artifact budget exceeded")
            self.bytes += captured
            evidence = self.store.add(run, observation)
            self.store.event(run, "evidence_added", {"evidence_id": evidence.evidence_id})
            return evidence
        except BaseException as error:
            self.store.event(run, "probe_failed", {"type": type(error).__name__})
            raise
        finally:
            self.tasks.discard(task)

    def update_hypothesis(self, hypothesis: Hypothesis) -> Hypothesis:
        run = self.active()
        known = {item.evidence_id for item in self.store.evidence(run)}
        references = set(hypothesis.supporting_evidence_ids) | set(
            hypothesis.contradicting_evidence_ids
        )
        if not references <= known:
            raise PolicyError("Hypothesis cites unknown or foreign evidence")
        stored = self.store.update_hypothesis(run, hypothesis)
        self.store.event(
            run,
            "hypothesis_updated",
            {"hypothesis_id": stored.hypothesis_id, "version": stored.version},
        )
        return stored

    def submit(self, report: Report) -> dict[str, Any]:
        run = self.active()
        evidence = {x.evidence_id: x for x in self.store.evidence(run)}
        for claim in report.claims:
            if any(ref not in evidence for ref in claim.evidence_ids):
                raise PolicyError("Report cites unknown or foreign evidence")
            if report.outcome == "completed" and not claim.fact_fields:
                raise PolicyError("Completed findings must name cited fact fields")
            available_fields: set[str] = set()
            for ref in claim.evidence_ids:
                facts = evidence[ref].facts
                if facts is not None:
                    available_fields.update(facts.model_dump())
            if any(field not in available_fields for field in claim.fact_fields):
                raise PolicyError("Report cites a fact field absent from its cited evidence")
        boot_ids = {x.boot_id for x in evidence.values()}
        if len(boot_ids) > 1:
            raise PolicyError("Target boot identity changed; start a fresh investigation")
        target_ids = {x.target_id for x in evidence.values()}
        if len(target_ids) > 1:
            raise PolicyError("Evidence spans multiple targets")
        cited = {ref for claim in report.claims for ref in claim.evidence_ids}
        if report.outcome == "completed" and any(
            item.status != "ok" and item.evidence_id in cited for item in evidence.values()
        ):
            raise PolicyError("Completed report relies on limited or unavailable evidence")
        self.store.finish(run, report.outcome, report)
        self.store.event(run, "report_accepted", {"outcome": report.outcome})
        return {"accepted": True, "investigation_id": run, "outcome": report.outcome}

    def cancel(self) -> None:
        if self.run_id and self.store.state(self.run_id)["status"] == "running":
            self.store.finish(self.run_id, "cancelled")
            self.store.event(self.run_id, "cancelled", {})
            for task in list(self.tasks):
                task.cancel()

    def state(self) -> dict[str, Any]:
        if not self.run_id:
            raise PolicyError("No investigation")
        return {
            **self.store.state(self.run_id),
            "probe_calls": self.calls,
            "captured_bytes": self.bytes,
            "remaining_capture_bytes": max(0, 10 * 1024 * 1024 - self.bytes),
            "remaining_seconds": max(0, self.deadline - time.monotonic()),
        }
