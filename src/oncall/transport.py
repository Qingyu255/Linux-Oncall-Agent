"""Fixed target transport; callers cannot supply destination URLs."""

import uuid
from pathlib import Path
from typing import Any

import httpx

from oncall.domain import Observation, ProbeRequest


class HttpTargetClient:
    def __init__(self, url: str, token: str, ca_file: Path | None = None) -> None:
        self.client = httpx.AsyncClient(
            base_url=url,
            timeout=8,
            trust_env=False,
            verify=str(ca_file) if ca_file else True,
            headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
        )

    async def collect(self, request: ProbeRequest) -> Observation:
        headers = {"Idempotency-Key": uuid.uuid4().hex}
        async with self.client.stream(
            "POST", "/v1/probe", json=request.model_dump(), headers=headers
        ) as response:
            response.raise_for_status()
            wire_length = response.headers.get("content-length")
            if wire_length and int(wire_length) > 1024 * 1024:
                raise ValueError("Compressed target response exceeds wire bound")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("Target response exceeds bound")
        return Observation.model_validate_json(data)

    async def health(self) -> dict[str, Any]:
        response = await self.client.get("/health")
        response.raise_for_status()
        if len(response.content) > 4096:
            raise ValueError("Target health response exceeds bound")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Target health response is not an object")
        return payload

    async def close(self) -> None:
        await self.client.aclose()
