import json
from io import StringIO

from rich.console import Console

import oncall.cli as cli
from oncall.harness_progress import HarnessProgressAdapter, progress_message
from oncall.progress_projection import bounded_assistant_response, facts_projection


def notification(kind: str, data: dict[str, object]) -> dict[str, object]:
    return {"event": {"type": kind, "data": data}}


def result_event(
    call_id: str, value: dict[str, object], *, failed: bool = False
) -> dict[str, object]:
    return notification(
        "tool/result",
        {
            "message": {
                "source": {"kind": "tool", "callId": call_id},
                "isError": failed,
                "content": [{"type": "text", "text": json.dumps(value)}],
            }
        },
    )


def cpu_evidence() -> dict[str, object]:
    return {
        "schema_version": 3,
        "evidence_id": "a" * 32,
        "investigation_id": "b" * 32,
        "target_id": "local-target",
        "boot_id": "boot-1",
        "probe_version": "3",
        "parser_version": "3",
        "request": {"name": "sample_cpu_pressure", "duration_seconds": 2},
        "started_at": "2026-09-25T00:00:00Z",
        "completed_at": "2026-09-25T00:00:02Z",
        "duration_ms": 2000.0,
        "status": "ok",
        "error_code": None,
        "facts": {
            "kind": "cpu",
            "logical_cpus": 10,
            "host_busy_pct": 18.4,
            "host_iowait_pct": 0.2,
            "host_steal_pct": 0.0,
            "probe_cgroup_cpu_cores_used": 0.92,
            "probe_cgroup_quota_cores": 1.0,
            "probe_cgroup_throttled_usec_delta": 847000,
            "load1": 1.2,
        },
        "limitations": [],
        "artifact_id": "c" * 32,
        "artifact_sha256": "d" * 64,
        "artifact_bytes": 1536,
        "artifact_truncated": False,
    }


def test_projects_safe_probe_parameters_and_typed_evidence():
    emitted: list[dict[str, object]] = []
    adapter = HarnessProgressAdapter(emitted.append)
    adapter.notification(
        "session.event",
        notification(
            "tool/call",
            {
                "callId": "call-1",
                "name": "mcp__oncall__sample_cpu_pressure",
                "arguments": '{"duration_seconds":2,"secret":"must-not-cross"}',
            },
        ),
    )
    adapter.notification("session.event", result_event("call-1", cpu_evidence()))

    assert emitted[0] == {
        "protocol": "oncall-progress-v1",
        "kind": "tool_started",
        "tool": "sample_cpu_pressure",
        "duration_seconds": 2,
    }
    assert emitted[1]["quality"] == "ok"
    assert emitted[1]["evidence_id"] == "aaaaaaaa"
    assert emitted[1]["artifact_bytes"] == 1536
    assert emitted[1]["facts"] == {
        "kind": "cpu",
        "host_busy_pct": 18.4,
        "host_iowait_pct": 0.2,
        "probe_cgroup_cpu_cores_used": 0.92,
        "probe_cgroup_quota_cores": 1.0,
        "probe_cgroup_throttled_usec_delta": 847000,
    }
    assert "secret" not in str(emitted)
    assert progress_message(emitted[1]) == (
        "green",
        "✓",
        "Sampling CPU pressure captured · quality ok · 2.00s · 1.5 KiB · evidence aaaaaaaa\n"
        "          host 18.4% busy · probe 0.92/1.00 CPU · 847 ms throttled",
    )


