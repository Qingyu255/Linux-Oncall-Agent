import pytest
from pydantic import ValidationError

from oncall.domain import utcnow
from oncall.evaluation import HumanReview, ScenarioTruth, TrialManifest, TrialScorer


def test_evaluator_separates_deterministic_gates_from_model_quality():
    now = utcnow()
    evidence_id = "a" * 32
    state = {
        "probe_calls": 1,
        "captured_bytes": 20,
        "evidence": [
            {
                "evidence_id": evidence_id,
                "target_id": "target",
                "boot_id": "boot",
                "status": "ok",
                "facts": {"kind": "cpu", "host_busy_pct": 90.0},
            }
        ],
        "report": {
            "outcome": "inconclusive",
            "claims": [
                {
                    "text": "CPU sampled",
                    "evidence_ids": [evidence_id],
                    "fact_fields": ["host_busy_pct"],
                }
            ],
        },
    }
    manifest = TrialManifest(
        trial_id="b" * 32,
        investigation_id="run",
        scenario="cpu",
        provider_mode="fixture",
        model="fixture",
        target_id="target",
        boot_id="boot",
        seed=1,
        started_at=now,
        completed_at=now,
        max_probe_calls=20,
        max_capture_bytes=10 * 1024 * 1024,
        timeout_seconds=180,
        source_revision="test",
        policy_version="3",
        cleanup_status="passed",
    )
    truth = ScenarioTruth(
        scenario="cpu",
        setup_ready=True,
        expected_outcome="inconclusive",
        required_fact_fields=("cpu.host_busy_pct",),
    )
    score = TrialScorer().score(state, manifest, truth, HumanReview())
    assert score.deterministic_gate == "pass"
    assert score.diagnostic_success == "not_applicable"
    assert score.evidence_completeness == 1


def test_evaluator_keeps_failed_setup_and_missing_evidence_in_denominator():
    now = utcnow()
    state = {"evidence": [], "report": None}
    manifest = TrialManifest(
        trial_id="c" * 32,
        investigation_id="run",
        scenario="filesystem",
        provider_mode="openai",
        model="model",
        target_id="target",
        boot_id="boot",
        seed=2,
        started_at=now,
        completed_at=now,
        max_probe_calls=20,
        max_capture_bytes=10,
        timeout_seconds=180,
        source_revision="test",
        policy_version="3",
        cleanup_status="failed",
    )
    truth = ScenarioTruth(
        scenario="filesystem",
        setup_ready=False,
        expected_outcome="completed",
        required_fact_fields=("filesystem.available_bytes",),
    )
    score = TrialScorer().score(state, manifest, truth, HumanReview())
    assert score.deterministic_gate == "fail"
    assert score.missing_fact_fields == ("filesystem.available_bytes",)


def test_unavailable_control_can_score_without_observations():
    now = utcnow()
    state = {
        "evidence": [],
        "report": {"outcome": "inconclusive", "claims": []},
    }
    manifest = TrialManifest(
        trial_id="d" * 32,
        investigation_id="run",
        scenario="unavailable",
        provider_mode="openai",
        model="model",
        target_id="target",
        boot_id="boot",
        seed=3,
        started_at=now,
        completed_at=now,
        max_probe_calls=20,
        max_capture_bytes=10,
        timeout_seconds=180,
        source_revision="test",
        policy_version="3",
        cleanup_status="not_required",
    )
    truth = ScenarioTruth(
        scenario="unavailable",
        setup_ready=True,
        expected_outcome="inconclusive",
    )
    score = TrialScorer().score(state, manifest, truth, HumanReview())
    assert score.deterministic_gate == "pass"
    assert score.evidence_completeness == 1
    assert score.target_identity_consistent


def test_available_scenario_requires_evidence_fields():
    with pytest.raises(ValidationError):
        ScenarioTruth(
            scenario="cpu",
            setup_ready=True,
            expected_outcome="completed",
        )
