"""Deterministic OpenAI-wire fixture, NOT an LLM or model-quality evaluation.

Uses only tool results in the request, never injection ground truth. Its role is
to exercise the real DSH loop, MCP bridge, local shell, and evidence admission.
"""

import json
import re
import uuid
from collections.abc import Iterator
from typing import Any


def fixture_message(body: dict[str, Any]) -> dict[str, Any]:
    tools = {item["function"]["name"]: item["function"] for item in body.get("tools", [])}
    results = [m for m in body["messages"] if m.get("role") == "tool"]
    step = len(results)

    def call(suffix: str, arguments: dict[str, Any]) -> dict[str, Any]:
        name = next((name for name in tools if name.endswith(suffix)), None)
        if name is None:
            raise ValueError(f"Fixture requires {suffix}; tools={list(tools)}")
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_" + uuid.uuid4().hex[:12],
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }

    def evidence_refs() -> list[str]:
        text = json.dumps(results)
        return list(dict.fromkeys(re.findall(r'evidence_id[\\"\s:]+([a-f0-9]{32})', text)))

    if step == 0:
        return call("sample_cpu_pressure", {"duration_seconds": 2})
    if step == 1:
        return call("rank_processes", {"duration_seconds": 2, "limit": 5})
    if step == 2:
        return call("inspect_memory_pressure", {})
    if step == 3:
        return call("inspect_cgroup_memory", {"scope_id": "self", "duration_seconds": 1})
    if step == 4:
        return call("inspect_filesystem", {"mount_id": "root"})
    if step == 5:
        refs = evidence_refs()
        return call(
            "update_hypothesis",
            {
                "hypothesis": {
                    "hypothesis_id": "observed_resource_pressure",
                    "claim": "One or more sampled resources may explain the reported symptom.",
                    "status": "open",
                    "supporting_evidence_ids": refs,
                    "contradicting_evidence_ids": [],
                    "unresolved_questions": [
                        "The deterministic fixture cannot interpret application causality."
                    ],
                }
            },
        )
    if step == 6:
        return call("get_investigation_state", {})
    # A local shell tool verifies useful computation without target authority.
    if step == 7:
        return call("bash", {"command": "python -c 'print(\"sandbox-analysis-ok\")'"})
    if step == 8:
        refs = evidence_refs()
        if not refs:
            raise ValueError("Fixture received no evidence IDs through MCP")
        return call(
            "submit_report",
            {
                "report": {
                    "outcome": "inconclusive",
                    "summary": (
                        "Protocol fixture completed against live Linux resource measurements."
                    ),
                    "claims": [
                        {
                            "text": (
                                "CPU, process, memory, cgroup, and filesystem observations "
                                "were collected."
                            ),
                            "evidence_ids": refs,
                        }
                    ],
                    "alternatives": [
                        "Application pathology versus expected workload is unresolved."
                    ],
                    "limitations": [
                        "Deterministic fixture, not live model reasoning.",
                        "Resource scopes must be interpreted from the cited evidence.",
                    ],
                    "next_steps": ["Use a real OpenAI model for autonomous interpretation."],
                }
            },
        )
    return {"role": "assistant", "content": "Fixture finished. Read the broker's validated report."}


def sse_chunks(body: dict[str, Any], message: dict[str, Any]) -> Iterator[str]:
    base = {
        "id": "chatcmpl-fixture",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": body["model"],
    }
    if "tool_calls" in message:
        calls = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
        delta, finish = {"role": "assistant", "tool_calls": calls}, "tool_calls"
    else:
        delta, finish = {"role": "assistant", "content": message["content"]}, "stop"
    yield (
        "data: "
        + json.dumps({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        + "\n\n"
    )
    yield (
        "data: "
        + json.dumps({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        + "\n\n"
    )
    yield "data: [DONE]\n\n"
