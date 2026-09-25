from io import StringIO
from pathlib import Path

import httpx
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

    cli.show_result(state, Path("report.md"), Path("report.json"), 4.2, verbose=True)

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

    cli.show_result(state, Path("report.md"), Path("report.json"), 5.0, verbose=True)

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


def test_default_result_focuses_on_diagnosis_without_runtime_metadata(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False, width=100))
    evidence_id = "b" * 32
    state = {
        "investigation_id": "a" * 32,
        "mode": "openai",
        "probe_calls": 4,
        "captured_bytes": 1528,
        "report": {
            "outcome": "completed",
            "summary": "The lab mount is full and caused the write failure.",
            "claims": [
                {
                    "text": "The lab mount is 99.6% used.",
                    "evidence_ids": [evidence_id],
                }
            ],
            "limitations": ["The observation window was brief."],
            "next_steps": ["Remove the disposable fill file."],
        },
        "evidence": [
            {
                "request": {"name": "inspect_filesystem"},
                "status": "ok",
                "artifact_bytes": 1528,
                "evidence_id": evidence_id,
            }
        ],
    }

    cli.show_result(state, Path("report.md"), Path("report.json"), 24.9)

    rendered = output.getvalue()
    assert "The lab mount is full" in rendered
    assert "The lab mount is 99.6% used" in rendered
    assert "The observation window was brief" in rendered
    assert "Remove the disposable fill file" in rendered
    assert "Detailed report: report.md" in rendered
    for hidden in (
        "Investigation complete",
        "Provider",
        "Elapsed",
        "Probe calls",
        "Captured",
        evidence_id[:8],
        "inspect_filesystem",
        "report.json",
        "a" * 32,
    ):
        assert hidden not in rendered


def test_interactive_session_continues_until_new(monkeypatch):
    requests: list[str] = []
    prompts: list[tuple[str, str, str | None]] = []
    run_ids = iter(("root-run", "child-run", "new-root"))

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={"investigation_id": next(run_ids)},
            request=request,
        )

    def execute(
        _connection,
        run,
        prompt,
        _display_message,
        parent_id=None,
        **_options,
    ):
        prompts.append((run, prompt, parent_id))
        return {"investigation_id": run, "status": "completed"}

    monkeypatch.setattr(cli, "execute_investigation", execute)
    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://broker"
    ) as connection:
        session = cli.InteractiveSession(connection)
        session._investigate("Disk writes are failing")
        session._investigate("Is it still happening?")
        session._command("/new")
        session._investigate("Investigate CPU pressure")

    assert requests == [
        "/admin/start",
        "/admin/runs/root-run/continue",
        "/admin/start",
    ]
    assert prompts[0] == ("root-run", "Disk writes are failing", None)
    assert prompts[1][0] == "child-run"
    assert prompts[1][2] == "root-run"
    assert "Prior-run evidence is historical" in prompts[1][1]
    assert "Is it still happening?" in prompts[1][1]
    assert prompts[2] == ("new-root", "Investigate CPU pressure", None)
