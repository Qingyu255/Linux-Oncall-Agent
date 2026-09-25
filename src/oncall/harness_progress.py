"""Safe progress protocol adapter and terminal rendering."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from oncall.progress_projection import (
    CGROUP_SCOPES,
    HYPOTHESIS_ID_PATTERN,
    HYPOTHESIS_STATUSES,
    MOUNTS,
    OUTCOMES,
    PROBE_TOOLS,
    QUALITIES,
    RELATIONSHIPS,
    REPORT_REJECTIONS,
    TOOL_LABELS,
    UNITS,
    ToolCall,
    bounded_float,
    bounded_int,
    canonical_rejection,
    facts_projection,
    public_tool_name,
    result_projection,
    safe_name,
    short_id,
    tool_parameters,
    tool_result_failed,
)
from oncall.progress_projection import (
    PROGRESS_PROTOCOL as PROGRESS_PROTOCOL,
)


class HarnessProgressAdapter:
    """Translate raw SDK notifications into the safe progress protocol."""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self.emit = emit
        self.calls: dict[str, ToolCall] = {}
        self.model_requests = 0
        self.model_retries = 0

    def notification(self, method: str, payload: dict[str, Any]) -> None:
        if method != "session.event":
            return
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        kind, data = event.get("type"), event.get("data")
        data = data if isinstance(data, dict) else {}
        if kind == "turn/start":
            self._emit("analysis_started")
        elif kind == "assistant/message":
            self.model_requests += 1
            response_fields: dict[str, object] = {"request": self.model_requests}
            usage = data.get("usage")
            if isinstance(usage, dict):
                for source, destination in (
                    ("inputTokens", "input_tokens"),
                    ("outputTokens", "output_tokens"),
                ):
                    number = bounded_int(usage.get(source), 0, 10**9)
                    if number is not None and number > 0:
                        response_fields[destination] = number
            self._emit("model_response", **response_fields)
        elif kind == "tool/call":
            tool = public_tool_name(data.get("name"))
            parameters = tool_parameters(tool, data.get("arguments"))
            if isinstance(data.get("callId"), str):
                self.calls[data["callId"]] = ToolCall(tool, parameters)
            self._emit("tool_started", tool=tool, **parameters)
        elif kind == "tool/result":
            call_id = self._result_call_id(data)
            call = (
                self.calls.pop(call_id, ToolCall("harness_tool", {}))
                if call_id
                else ToolCall("harness_tool", {})
            )
            failed = tool_result_failed(data)
            fields: dict[str, object] = {
                "tool": call.tool,
                "failed": failed,
                **call.parameters,
            }
            if failed and call.tool == "submit_report":
                fields["rejection"] = canonical_rejection(data)
            elif not failed:
                fields.update(result_projection(call.tool, data))
            self._emit("tool_finished", **fields)
        elif kind == "llm/retry":
            self.model_retries += 1
            self._emit(
                "model_retry",
                retry=bounded_int(data.get("retry"), 0, 100),
                maximum=bounded_int(data.get("maxRetries"), 0, 100),
                total_retries=self.model_retries,
            )
        elif kind == "turn/end":
            reason = data.get("reason")
            value = reason.get("kind") if isinstance(reason, dict) else None
            safe_reason = (
                value if value in {"completed", "cancelled", "interrupted", "error"} else "unknown"
            )
            self._emit(
                "turn_finished",
                reason=safe_reason,
                model_requests=self.model_requests,
                model_retries=self.model_retries,
            )

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


def format_bytes(value: object) -> str | None:
    number = bounded_int(value, 0, 2**63 - 1)
    if number is None:
        return None
    for divisor, suffix in ((1024**3, "GiB"), (1024**2, "MiB"), (1024, "KiB")):
        if number >= divisor:
            return f"{number / divisor:.1f} {suffix}"
    return f"{number} B"


def format_parameters(tool: str, event: dict[str, Any]) -> str:
    parts: list[str] = []
    duration = bounded_int(event.get("duration_seconds"), 1, 5)
    limit = bounded_int(event.get("limit"), 1, 500)
    if duration is not None:
        parts.append(f"{duration}s sample")
    if limit is not None:
        parts.append(f"limit {limit}")
    for key, allowed, label in (
        ("scope_id", CGROUP_SCOPES, "scope"),
        ("mount_id", MOUNTS, "mount"),
    ):
        if event.get(key) in allowed:
            parts.append(f"{label} {event[key]}")
    if tool == "query_service_journal" and event.get("unit") in UNITS:
        parts.append(str(event["unit"]))
        since = bounded_int(event.get("since_seconds"), 1, 900)
        if since is not None:
            parts.append(f"last {since}s")
    if tool == "read_artifact":
        artifact = short_id(event.get("artifact_id"))
        if artifact is not None:
            parts.append(f"artifact {artifact}")
    if tool == "update_hypothesis":
        identifier, status = event.get("hypothesis_id"), event.get("hypothesis_status")
        if isinstance(identifier, str) and HYPOTHESIS_ID_PATTERN.fullmatch(identifier):
            parts.append(identifier)
        if status in HYPOTHESIS_STATUSES:
            parts.append(str(status))
    if tool == "submit_report" and event.get("outcome") in OUTCOMES:
        parts.append(str(event["outcome"]))
    return f" · {' · '.join(parts)}" if parts else ""


def format_facts(value: object) -> str | None:
    facts = facts_projection(value)
    kind = facts.get("kind")
    if kind == "cpu":
        parts = []
        busy = bounded_float(facts.get("host_busy_pct"), 0, 100)
        used = bounded_float(facts.get("probe_cgroup_cpu_cores_used"), 0, 4096)
        quota = bounded_float(facts.get("probe_cgroup_quota_cores"), 0, 4096)
        throttled = bounded_int(facts.get("probe_cgroup_throttled_usec_delta"), 0, 10**15)
        if busy is not None:
            parts.append(f"host {busy:.1f}% busy")
        if used is not None:
            parts.append(
                f"probe {used:.2f}/{quota:.2f} CPU"
                if quota is not None
                else f"probe {used:.2f} CPU"
            )
        if throttled is not None:
            parts.append(f"{throttled / 1000:.0f} ms throttled")
        return " · ".join(parts) or None
    if kind == "processes":
        parts = []
        name, pid = (
            safe_name(facts.get("top_name")),
            bounded_int(facts.get("top_pid"), 1, 2**31 - 1),
        )
        cpu, rss = (
            bounded_float(facts.get("top_cpu_pct"), 0, 10**6),
            format_bytes(facts.get("top_rss_bytes")),
        )
        if name is not None:
            parts.append(f"top {name}" + (f" PID {pid}" if pid is not None else ""))
        if cpu is not None:
            parts.append(f"{cpu:.1f}% of one CPU")
        if rss is not None:
            parts.append(f"{rss} RSS")
        return " · ".join(parts) or None
    if kind == "memory":
        available, total = (
            format_bytes(facts.get("mem_available_bytes")),
            format_bytes(facts.get("mem_total_bytes")),
        )
        swap_free, swap_total = (
            format_bytes(facts.get("swap_free_bytes")),
            format_bytes(facts.get("swap_total_bytes")),
        )
        parts = []
        if available is not None and total is not None:
            parts.append(f"memory {available}/{total} available")
        if swap_free is not None and swap_total is not None:
            parts.append(f"swap {swap_free}/{swap_total} free")
        oom = bounded_int(facts.get("oom_kill"), 0, 2**63 - 1)
        if oom is not None:
            parts.append(f"oom_kill {oom}")
        return " · ".join(parts) or None
    if kind == "cgroup_memory":
        current, maximum = (
            format_bytes(facts.get("current_bytes")),
            format_bytes(facts.get("max_bytes")),
        )
        parts = [f"usage {current}" + (f"/{maximum}" if maximum else "")] if current else []
        for key, label in (("oom_delta", "OOM"), ("oom_kill_delta", "OOM kills")):
            count = bounded_int(facts.get(key), 0, 2**63 - 1)
            if count is not None:
                parts.append(f"{label} +{count}")
        return " · ".join(parts) or None
    if kind == "filesystem":
        used, available = (
            bounded_float(facts.get("used_percent"), 0, 100),
            format_bytes(facts.get("available_bytes")),
        )
        parts = [f"mount {facts['mount_id']}"] if facts.get("mount_id") in MOUNTS else []
        if used is not None:
            parts.append(f"{used:.1f}% used")
        if available is not None:
            parts.append(f"{available} available")
        return " · ".join(parts) or None
    if kind == "journal":
        entries = bounded_int(facts.get("entries"), 0, 10**9)
        captured = format_bytes(facts.get("captured_bytes"))
        parts = [f"{entries} entries"] if entries is not None else []
        if captured is not None:
            parts.append(captured)
        if facts.get("truncated") is True:
            parts.append("truncated")
        return " · ".join(parts) or None
    return None


def format_tool_result(tool: str, event: dict[str, Any], label: str) -> str:
    if tool in PROBE_TOOLS:
        metadata = []
        if event.get("quality") in QUALITIES:
            metadata.append(f"quality {event['quality']}")
        duration = bounded_float(event.get("duration_ms"), 0, 3_600_000)
        size, evidence = (
            format_bytes(event.get("artifact_bytes")),
            short_id(event.get("evidence_id")),
        )
        if duration is not None:
            metadata.append(f"{duration / 1000:.2f}s")
        if size is not None:
            metadata.append(size)
        if evidence is not None:
            metadata.append(f"evidence {evidence}")
        line = f"{label} captured" + (f" · {' · '.join(metadata)}" if metadata else "")
        facts = format_facts(event.get("facts"))
        return line + (f"\n          {facts}" if facts else "")
    if tool == "get_investigation_state":
        details = []
        probes, captured = (
            bounded_int(event.get("probe_calls"), 0, 10_000),
            format_bytes(event.get("captured_bytes")),
        )
        remaining = bounded_float(event.get("remaining_seconds"), 0, 3600)
        if probes is not None:
            details.append(f"{probes} probes")
        if captured is not None:
            details.append(f"{captured} captured")
        if remaining is not None:
            details.append(f"{remaining:.0f}s left")
        line = f"{label} completed" + (f" · {' · '.join(details)}" if details else "")
        relationship = event.get("target_relationship")
        if relationship in RELATIONSHIPS:
            relation = f"target relationship: {str(relationship).replace('_', ' ')}"
            parent, history = (
                short_id(event.get("parent_investigation_id")),
                bounded_int(event.get("historical_evidence"), 0, 10_000),
            )
            if parent is not None:
                relation += f" · parent {parent}"
            if history is not None:
                relation += f" · {history} historical evidence"
            line += f"\n          {relation}"
        return line
    if tool == "read_artifact":
        next_offset, total = (
            bounded_int(event.get("next_offset"), 0, 1024 * 1024),
            bounded_int(event.get("total_bytes"), 0, 1024 * 1024),
        )
        suffix = (
            f" · bytes {next_offset:,}/{total:,}"
            if next_offset is not None and total is not None
            else ""
        )
        return f"{label} completed{suffix}" + (" · end reached" if event.get("eof") is True else "")
    if tool == "update_hypothesis":
        identifier, status = event.get("hypothesis_id"), event.get("hypothesis_status")
        if (
            isinstance(identifier, str)
            and HYPOTHESIS_ID_PATTERN.fullmatch(identifier)
            and status in HYPOTHESIS_STATUSES
        ):
            version = bounded_int(event.get("version"), 1, 10_000)
            counts = []
            for key, name in (
                ("supporting", "supporting"),
                ("contradicting", "contradicting"),
                ("unresolved", "open questions"),
            ):
                count = bounded_int(event.get(key), 0, 20)
                if count is not None:
                    counts.append(f"{count} {name}")
            return (
                f"Hypothesis {identifier}"
                + (f" v{version}" if version else "")
                + f" → {status}"
                + (f" · {' · '.join(counts)}" if counts else "")
            )
    if tool == "submit_report" and event.get("outcome") in OUTCOMES:
        counts = []
        for key in ("claims", "alternatives", "limitations", "next_steps"):
            count = bounded_int(event.get(key), 0, 10)
            if count is not None:
                counts.append(f"{count} {key.replace('_', ' ')}")
        return f"Report accepted · {event['outcome']}" + (
            f" · {' · '.join(counts)}" if counts else ""
        )
    return f"{label} completed"


def progress_message(event: dict[str, Any]) -> tuple[str, str, str] | None:
    """Build a validated Rich style, symbol, and message from one protocol event."""
    if event.get("protocol") != PROGRESS_PROTOCOL:
        return None
    kind = event.get("kind")
    if kind == "harness_started":
        return "cyan", "●", "DeepSeek Harness started"
    if kind == "analysis_started":
        return "cyan", "●", "Analyzing the symptom and available evidence"
    if kind == "model_response":
        request = bounded_int(event.get("request"), 1, 10_000)
        if request is None:
            return None
        tokens = []
        for key, label in (("input_tokens", "input tokens"), ("output_tokens", "output tokens")):
            count = bounded_int(event.get(key), 0, 10**9)
            if count is not None:
                tokens.append(f"{count:,} {label}")
        return (
            "magenta",
            "◆",
            f"Model response {request} received" + (f" · {' · '.join(tokens)}" if tokens else ""),
        )
    if kind == "tool_started":
        tool = public_tool_name(event.get("tool"))
        return (
            "yellow",
            "→",
            TOOL_LABELS.get(tool, "Using a sandbox utility") + format_parameters(tool, event),
        )
    if kind == "tool_finished":
        tool = public_tool_name(event.get("tool"))
        label = TOOL_LABELS.get(tool, "Sandbox utility")
        if event.get("failed") is True:
            rejection = event.get("rejection")
            if tool == "submit_report" and rejection in (
                *REPORT_REJECTIONS,
                "Report rejected by policy",
            ):
                return "red", "✗", f"Report rejected: {rejection}"
            return "red", "✗", f"{label} failed; the agent can revise its approach"
        return "green", "✓", format_tool_result(tool, event, label)
    if kind == "model_retry":
        retry, maximum = (
            bounded_int(event.get("retry"), 0, 100),
            bounded_int(event.get("maximum"), 0, 100),
        )
        total = bounded_int(event.get("total_retries"), 1, 100)
        suffix = f" ({retry}/{maximum})" if retry is not None and maximum is not None else ""
        return (
            "yellow",
            "↻",
            "Retrying the model request" + suffix + (f" · total retries {total}" if total else ""),
        )
    if kind == "turn_finished":
        reason = event.get("reason")
        safe_reason = (
            reason if reason in {"completed", "cancelled", "interrupted", "error"} else "unknown"
        )
        requests, retries = (
            bounded_int(event.get("model_requests"), 0, 10_000),
            bounded_int(event.get("model_retries"), 0, 100),
        )
        counts = []
        if requests is not None:
            counts.append(f"{requests} model requests")
        if retries is not None:
            counts.append(f"{retries} retries")
        return (
            "cyan",
            "●",
            f"Harness turn finished: {safe_reason}"
            + (f" · {' · '.join(counts)}" if counts else ""),
        )
    if kind == "harness_failed":
        return "red", "✗", "DeepSeek Harness stopped with an error"
    return None
