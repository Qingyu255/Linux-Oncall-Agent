from oncall.operator_view import operator_progress_message


def event(kind: str, **values: object) -> dict[str, object]:
    return {"protocol": "oncall-progress-v1", "kind": kind, **values}


def test_lifecycle_and_model_events_are_suppressed():
    assert operator_progress_message(event("harness_started")) is None
    assert operator_progress_message(event("analysis_started")) is None
    assert operator_progress_message(event("model_response", request=3)) is None
    assert operator_progress_message(event("turn_finished")) is None
    assert (
        operator_progress_message(
            event("tool_started", tool="skill", skill_name="linux-cpu-diagnosis")
        )
        is None
    )


def test_filesystem_result_is_natural_and_omits_transport_metadata():
    progress = event(
        "tool_finished",
        tool="inspect_filesystem",
        quality="ok",
        duration_ms=3,
        artifact_bytes=208,
        evidence_id="2e9eeec0",
        facts={
            "kind": "filesystem",
            "mount_id": "lab",
            "used_percent": 99.6,
            "available_bytes": 4 * 1024 * 1024,
        },
    )

    assert operator_progress_message(progress) == (
        "green",
        "✓",
        "The lab mount is 99.6% used with 4.0 MiB available.",
    )
    rendered = str(operator_progress_message(progress))
    assert "208" not in rendered
    assert "2e9eeec0" not in rendered
    assert "3 ms" not in rendered


def test_empty_journal_has_operator_meaning_instead_of_zero_bytes():
    progress = event(
        "tool_finished",
        tool="query_service_journal",
        quality="ok",
        artifact_bytes=0,
        facts={
            "kind": "journal",
            "unit": "oncall-target.service",
            "entries": 0,
            "returned_bytes": 0,
        },
    )

    assert operator_progress_message(progress) == (
        "green",
        "✓",
        "No recent target-service log entries were found.",
    )
    assert "0 B" not in str(operator_progress_message(progress))


def test_hypothesis_is_explained_without_claim_or_raw_reason():
    assert operator_progress_message(
        event(
            "tool_finished",
            tool="update_hypothesis",
            hypothesis_id="capacity-enospc",
            hypothesis_status="supported",
            claim="untrusted arbitrary content",
        )
    ) == ("green", "◆", "The evidence supports the capacity ENOSPC hypothesis.")


def test_unknown_content_does_not_cross_the_terminal_boundary():
    progress = event(
        "tool_finished",
        tool="unknown_tool",
        message="[bold]arbitrary model text[/]",
        facts={"kind": "unknown", "secret": "do not print"},
    )

    assert operator_progress_message(progress) is None
