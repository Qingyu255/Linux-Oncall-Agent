"""Concise operator-facing language for safe progress protocol events."""

from __future__ import annotations

from typing import Any

from oncall.progress_projection import (
    HYPOTHESIS_ID_PATTERN,
    HYPOTHESIS_STATUSES,
    MOUNTS,
    PROGRESS_PROTOCOL,
    QUALITIES,
    REPORT_REJECTIONS,
    UNITS,
    bounded_float,
    bounded_int,
    facts_projection,
    public_tool_name,
)

START_MESSAGES = {
    "sample_cpu_pressure": "Measuring host and cgroup CPU pressure…",
    "rank_processes": "Checking which processes are using CPU…",
    "inspect_memory_pressure": "Checking host memory pressure…",
    "inspect_cgroup_memory": "Checking cgroup memory limits and OOM events…",
    "inspect_filesystem": "Checking filesystem capacity…",
    "query_service_journal": "Checking recent service logs…",
    "read_artifact": "Reviewing the relevant captured log entries…",
    "submit_report": "Preparing an evidence-backed diagnosis…",
}


def _bytes(value: object) -> str | None:
    number = bounded_int(value, 0, 2**63 - 1)
    if number is None:
        return None
    for divisor, suffix in ((1024**3, "GiB"), (1024**2, "MiB"), (1024, "KiB")):
        if number >= divisor:
            return f"{number / divisor:.1f} {suffix}"
    return f"{number} B"


def _hypothesis_name(value: object) -> str | None:
    if not isinstance(value, str) or HYPOTHESIS_ID_PATTERN.fullmatch(value) is None:
        return None
    words = value.replace("_", "-").split("-")
    return " ".join(word.upper() if word in {"cpu", "oom", "enospc"} else word for word in words)


def _start_message(tool: str, event: dict[str, Any]) -> str | None:
    if tool in {"get_investigation_state", "update_hypothesis", "harness_tool"}:
        return None
    if tool == "inspect_filesystem" and event.get("mount_id") in MOUNTS:
        return f"Checking capacity on the {event['mount_id']} mount…"
    if tool == "inspect_cgroup_memory" and event.get("scope_id") in {"self", "lab"}:
        return f"Checking memory limits and OOM events for the {event['scope_id']} cgroup…"
    if tool == "query_service_journal" and event.get("unit") in UNITS:
        service = "target service" if event["unit"] == "oncall-target.service" else "lab workload"
        return f"Checking recent logs for the {service}…"
    return START_MESSAGES.get(tool)


def _quality_prefix(event: dict[str, Any]) -> str:
    quality = event.get("quality")
    if quality not in QUALITIES or quality == "ok":
        return ""
    descriptions = {
        "partial": "Only partial evidence was available. ",
        "unsupported": "This observation is not supported on the target. ",
        "denied": "The target denied this observation. ",
        "output_limited": "The observation reached its output limit. ",
    }
    return descriptions.get(str(quality), "")


