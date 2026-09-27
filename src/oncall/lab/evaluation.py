"""Deterministic evaluation artifacts; semantic diagnosis remains a human review."""

import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from oncall.domain import Value, utcnow

ScenarioName = Literal["cpu", "memory", "filesystem", "healthy", "unavailable"]
ReviewResult = Literal["pass", "fail", "pending", "not_applicable"]


class ScenarioTruth(Value):
    schema_version: int = 1
    scenario: ScenarioName
    setup_ready: bool
    expected_outcome: Literal["completed", "inconclusive"]
    required_fact_fields: tuple[str, ...] = ()
    cleanup_required: bool = True
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def evidence_requirements_match_scenario(self) -> "ScenarioTruth":
        if self.scenario != "unavailable" and not self.required_fact_fields:
            raise ValueError("Available scenarios require at least one fact field")
        return self


class TrialManifest(Value):
    schema_version: int = 1
    trial_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    investigation_id: str
    scenario: ScenarioName
    provider_mode: str
    model: str
    target_id: str
    boot_id: str
    seed: int = Field(ge=0)
    started_at: datetime
    completed_at: datetime
    max_probe_calls: int = Field(ge=1)
    max_capture_bytes: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)
    source_revision: str
    policy_version: str
    cleanup_status: Literal["passed", "failed", "not_required"]

    @model_validator(mode="after")
    def interval_is_ordered(self) -> "TrialManifest":
        if self.completed_at < self.started_at:
            raise ValueError("Trial completion precedes its start")
        return self


class HumanReview(Value):
    diagnostic_success: ReviewResult = "pending"
    unsupported_claims: int | None = Field(default=None, ge=0)
    notes: tuple[str, ...] = ()


class TrialScore(Value):
    schema_version: int = 1
    trial_id: str
    deterministic_gate: Literal["pass", "fail"]
    diagnostic_success: ReviewResult
    unsupported_claims: int | None
    evidence_completeness: float = Field(ge=0, le=1)
    citation_integrity: bool
    outcome_matches: bool
    target_identity_consistent: bool
    cleanup_passed: bool
    missing_fact_fields: tuple[str, ...]
    probe_calls: int = Field(ge=0)
    captured_bytes: int = Field(ge=0)
    wall_time_seconds: float = Field(ge=0)
    limitations: tuple[str, ...] = ()


class TrialScorer:
    """Scores mechanical gates without pretending to judge causal reasoning."""

    @staticmethod
    def _has_fact(evidence: list[dict[str, Any]], specification: str) -> bool:
        kind, separator, path = specification.partition(".")
        if not separator or not kind or not path:
            raise ValueError(f"Invalid fact specification: {specification}")
        for item in evidence:
            facts = item.get("facts")
            if item.get("status") != "ok" or not isinstance(facts, dict):
                continue
            if facts.get("kind") != kind:
                continue
            value: Any = facts
            for component in path.split("."):
                if not isinstance(value, dict) or component not in value:
                    break
                value = value[component]
            else:
                if value is not None:
                    return True
        return False

    @staticmethod
    def _citations_are_valid(state: dict[str, Any]) -> bool:
        evidence = {
            item.get("evidence_id"): item
            for item in state.get("evidence", [])
            if isinstance(item, dict)
        }
        report = state.get("report")
        if not isinstance(report, dict):
            return False
        for claim in report.get("claims", []):
            if not isinstance(claim, dict):
                return False
            references = claim.get("evidence_ids", [])
            if not references or any(reference not in evidence for reference in references):
                return False
            available: set[str] = set()
            for reference in references:
                facts = evidence[reference].get("facts")
                if isinstance(facts, dict):
                    available.update(facts)
            if any(field not in available for field in claim.get("fact_fields", [])):
                return False
        return True

    def score(
        self,
        state: dict[str, Any],
        manifest: TrialManifest,
        truth: ScenarioTruth,
        review: HumanReview,
    ) -> TrialScore:
        if truth.scenario != manifest.scenario:
            raise ValueError("Manifest and truth scenarios differ")
        evidence = [item for item in state.get("evidence", []) if isinstance(item, dict)]
        missing = tuple(
            field for field in truth.required_fact_fields if not self._has_fact(evidence, field)
        )
        completeness = (
            1 - len(missing) / len(truth.required_fact_fields)
            if truth.required_fact_fields
            else 1.0
        )
        identities = {
            (item.get("target_id"), item.get("boot_id"))
            for item in evidence
            if item.get("target_id") and item.get("boot_id")
        }
        identity_ok = identities == {(manifest.target_id, manifest.boot_id)} or (
            truth.scenario == "unavailable" and not evidence
        )
        citation_ok = self._citations_are_valid(state)
        report = state.get("report")
        outcome_ok = isinstance(report, dict) and report.get("outcome") == truth.expected_outcome
        cleanup_ok = not truth.cleanup_required or manifest.cleanup_status in {
            "passed",
            "not_required",
        }
        deterministic = all(
            (truth.setup_ready, not missing, identity_ok, citation_ok, outcome_ok, cleanup_ok)
        )
        diagnostic = review.diagnostic_success
        limitations = list(review.notes)
        if manifest.provider_mode == "fixture" and diagnostic == "pending":
            diagnostic = "not_applicable"
            limitations.append("Fixture mode is not model diagnostic evaluation")
        return TrialScore(
            trial_id=manifest.trial_id,
            deterministic_gate="pass" if deterministic else "fail",
            diagnostic_success=diagnostic,
            unsupported_claims=review.unsupported_claims,
            evidence_completeness=completeness,
            citation_integrity=citation_ok,
            outcome_matches=outcome_ok,
            target_identity_consistent=identity_ok,
            cleanup_passed=cleanup_ok,
            missing_fact_fields=missing,
            probe_calls=int(state.get("probe_calls", 0)),
            captured_bytes=int(state.get("captured_bytes", 0)),
            wall_time_seconds=(manifest.completed_at - manifest.started_at).total_seconds(),
            limitations=tuple(limitations),
        )


class EvaluationBundleWriter:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def write(
        self,
        state: dict[str, Any],
        manifest: TrialManifest,
        truth: ScenarioTruth,
        score: TrialScore,
        report_markdown: Path | None = None,
    ) -> Path:
        output = self.root / manifest.trial_id
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
        self._write_json(output / "run-manifest.json", manifest.model_dump(mode="json"))
        self._write_json(output / "scenario-truth.json", truth.model_dump(mode="json"))
        self._write_json(output / "evidence.json", state.get("evidence", []))
        self._write_json(output / "report.json", state.get("report"))
        self._write_json(output / "score.json", score.model_dump(mode="json"))
        events = state.get("events", [])
        (output / "events.jsonl").write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
        )
        os.chmod(output / "events.jsonl", 0o600)
        if report_markdown is not None:
            shutil.copyfile(report_markdown, output / "report.md")
        return output


def source_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or not revision:
        return "unavailable"
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return f"{revision}-dirty" if status.returncode == 0 and status.stdout else revision


def infer_interval(state: dict[str, Any]) -> tuple[datetime, datetime]:
    evidence = state.get("evidence", [])
    starts = [datetime.fromisoformat(item["started_at"]) for item in evidence]
    completions = [datetime.fromisoformat(item["completed_at"]) for item in evidence]
    now = utcnow()
    return (min(starts) if starts else now, max(completions) if completions else now)


def new_trial_id() -> str:
    return uuid.uuid4().hex
