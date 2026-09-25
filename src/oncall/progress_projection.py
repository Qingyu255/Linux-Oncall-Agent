"""Bounded extraction of public fields from untrusted harness events."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from oncall.domain import Evidence, Hypothesis

PROGRESS_PROTOCOL = "oncall-progress-v1"
MAX_ARGUMENT_BYTES = 16 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_ASSISTANT_RESPONSE_BYTES = 8 * 1024

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
PROBE_TOOLS = {
    "sample_cpu_pressure",
    "rank_processes",
    "inspect_memory_pressure",
    "inspect_cgroup_memory",
    "inspect_filesystem",
    "query_service_journal",
}
QUALITIES = {"ok", "partial", "unsupported", "denied", "output_limited"}
HYPOTHESIS_STATUSES = {"open", "supported", "weakened", "rejected"}
RELATIONSHIPS = {"unverified", "same_boot", "rebooted"}
OUTCOMES = {"completed", "inconclusive"}
SCOPES = {"current", "historical"}
MOUNTS = {"root", "lab"}
CGROUP_SCOPES = {"self", "lab"}
UNITS = {"oncall-target.service", "oncall-lab-workload.service"}
ID_PATTERN = re.compile(r"^[a-f0-9]{8,128}$")
HYPOTHESIS_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.:+@/-]")
REPORT_REJECTIONS = (
    "Current claim cites evidence outside that scope",
    "Historical claim cites evidence outside that scope",
    "Completed findings must name cited fact fields",
    "Report cites a fact field absent from its cited evidence",
    "One claim cites evidence from multiple targets",
    "Target boot identity changed; start a fresh investigation",
    "Evidence spans multiple targets",
    "Completed report relies on limited or unavailable evidence",
    "Investigation is already terminal",
    "No investigation",
)


@dataclass(frozen=True)
class ToolCall:
    tool: str
    parameters: dict[str, object]


def public_tool_name(value: object) -> str:
    if not isinstance(value, str):
        return "harness_tool"
    prefix = "mcp__oncall__"
    name = value[len(prefix) :] if value.startswith(prefix) else value
    return name if name in TOOL_LABELS else "harness_tool"


def tool_result_failed(data: dict[str, Any]) -> bool:
    if isinstance(data.get("error"), dict):
        return True
    message = data.get("message")
    if not isinstance(message, dict):
        return False
    if message.get("isError") is True:
        return True
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(item, dict) and item.get("isError") is True for item in content
    )


def bounded_int(value: object, low: int = 0, high: int = 10**12) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def bounded_float(value: object, low: float = 0, high: float = 10**12) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and low <= number <= high else None


def short_id(value: object) -> str | None:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        return None
    return value[:8]


def safe_name(value: object, limit: int = 40) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return SAFE_NAME_PATTERN.sub("?", value[:limit]) or None


def bounded_assistant_response(value: object) -> str | None:
    """Bound terminal assistant text and remove control characters."""
    if not isinstance(value, str):
        return None
    bounded = value.encode("utf-8")[:MAX_ASSISTANT_RESPONSE_BYTES].decode("utf-8", errors="ignore")
    cleaned = "".join(
        character
        for character in bounded
        if character in {"\n", "\t"} or ord(character) >= 32 and ord(character) != 127
    ).strip()
    return cleaned or None


def assistant_response_projection(data: dict[str, Any]) -> str | None:
    """Extract only final-answer text blocks from one assistant message."""
    message = data.get("message")
    owner = message if isinstance(message, dict) else data
    content = owner.get("content")
    if not isinstance(content, list):
        return None
    text = "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    return bounded_assistant_response(text)


def json_object(value: object, byte_limit: int) -> dict[str, Any] | None:
    if not isinstance(value, str) or len(value.encode()) > byte_limit:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, UnicodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def list_count(value: object, limit: int = 10_000) -> int | None:
    return len(value) if isinstance(value, list) and len(value) <= limit else None


def hypothesis_projection(value: dict[str, Any]) -> dict[str, object]:
    try:
        hypothesis = Hypothesis.model_validate(value)
    except ValidationError:
        return {}
    return {
        "hypothesis_id": hypothesis.hypothesis_id,
        "hypothesis_status": hypothesis.status,
        "version": hypothesis.version,
        "supporting": len(hypothesis.supporting_evidence_ids),
        "contradicting": len(hypothesis.contradicting_evidence_ids),
        "unresolved": len(hypothesis.unresolved_questions),
    }


def report_projection(value: dict[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {}
    if value.get("outcome") in OUTCOMES:
        result["outcome"] = value["outcome"]
    for key in ("claims", "alternatives", "limitations", "next_steps"):
        count = list_count(value.get(key), 10)
        if count is not None:
            result[key] = count
    return result


def tool_parameters(tool: str, raw: object) -> dict[str, object]:
    args = json_object(raw, MAX_ARGUMENT_BYTES)
    if args is None or tool == "harness_tool":
        return {}
    result: dict[str, object] = {}
    duration = bounded_int(args.get("duration_seconds"), 1, 5)
    limit = bounded_int(args.get("limit"), 1, 500)
    if tool in {"sample_cpu_pressure", "rank_processes", "inspect_cgroup_memory"}:
        if duration is not None:
            result["duration_seconds"] = duration
    if tool in {"rank_processes", "query_service_journal", "read_artifact"} and limit is not None:
        result["limit"] = limit
    if tool == "inspect_cgroup_memory" and args.get("scope_id") in CGROUP_SCOPES:
        result["scope_id"] = args["scope_id"]
    if tool == "inspect_filesystem" and args.get("mount_id") in MOUNTS:
        result["mount_id"] = args["mount_id"]
    if tool == "query_service_journal" and args.get("unit") in UNITS:
        result["unit"] = args["unit"]
        since = bounded_int(args.get("since_seconds"), 1, 900)
        if since is not None:
            result["since_seconds"] = since
    if tool == "read_artifact":
        artifact = short_id(args.get("artifact_id"))
        offset = bounded_int(args.get("offset"), 0, 1024 * 1024)
        if artifact is not None:
            result["artifact_id"] = artifact
        if offset is not None:
            result["offset"] = offset
    nested = args.get("hypothesis")
    if tool == "update_hypothesis" and isinstance(nested, dict):
        result.update(hypothesis_projection(nested))
    nested = args.get("report")
    if tool == "submit_report" and isinstance(nested, dict):
        result.update(report_projection(nested))
    return result


def text_blocks(value: object) -> list[str]:
    """Extract text from current and legacy result wrappers for local parsing only."""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            result.append(item["text"])
        if isinstance(item.get("content"), list):
            result.extend(text_blocks(item["content"]))
    return result


def result_object(data: dict[str, Any]) -> dict[str, Any] | None:
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    for text in text_blocks(message.get("content")):
        parsed = json_object(text, MAX_RESULT_BYTES)
        if parsed is None:
            continue
        structured = parsed.get("structuredContent")
        return structured if isinstance(structured, dict) else parsed
    return None


def facts_projection(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    kind = value.get("kind")
    result: dict[str, object] = {}
    if kind == "cpu":
        result["kind"] = kind
        for key, high in (
            ("host_busy_pct", 100.0),
            ("host_iowait_pct", 100.0),
            ("probe_cgroup_cpu_cores_used", 4096.0),
            ("probe_cgroup_quota_cores", 4096.0),
        ):
            number = bounded_float(value.get(key), 0, high)
            if number is not None:
                result[key] = number
        number = bounded_int(value.get("probe_cgroup_throttled_usec_delta"), 0, 10**15)
        if number is not None:
            result["probe_cgroup_throttled_usec_delta"] = number
    elif kind == "processes":
        result["kind"] = kind
        processes = value.get("processes")
        top = processes[0] if isinstance(processes, (list, tuple)) and processes else None
        source = top if isinstance(top, dict) else value
        fields = (
            ("name" if top else "top_name", "top_name", safe_name),
            (
                "pid" if top else "top_pid",
                "top_pid",
                lambda item: bounded_int(item, 1, 2**31 - 1),
            ),
            (
                "cpu_pct_one_core" if top else "top_cpu_pct",
                "top_cpu_pct",
                lambda item: bounded_float(item, 0, 10**6),
            ),
            (
                "rss_bytes" if top else "top_rss_bytes",
                "top_rss_bytes",
                lambda item: bounded_int(item, 0, 2**63 - 1),
            ),
        )
        for source_name, destination, projector in fields:
            projected = projector(source.get(source_name))
            if projected is not None:
                result[destination] = projected
    elif kind == "memory":
        result["kind"] = kind
        for key in (
            "mem_total_bytes",
            "mem_available_bytes",
            "swap_total_bytes",
            "swap_free_bytes",
        ):
            number = bounded_int(value.get(key), 0, 2**63 - 1)
            if number is not None:
                result[key] = number
        vmstat = value.get("vmstat")
        number = (
            bounded_int(vmstat.get("oom_kill"), 0, 2**63 - 1) if isinstance(vmstat, dict) else None
        )
        if number is not None:
            result["oom_kill"] = number
    elif kind == "cgroup_memory":
        result["kind"] = kind
        if value.get("scope_id") in CGROUP_SCOPES:
            result["scope_id"] = value["scope_id"]
        for key in ("current_bytes", "max_bytes", "high_bytes", "oom_delta", "oom_kill_delta"):
            number = bounded_int(value.get(key), 0, 2**63 - 1)
            if number is not None:
                result[key] = number
    elif kind == "filesystem":
        result["kind"] = kind
        if value.get("mount_id") in MOUNTS:
            result["mount_id"] = value["mount_id"]
        filesystem = safe_name(value.get("filesystem_type"), 24)
        used = bounded_float(value.get("used_percent"), 0, 100)
        if filesystem is not None:
            result["filesystem_type"] = filesystem
        if used is not None:
            result["used_percent"] = used
        for key in ("available_bytes", "available_inodes"):
            number = bounded_int(value.get(key), 0, 2**63 - 1)
            if number is not None:
                result[key] = number
    elif kind == "journal":
        result["kind"] = kind
        if value.get("unit") in UNITS:
            result["unit"] = value["unit"]
        for key in ("entries", "captured_bytes"):
            number = bounded_int(value.get(key), 0, 10**9)
            if number is not None:
                result[key] = number
        if isinstance(value.get("truncated"), bool):
            result["truncated"] = value["truncated"]
    return result


def evidence_projection(value: dict[str, Any]) -> dict[str, object]:
    try:
        evidence = Evidence.model_validate(value)
    except ValidationError:
        return {}
    result: dict[str, object] = {
        "quality": evidence.status,
        "duration_ms": evidence.duration_ms,
        "artifact_bytes": evidence.artifact_bytes,
        "evidence_id": evidence.evidence_id[:8],
        "artifact_truncated": evidence.artifact_truncated,
    }
    facts = facts_projection(evidence.facts.model_dump() if evidence.facts is not None else None)
    if facts:
        result["facts"] = facts
    return result


def state_projection(value: dict[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, high in (("probe_calls", 10_000), ("captured_bytes", 10 * 1024 * 1024)):
        number = bounded_int(value.get(key), 0, high)
        if number is not None:
            result[key] = number
    remaining = bounded_float(value.get("remaining_seconds"), 0, 3600)
    if remaining is not None:
        result["remaining_seconds"] = remaining
    continuation = value.get("continuation")
    if isinstance(continuation, dict):
        if continuation.get("target_relationship") in RELATIONSHIPS:
            result["target_relationship"] = continuation["target_relationship"]
        parent = short_id(continuation.get("parent_investigation_id"))
        historical = list_count(continuation.get("historical_evidence"))
        if parent is not None:
            result["parent_investigation_id"] = parent
        if historical is not None:
            result["historical_evidence"] = historical
    return result


def artifact_projection(value: dict[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {}
    artifact = short_id(value.get("artifact_id"))
    if artifact is not None:
        result["artifact_id"] = artifact
    for key in ("offset", "next_offset", "total_bytes", "line_start", "line_end"):
        number = bounded_int(value.get(key), 0, 1024 * 1024)
        if number is not None:
            result[key] = number
    if isinstance(value.get("eof"), bool):
        result["eof"] = value["eof"]
    if value.get("evidence_scope") in SCOPES:
        result["evidence_scope"] = value["evidence_scope"]
    return result


def result_projection(tool: str, data: dict[str, Any]) -> dict[str, object]:
    value = result_object(data)
    if value is None:
        return {}
    if tool in PROBE_TOOLS:
        return evidence_projection(value)
    if tool == "get_investigation_state":
        return state_projection(value)
    if tool == "read_artifact":
        return artifact_projection(value)
    if tool == "update_hypothesis":
        return hypothesis_projection(value)
    if tool == "submit_report":
        return report_projection(value)
    return {}


def canonical_rejection(data: dict[str, Any]) -> str:
    candidates: list[str] = []
    error = data.get("error")
    if isinstance(error, dict) and isinstance(error.get("reason"), str):
        candidates.append(error["reason"])
    message = data.get("message")
    if isinstance(message, dict):
        candidates.extend(text_blocks(message.get("content")))
    for candidate in candidates:
        for reason in REPORT_REJECTIONS:
            if reason in candidate:
                return reason
    return "Report rejected by policy"
