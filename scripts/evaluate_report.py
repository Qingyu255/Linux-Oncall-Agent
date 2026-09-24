"""Package one investigation into a reproducible Day 4 evaluation bundle."""

import argparse
import json
from pathlib import Path

from oncall.evaluation import (
    EvaluationBundleWriter,
    HumanReview,
    ScenarioTruth,
    TrialManifest,
    TrialScorer,
    infer_interval,
    new_trial_id,
    source_revision,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-json", type=Path, required=True)
    parser.add_argument(
        "--scenario",
        choices=("cpu", "memory", "filesystem", "healthy", "unavailable"),
        required=True,
    )
    parser.add_argument("--required-fact", action="append", default=[])
    parser.add_argument("--target-id")
    parser.add_argument("--boot-id")
    parser.add_argument("--expected-outcome", choices=("completed", "inconclusive"), required=True)
    parser.add_argument("--model", default="unavailable")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--cleanup-status", choices=("passed", "failed", "not_required"), default="passed"
    )
    parser.add_argument("--setup-failed", action="store_true")
    parser.add_argument(
        "--diagnostic-success", choices=("pass", "fail", "pending"), default="pending"
    )
    parser.add_argument("--unsupported-claims", type=int)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/evaluation")
    args = parser.parse_args()

    state = json.loads(args.report_json.read_text())
    evidence = state.get("evidence", [])
    identities = {(item["target_id"], item["boot_id"]) for item in evidence}
    if len(identities) == 1:
        target_id, boot_id = identities.pop()
    elif not identities and args.target_id and args.boot_id:
        target_id, boot_id = args.target_id, args.boot_id
    else:
        raise RuntimeError(
            "Evaluation requires one evidence identity, or --target-id and --boot-id "
            "when an unavailable control has no evidence"
        )
    started, completed = infer_interval(state)
    manifest = TrialManifest(
        trial_id=new_trial_id(),
        investigation_id=state["investigation_id"],
        scenario=args.scenario,
        provider_mode=state.get("mode", "unknown"),
        model=args.model,
        target_id=target_id,
        boot_id=boot_id,
        seed=args.seed,
        started_at=started,
        completed_at=completed,
        max_probe_calls=20,
        max_capture_bytes=10 * 1024 * 1024,
        timeout_seconds=180,
        source_revision=source_revision(ROOT),
        policy_version="3",
        cleanup_status=args.cleanup_status,
    )
    truth = ScenarioTruth(
        scenario=args.scenario,
        setup_ready=not args.setup_failed,
        expected_outcome=args.expected_outcome,
        required_fact_fields=tuple(args.required_fact),
    )
    review = HumanReview(
        diagnostic_success=args.diagnostic_success,
        unsupported_claims=args.unsupported_claims,
    )
    score = TrialScorer().score(state, manifest, truth, review)
    markdown = args.report_json.with_name("report.md")
    output = EvaluationBundleWriter(args.output).write(
        state, manifest, truth, score, markdown if markdown.exists() else None
    )
    print(output)


if __name__ == "__main__":
    main()
