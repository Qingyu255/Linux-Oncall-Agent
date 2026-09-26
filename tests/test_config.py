import pytest

from oncall.config import RuntimeConfig


def test_runtime_config_has_consistent_safe_defaults():
    config = RuntimeConfig.from_env({})

    assert config.provider_mode == "fixture"
    assert config.model == "gpt-5.6-terra"
    assert config.target_url == "http://target:8765"
    assert config.model_max_tokens == 2048
    assert config.target_probe_concurrency == 2


def test_runtime_config_reads_only_deployment_values_from_environment():
    config = RuntimeConfig.from_env(
        {
            "ONCALL_PROVIDER": "openai",
            "ONCALL_MODEL": "model-from-env",
            "OPENAI_API_KEY": "api-key-value",
            "ONCALL_TARGET_URL": "https://target.example",
            "ONCALL_TARGET_CA": "/tmp/target-ca.pem",
            "ONCALL_DATA": "/tmp/evidence",
            "ONCALL_SECRETS_DIR": "/tmp/secrets",
            "TARGET_ID": "i-test",
            "ONCALL_LAB_CGROUP": "/test/cgroup",
            "ONCALL_LAB_MOUNT": "/test/mount",
        }
    )

    assert config.provider_mode == "openai"
    assert config.model == "model-from-env"
    assert config.openai_api_key == "api-key-value"
    assert "api-key-value" not in repr(config)
    assert str(config.target_ca) == "/tmp/target-ca.pem"
    assert str(config.data_dir) == "/tmp/evidence"
    assert config.target_id == "i-test"
    assert config.investigation_max_calls == 20


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"ONCALL_PROVIDER": "other"}, "ONCALL_PROVIDER"),
        ({"ONCALL_MODEL": "  "}, "ONCALL_MODEL"),
        ({"ONCALL_TARGET_URL": ""}, "ONCALL_TARGET_URL"),
    ],
)
def test_runtime_config_rejects_invalid_environment(environment, message):
    with pytest.raises(ValueError, match=message):
        RuntimeConfig.from_env(environment)
