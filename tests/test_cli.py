from io import StringIO
from pathlib import Path

from rich.console import Console

import oncall.cli as cli


def test_professional_summary_keeps_raw_capture_out_of_terminal(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False, width=100))
    state = {
        "investigation_id": "a" * 32,
        "mode": "openai",
        "probe_calls": 1,
        "captured_bytes": 1234,
        "report": {
            "outcome": "completed",
            "summary": "CPU pressure is cgroup-local.",
            "claims": [
                {
                    "text": "The cgroup reached its quota.",
                    "evidence_ids": ["b" * 32],
                }
            ],
            "limitations": ["Short sample."],
            "next_steps": ["Repeat the sample."],
        },
        "evidence": [
            {
                "request": {"name": "sample_cpu_pressure"},
                "status": "ok",
                "artifact_bytes": 1234,
                "evidence_id": "b" * 32,
                "raw": "must not appear in the terminal",
            }
        ],
    }

    cli.show_result(state, Path("report.md"), Path("report.json"), 4.2)

    rendered = output.getvalue()
    assert "Investigation complete" in rendered
    assert "CPU pressure is cgroup-local" in rendered
    assert "sample_cpu_pressure" in rendered
    assert "report.md" in rendered
    assert "must not appear" not in rendered


def test_continuation_summary_discloses_parent_and_historical_evidence(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False, width=100))
    parent_id = "a" * 32
    historical_id = "b" * 32
    current_id = "c" * 32
    state = {
        "investigation_id": "d" * 32,
        "parent_investigation_id": parent_id,
        "mode": "openai",
        "probe_calls": 1,
        "captured_bytes": 12,
        "report": {
            "outcome": "completed",
            "summary": "The earlier condition has cleared.",
            "claims": [
                {
                    "text": "CPU pressure existed in the parent run.",
                    "evidence_ids": [historical_id],
                    "evidence_scope": "historical",
                },
                {
                    "text": "CPU pressure is absent now.",
                    "evidence_ids": [current_id],
                    "evidence_scope": "current",
                },
            ],
            "limitations": ["The current sample is brief."],
            "next_steps": ["Repeat if symptoms recur."],
        },
        "evidence": [
            {
                "request": {"name": "rank_processes"},
                "status": "ok",
                "artifact_bytes": 12,
                "evidence_id": current_id,
            }
        ],
        "continuation": {
            "historical_evidence": [
                {
                    "request": {"name": "sample_cpu_pressure"},
                    "status": "ok",
                    "artifact_bytes": 34,
                    "evidence_id": historical_id,
                    "evidence_scope": "historical",
                }
            ]
        },
    }

    cli.show_result(state, Path("report.md"), Path("report.json"), 5.0)

    rendered = output.getvalue()
    assert parent_id in rendered
    assert "Evidence (historical)" in rendered
    assert "Evidence (current)" in rendered
    assert "sample_cpu_pressure" in rendered
    assert "rank_processes" in rendered


def test_failure_detail_counts_probe_errors():
    state = {
        "events": [
            {"kind": "probe_failed", "payload": {"type": "ConnectError"}},
            {"kind": "probe_failed", "payload": {"type": "ConnectError"}},
            {"kind": "cancelled", "payload": {}},
        ]
    }

    assert cli.failure_detail(state) == "\nProbe failures: ConnectError × 2"
