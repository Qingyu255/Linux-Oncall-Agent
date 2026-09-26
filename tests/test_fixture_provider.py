from oncall.fixture_provider import fixture_message


def request_body(*, tool_results=()):
    return {
        "model": "fixture",
        "tools": [
            {"type": "function", "function": {"name": "skill"}},
            {"type": "function", "function": {"name": "sample_cpu_pressure"}},
        ],
        "messages": [{"role": "tool", "content": value} for value in tool_results],
    }


def test_fixture_loads_a_real_diagnostic_skill_before_probing():
    message = fixture_message(request_body())
    call = message["tool_calls"][0]["function"]

    assert call["name"] == "skill"
    assert call["arguments"] == '{"name": "linux-cpu-diagnosis"}'


def test_fixture_starts_probe_sequence_after_skill_load():
    message = fixture_message(request_body(tool_results=("loaded",)))
    call = message["tool_calls"][0]["function"]

    assert call["name"] == "sample_cpu_pressure"
    assert call["arguments"] == '{"duration_seconds": 2}'
