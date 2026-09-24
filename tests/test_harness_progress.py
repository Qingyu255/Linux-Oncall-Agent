from io import StringIO

from rich.console import Console

import oncall.cli as cli
from oncall.harness_progress import HarnessProgressAdapter, progress_message


def test_progress_adapter_projects_tool_lifecycle_without_content():
    emitted: list[dict[str, object]] = []
    adapter = HarnessProgressAdapter(emitted.append)
    adapter.notification(
        "session.event",
        {
            "event": {
                "type": "tool/call",
                "data": {
                    "callId": "call-1",
                    "name": "mcp__oncall__sample_cpu_pressure",
                    "arguments": '{"secret":"must-not-cross-boundary"}',
                },
            }
        },
    )
    adapter.notification(
        "session.event",
        {
            "event": {
                "type": "tool/result",
                "data": {
                    "message": {
                        "source": {"kind": "tool", "callId": "call-1"},
                        "content": [
                            {
                                "type": "tool-result",
                                "isError": False,
                                "content": [{"type": "text", "text": "raw evidence"}],
                            }
                        ],
                    }
                },
            }
        },
    )

    assert emitted == [
        {
            "protocol": "oncall-progress-v1",
            "kind": "tool_started",
            "tool": "sample_cpu_pressure",
        },
        {
            "protocol": "oncall-progress-v1",
            "kind": "tool_finished",
            "tool": "sample_cpu_pressure",
            "failed": False,
        },
    ]
    assert "secret" not in str(emitted)
    assert "raw evidence" not in str(emitted)


def test_unknown_tool_and_reasoning_are_not_exposed():
    emitted: list[dict[str, object]] = []
    adapter = HarnessProgressAdapter(emitted.append)
    adapter.notification(
        "session.event",
        {
            "event": {
                "type": "assistant/message",
                "data": {"message": {"content": [{"type": "reasoning", "text": "private"}]}},
            }
        },
    )
    adapter.notification(
        "session.event",
        {"event": {"type": "tool/call", "data": {"callId": "x", "name": "bash"}}},
    )

    assert emitted == [
        {"protocol": "oncall-progress-v1", "kind": "tool_started", "tool": "harness_tool"}
    ]
    assert progress_message(emitted[0]) == ("yellow", "Using a sandbox utility")


def test_cli_ignores_non_protocol_harness_output(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False, width=100))
    monkeypatch.setattr(cli.time, "monotonic", lambda: 12.0)

    cli.handle_harness_line('{"reasoning":"must stay hidden"}\n', started=10.0)
    cli.handle_harness_line("runtime diagnostic with a token\n", started=10.0)
    cli.handle_harness_line(
        '{"protocol":"oncall-progress-v1","kind":"analysis_started"}\n', started=10.0
    )

    rendered = output.getvalue()
    assert "Analyzing the symptom" in rendered
    assert "reasoning" not in rendered
    assert "token" not in rendered
