from oncall.broker import provider_payload


def test_terra_chat_completions_adapts_harness_tool_parameters():
    body = {
        "model": "gpt-5.6-terra",
        "messages": [{"role": "user", "content": "inspect"}],
        "tools": [{"type": "function"}],
        "temperature": 0,
        "stream": True,
        "untrusted_extra": "discard",
    }

    payload = provider_payload(body, "gpt-5.6-terra")

    assert "temperature" not in payload
    assert "untrusted_extra" not in payload
    assert payload["reasoning_effort"] == "none"
    assert payload["max_completion_tokens"] == 2048


def test_non_terra_model_retains_allowlisted_temperature():
    payload = provider_payload(
        {"model": "gpt-4.1-mini", "messages": [], "temperature": 0.7},
        "gpt-4.1-mini",
    )

    assert payload["temperature"] == 0.7
    assert "reasoning_effort" not in payload