def _fact_message(value: object) -> str | None:
    facts = facts_projection(value)
    kind = facts.get("kind")
    if kind == "cpu":
        parts: list[str] = []
        busy = bounded_float(facts.get("host_busy_pct"), 0, 100)
        used = bounded_float(facts.get("probe_cgroup_cpu_cores_used"), 0, 4096)
        quota = bounded_float(facts.get("probe_cgroup_quota_cores"), 0, 4096)
        throttled = bounded_int(facts.get("probe_cgroup_throttled_usec_delta"), 0, 10**15)
        if busy is not None:
            parts.append(f"Host CPU was {busy:.1f}% busy")
        if used is not None and quota is not None:
            parts.append(f"the observed cgroup used {used:.2f} of its {quota:.2f}-CPU quota")
        elif used is not None:
            parts.append(f"the observed cgroup used {used:.2f} CPU")
        if throttled is not None:
            parts.append(f"it was throttled for {throttled / 1000:.0f} ms")
        return "; ".join(parts) + "." if parts else None
    if kind == "processes":
        name = facts.get("top_name")
        pid = bounded_int(facts.get("top_pid"), 1, 2**31 - 1)
        cpu = bounded_float(facts.get("top_cpu_pct"), 0, 10**6)
        if isinstance(name, str):
            message = f"The busiest observed process was {name}"
            if pid is not None:
                message += f" (PID {pid})"
            if cpu is not None:
                message += f" at {cpu:.1f}% of one CPU"
            return message + "."
    if kind == "memory":
        available = _bytes(facts.get("mem_available_bytes"))
        total = _bytes(facts.get("mem_total_bytes"))
        swap_free = _bytes(facts.get("swap_free_bytes"))
        swap_total = _bytes(facts.get("swap_total_bytes"))
        parts = []
        if available is not None and total is not None:
            parts.append(f"{available} of {total} memory was available")
        if swap_free is not None and swap_total is not None:
            parts.append(f"{swap_free} of {swap_total} swap was free")
        return "; ".join(parts).capitalize() + "." if parts else None
    if kind == "cgroup_memory":
        current = _bytes(facts.get("current_bytes"))
        maximum = _bytes(facts.get("max_bytes"))
        oom = bounded_int(facts.get("oom_delta"), 0, 2**63 - 1)
        kills = bounded_int(facts.get("oom_kill_delta"), 0, 2**63 - 1)
        parts = []
        if current is not None:
            usage = f"Cgroup memory usage was {current}"
            if maximum is not None:
                usage += f" of {maximum}"
            parts.append(usage)
        if oom is not None:
            parts.append(f"OOM events increased by {oom}")
        if kills is not None:
            parts.append(f"OOM kills increased by {kills}")
        return "; ".join(parts) + "." if parts else None
    if kind == "filesystem":
        mount = facts.get("mount_id")
        used = bounded_float(facts.get("used_percent"), 0, 100)
        available = _bytes(facts.get("available_bytes"))
        if mount in MOUNTS and used is not None:
            message = f"The {mount} mount is {used:.1f}% used"
            if available is not None:
                message += f" with {available} available"
            return message + "."
    if kind == "journal":
        entries = bounded_int(facts.get("entries"), 0, 10**9)
        if entries is not None:
            unit = facts.get("unit")
            service = "target-service" if unit == "oncall-target.service" else "lab-workload"
            if entries == 0:
                return f"No recent {service} log entries were found."
            return f"Found {entries} recent {service} log entries."
    return None


def operator_progress_message(event: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return one concise message without exposing unrestricted event content."""
    if event.get("protocol") != PROGRESS_PROTOCOL:
        return None
    kind = event.get("kind")
    if kind == "tool_started":
        tool = public_tool_name(event.get("tool"))
        message = _start_message(tool, event)
        return ("cyan", "→", message) if message else None
    if kind == "tool_finished":
        tool = public_tool_name(event.get("tool"))
        if event.get("failed") is True:
            if tool == "submit_report" and event.get("rejection") in (
                *REPORT_REJECTIONS,
                "Report rejected by policy",
            ):
                return "yellow", "↻", "The diagnosis needs stronger evidence; revising it…"
            message = _start_message(tool, event)
            if message:
                return (
                    "yellow",
                    "!",
                    message.replace("Checking", "Could not check").rstrip("…") + ".",
                )
            return None
        if tool in {
            "sample_cpu_pressure",
            "rank_processes",
            "inspect_memory_pressure",
            "inspect_cgroup_memory",
            "inspect_filesystem",
            "query_service_journal",
        }:
            message = _fact_message(event.get("facts"))
            if message:
                return "green", "✓", _quality_prefix(event) + message
            quality = event.get("quality")
            if quality in QUALITIES and quality != "ok":
                return "yellow", "!", _quality_prefix(event).strip()
            return None
        if tool == "update_hypothesis":
            name = _hypothesis_name(event.get("hypothesis_id"))
            status = event.get("hypothesis_status")
            if name is None or status not in HYPOTHESIS_STATUSES:
                return None
            messages = {
                "supported": f"The evidence supports the {name} hypothesis.",
                "weakened": f"The evidence weakens the {name} hypothesis.",
                "rejected": f"The evidence rules out the {name} hypothesis.",
                "open": f"The {name} hypothesis remains open.",
            }
            return "green" if status == "supported" else "cyan", "◆", messages[str(status)]
        return None
    if kind == "model_retry":
        return "yellow", "↻", "The model request was interrupted; retrying…"
    if kind == "harness_failed":
        return "red", "✗", "The investigation runtime stopped unexpectedly."
    return None
