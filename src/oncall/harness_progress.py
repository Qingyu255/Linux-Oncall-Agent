"""Safe progress projection for DeepSeek Harness notifications.

The harness event stream contains model reasoning, prompts, tool arguments, and tool
results.  None of those are suitable for an operator terminal.  This adapter emits a
small allowlisted projection that describes lifecycle and tool activity without
copying event content across the sandbox boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

PROGRESS_PROTOCOL = "oncall-progress-v1"

TOOL_LABELS = {
    "sample_cpu_pressure": "Sampling CPU pressure",
    "rank_processes": "Ranking CPU-consuming processes",
    "inspect_memory_pressure": "Inspecting host memory pressure",
    "inspect_cgroup_memory": "Inspecting cgroup memory events",
    "inspect_filesystem": "Inspecting filesystem capacity",
    "query_service_journal": "Reading the bounded service journal",
    "get_investigation_state": "Reviewing captured evidence",
    "read_artifact": "Reading a bounded evidence page",
    "update_hypothesis": "Updating a competing hypothesis",
    "submit_report": "Submitting the evidence-cited report",
}


def public_tool_name(value: object) -> str:
    """Return an allowlisted tool identifier or a generic harness-tool name."""
    if not isinstance(value, str):
        return "harness_tool"
    prefix = "mcp__oncall__"
    name = value[len(prefix) :] if value.startswith(prefix) else value
    return name if name in TOOL_LABELS else "harness_tool"


def tool_result_failed(data: dict[str, Any]) -> bool:
    """Read only the result status, never its potentially sensitive content."""
    if isinstance(data.get("error"), dict):
        return True
    message = data.get("message")
    if not isinstance(message, dict):
        return False
    if message.get("isError") is True:
        return True
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(item, dict) and item.get("isError") is True for item in content)


class HarnessProgressAdapter:
    """Translate raw SDK notifications into a stable, content-free progress stream."""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self.emit = emit
        self.calls: dict[str, str] = {}

    def notification(self, method: str, payload: dict[str, Any]) -> None:
        if method != "session.event":
            return
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            data = {}

        if kind == "turn/start":
            self._emit("analysis_started")
        elif kind == "tool/call":
            call_id = data.get("callId")
            tool = public_tool_name(data.get("name"))
            if isinstance(call_id, str):
                self.calls[call_id] = tool
            self._emit("tool_started", tool=tool)
        elif kind == "tool/result":
            call_id = self._result_call_id(data)
            tool = self.calls.pop(call_id, "harness_tool") if call_id else "harness_tool"
            self._emit("tool_finished", tool=tool, failed=tool_result_failed(data))
        elif kind == "llm/retry":
            retry = data.get("retry")
            maximum = data.get("maxRetries")
            safe_retry = retry if isinstance(retry, int) else None
            safe_maximum = maximum if isinstance(maximum, int) else None
            self._emit("model_retry", retry=safe_retry, maximum=safe_maximum)
        elif kind == "turn/end":
            reason = data.get("reason")
            value = reason.get("kind") if isinstance(reason, dict) else None
            self._emit("turn_finished", reason=value if isinstance(value, str) else "unknown")

    def _emit(self, kind: str, **fields: object) -> None:
        self.emit({"protocol": PROGRESS_PROTOCOL, "kind": kind, **fields})

    @staticmethod
    def _result_call_id(data: dict[str, Any]) -> str | None:
        message = data.get("message")
        if not isinstance(message, dict):
            return None
        source = message.get("source")
        call_id = source.get("callId") if isinstance(source, dict) else None
        if isinstance(call_id, str):
            return call_id
        value = message.get("toolCallId")
        return value if isinstance(value, str) else None


def progress_message(event: dict[str, Any]) -> tuple[str, str] | None:
    """Build a trusted Rich style/message pair from a validated progress event."""
    if event.get("protocol") != PROGRESS_PROTOCOL:
        return None
    kind = event.get("kind")
    if kind == "harness_started":
        return "cyan", "DeepSeek Harness started"
    if kind == "analysis_started":
        return "cyan", "Analyzing the symptom and available evidence"
    if kind == "tool_started":
        tool = event.get("tool")
        label = (
            TOOL_LABELS.get(tool, "Using a sandbox utility")
            if isinstance(tool, str)
            else "Using a sandbox utility"
        )
        return "yellow", label
    if kind == "tool_finished":
        tool = event.get("tool")
        label = (
            TOOL_LABELS.get(tool, "Sandbox utility") if isinstance(tool, str) else "Sandbox utility"
        )
        if event.get("failed") is True:
            return "red", f"{label} failed; the agent can revise its approach"
        return "green", f"{label} completed"
    if kind == "model_retry":
        retry, maximum = event.get("retry"), event.get("maximum")
        suffix = (
            f" ({retry}/{maximum})" if isinstance(retry, int) and isinstance(maximum, int) else ""
        )
        return "yellow", f"Retrying the model request{suffix}"
    if kind == "turn_finished":
        return "cyan", f"Harness turn finished: {event.get('reason', 'unknown')}"
    if kind == "harness_failed":
        return "red", "DeepSeek Harness stopped with an error"
    return None