def test_assistant_content_is_hidden_while_usage_is_counted():
    emitted: list[dict[str, object]] = []
    adapter = HarnessProgressAdapter(emitted.append)
    adapter.notification(
        "session.event",
        notification(
            "assistant/message",
            {
                "message": {"content": [{"type": "reasoning", "text": "private"}]},
                "usage": {"inputTokens": 1200, "outputTokens": 80},
            },
        ),
    )
    adapter.notification(
        "session.event",
        notification("tool/call", {"callId": "x", "name": "bash", "arguments": "{}"}),
    )

    assert emitted == [
        {
            "protocol": "oncall-progress-v1",
            "kind": "model_response",
            "request": 1,
            "input_tokens": 1200,
            "output_tokens": 80,
        },
        {"protocol": "oncall-progress-v1", "kind": "tool_started", "tool": "harness_tool"},
    ]
    assert "private" not in str(emitted)
    assert progress_message(emitted[0]) == (
        "magenta",
        "◆",
        "Model response 1 received · 1,200 input tokens · 80 output tokens",
    )


def test_final_assistant_text_crosses_only_through_bounded_response_event():
    emitted: list[dict[str, object]] = []
    adapter = HarnessProgressAdapter(emitted.append)
    adapter.notification(
        "session.event",
        notification(
            "assistant/message",
            {
                "message": {
                    "content": [
                        {"type": "reasoning", "text": "private chain of thought"},
                        {"type": "text", "text": "I can investigate Linux incidents.\x1b[31m"},
                    ]
                }
            },
        ),
    )
    adapter.notification(
        "session.event",
        notification("turn/end", {"reason": {"kind": "completed"}}),
    )

    assert emitted[-2] == {
        "protocol": "oncall-progress-v1",
        "kind": "assistant_response",
        "text": "I can investigate Linux incidents.[31m",
    }
    assert "private chain of thought" not in str(emitted)
    assert bounded_assistant_response("safe\x00\x1b[bold]") == "safe[bold]"
    assert cli.handle_harness_line(json.dumps(emitted[-2]), 0) == (
        "I can investigate Linux incidents.[31m"
    )


def test_zero_usage_is_omitted_and_tuple_process_facts_are_projected():
    emitted: list[dict[str, object]] = []
    adapter = HarnessProgressAdapter(emitted.append)
    adapter.notification(
        "session.event",
        notification("assistant/message", {"usage": {"inputTokens": 0, "outputTokens": 0}}),
    )

    assert emitted == [{"protocol": "oncall-progress-v1", "kind": "model_response", "request": 1}]
    projected = facts_projection(
        {
            "kind": "processes",
            "processes": (
                {
                    "name": "python\n[bold]unsafe[/]",
                    "pid": 32,
                    "cpu_pct_one_core": 92.0,
                    "rss_bytes": 1024,
                },
            ),
        }
    )
    assert projected == {
        "kind": "processes",
        "top_name": "python??bold?unsafe?/?",
        "top_pid": 32,
        "top_cpu_pct": 92.0,
        "top_rss_bytes": 1024,
    }
    assert facts_projection(projected) == projected
    rendered = progress_message(
        {
            "protocol": "oncall-progress-v1",
            "kind": "tool_finished",
            "tool": "rank_processes",
            "failed": False,
            "facts": projected,
        }
    )
    assert rendered is not None
    assert rendered[2].endswith(
        "top python??bold?unsafe?/? PID 32 · 92.0% of one CPU · 1.0 KiB RSS"
    )


