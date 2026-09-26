"""Typed runtime configuration for process composition roots."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

ProviderMode = Literal["fixture", "openai"]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Deployment settings and enforced runtime limits with safe defaults."""

    provider_mode: ProviderMode = "fixture"
    model: str = "gpt-5.6-terra"
    openai_api_key: str = field(default="", repr=False)
    target_url: str = "http://target:8765"
    target_ca: Path | None = None
    data_dir: Path = Path("/data")
    secrets_dir: Path = Path("/run/secrets")
    target_id: str = "docker-target"
    lab_cgroup: Path = Path("/sys/fs/cgroup/oncall.slice/oncall-lab.slice")
    lab_mount: Path = Path("/var/lib/oncall-lab/data")

    model_max_tokens: int = 2048
    model_call_limit: int = 12
    investigation_max_calls: int = 20
    investigation_timeout_seconds: float = 180
    continuation_ttl_seconds: float = 24 * 60 * 60
    investigation_artifact_max_bytes: int = 10 * 1024 * 1024
    investigation_probe_timeout_seconds: float = 8
    investigation_probe_concurrency: int = 2

    provider_request_timeout_seconds: float = 60
    provider_stream_timeout_seconds: float = 90
    provider_response_max_bytes: int = 2 * 1024 * 1024
    broker_request_max_bytes: int = 1024 * 1024
    target_http_timeout_seconds: float = 8
    target_wire_max_bytes: int = 1024 * 1024
    target_response_max_bytes: int = 2 * 1024 * 1024
    target_health_max_bytes: int = 4096

    target_probe_timeout_seconds: float = 7
    target_probe_concurrency: int = 2
    target_inflight_limit: int = 64
    target_cache_limit: int = 128
    target_raw_max_bytes: int = 1024 * 1024
    target_encoded_response_max_bytes: int = 1536 * 1024
    target_request_max_bytes: int = 4096

    harness_timeout_seconds: int = 150
    harness_timeout_max_seconds: int = 180
    harness_initialize_timeout_seconds: int = 45
    harness_shutdown_timeout_seconds: int = 2

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeConfig:
        """Load deployment-specific values; security limits remain code-owned defaults."""
        values = os.environ if environ is None else environ
        defaults = cls()
        provider = values.get("ONCALL_PROVIDER", defaults.provider_mode)
        if provider not in {"fixture", "openai"}:
            raise ValueError("ONCALL_PROVIDER must be 'fixture' or 'openai'")
        model = values.get("ONCALL_MODEL", defaults.model).strip()
        if not model:
            raise ValueError("ONCALL_MODEL must not be empty")
        target_url = values.get("ONCALL_TARGET_URL", defaults.target_url).strip()
        if not target_url:
            raise ValueError("ONCALL_TARGET_URL must not be empty")
        ca_value = values.get("ONCALL_TARGET_CA", "").strip()
        return cls(
            provider_mode=cast(ProviderMode, provider),
            model=model,
            openai_api_key=values.get("OPENAI_API_KEY", ""),
            target_url=target_url,
            target_ca=Path(ca_value) if ca_value else None,
            data_dir=Path(values.get("ONCALL_DATA", str(defaults.data_dir))),
            secrets_dir=Path(values.get("ONCALL_SECRETS_DIR", str(defaults.secrets_dir))),
            target_id=values.get("TARGET_ID", defaults.target_id),
            lab_cgroup=Path(values.get("ONCALL_LAB_CGROUP", str(defaults.lab_cgroup))),
            lab_mount=Path(values.get("ONCALL_LAB_MOUNT", str(defaults.lab_mount))),
        )


DEFAULT_RUNTIME_CONFIG = RuntimeConfig()