def test_hypothesis_transition_and_continuation_relationship_are_projected():
    emitted: list[dict[str, object]] = []
    adapter = HarnessProgressAdapter(emitted.append)
    hypothesis = {
        "hypothesis_id": "cgroup_pressure",
        "version": 2,
        "claim": "content must not cross",
        "status": "supported",
        "supporting_evidence_ids": ["a" * 32],
        "contradicting_evidence_ids": [],
        "unresolved_questions": ["content must not cross"],
    }
    adapter.notification(
        "session.event",
        notification(
            "tool/call",
            {
                "callId": "hypothesis",
                "name": "mcp__oncall__update_hypothesis",
                "arguments": json.dumps({"hypothesis": hypothesis}),
            },
        ),
    )
    adapter.notification("session.event", result_event("hypothesis", hypothesis))
    adapter.notification(
        "session.event",
        notification(
            "tool/call",
            {
                "callId": "state",
                "name": "mcp__oncall__get_investigation_state",
                "arguments": "{}",
            },
        ),
    )
    adapter.notification(
        "session.event",
        result_event(
            "state",
            {
                "probe_calls": 3,
                "captured_bytes": 4096,
                "remaining_seconds": 93.4,
                "continuation": {
                    "parent_investigation_id": "b" * 32,
                    "target_relationship": "same_boot",
                    "historical_evidence": [{}, {}],
                    "private": "must not cross",
                },
            },
        ),
    )

    assert "content must not cross" not in str(emitted)
    assert "private" not in str(emitted)
    assert progress_message(emitted[1]) == (
        "green",
        "✓",
        "Hypothesis cgroup_pressure v2 → supported · 1 supporting · 0 contradicting · "
        "1 open questions",
    )
    assert progress_message(emitted[3]) == (
        "green",
        "✓",
        "Reviewing captured evidence completed · 3 probes · 4.0 KiB captured · 93s left\n"
        "          target relationship: same boot · parent bbbbbbbb · 2 historical evidence",
    )


def test_report_rejection_is_canonical_and_unknown_reason_is_hidden():
    emitted: list[dict[str, object]] = []
    adapter = HarnessProgressAdapter(emitted.append)
    for call_id in ("known", "unknown"):
        adapter.notification(
            "session.event",
            notification(
                "tool/call",
                {
                    "callId": call_id,
                    "name": "mcp__oncall__submit_report",
                    "arguments": "{}",
                },
            ),
        )
    known = result_event(
        "known", {"error": "Completed findings must name cited fact fields"}, failed=True
    )
    known["event"]["data"]["error"] = {  # type: ignore[index]
        "reason": "Completed findings must name cited fact fields"
    }
    unknown = result_event("unknown", {"error": "credential=secret"}, failed=True)
    unknown["event"]["data"]["error"] = {"reason": "credential=secret"}  # type: ignore[index]
    adapter.notification("session.event", known)
    adapter.notification("session.event", unknown)

    assert emitted[-2]["rejection"] == "Completed findings must name cited fact fields"
    assert emitted[-1]["rejection"] == "Report rejected by policy"
    assert "credential" not in str(emitted)
    assert progress_message(emitted[-2]) == (
        "red",
        "✗",
        "Report rejected: Completed findings must name cited fact fields",
    )


def test_accepted_report_retains_only_safe_shape_counts():
    emitted: list[dict[str, object]] = []
    adapter = HarnessProgressAdapter(emitted.append)
    report = {
        "outcome": "completed",
        "summary": "must not cross",
        "claims": [{"text": "must not cross"}],
        "alternatives": ["must not cross"],
        "limitations": ["must not cross"],
        "next_steps": ["must not cross"],
    }
    adapter.notification(
        "session.event",
        notification(
            "tool/call",
            {
                "callId": "report",
                "name": "mcp__oncall__submit_report",
                "arguments": json.dumps({"report": report}),
            },
        ),
    )
    adapter.notification(
        "session.event",
        result_event(
            "report",
            {"accepted": True, "investigation_id": "a" * 32, "outcome": "completed"},
        ),
    )

    assert "must not cross" not in str(emitted)
    assert progress_message(emitted[-1]) == (
        "green",
        "✓",
        "Report accepted · completed · 1 claims · 1 alternatives · 1 limitations · 1 next steps",
    )


def test_cli_ignores_non_protocol_harness_output(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False, width=100))
    monkeypatch.setattr(cli.time, "monotonic", lambda: 12.0)

    cli.handle_harness_line('{"reasoning":"must stay hidden"}\n', started=10.0)
    cli.handle_harness_line("runtime diagnostic with a token\n", started=10.0)
    cli.handle_harness_line(
        '{"protocol":"oncall-progress-v1","kind":"analysis_started"}\n',
        started=10.0,
        verbose=True,
    )

    rendered = output.getvalue()
    assert "Analyzing the symptom" in rendered
    assert "reasoning" not in rendered
    assert "token" not in rendered
