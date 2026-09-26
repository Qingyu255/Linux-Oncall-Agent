"""Target process: no harness, shell endpoint, or model credentials."""

import asyncio
import hashlib
import re
from collections import OrderedDict
from http import HTTPStatus

from fastapi import FastAPI, Header, HTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from oncall.config import RuntimeConfig
from oncall.domain import Observation, ProbeRequest
from oncall.http_boundary import Boundary, secret_file
from oncall.probes import default_registry


def create_app(config: RuntimeConfig | None = None) -> Boundary:
    settings = config or RuntimeConfig.from_env()
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # Raw artifacts remain bounded, and JSON is compressed only on the trusted
    # broker-to-target link. HTTPX enforces a separate decoded-size limit.
    api.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    api.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["target", "localhost", "127.0.0.1", "host.docker.internal"],
    )
    registry = default_registry(
        settings.target_id,
        lab_cgroup=settings.lab_cgroup,
        lab_mount=settings.lab_mount,
    )
    slots = asyncio.Semaphore(settings.target_probe_concurrency)
    cache_lock = asyncio.Lock()
    cache: OrderedDict[str, tuple[str, Observation]] = OrderedDict()
    inflight: dict[str, tuple[str, asyncio.Task[Observation]]] = {}

    async def execute(request: ProbeRequest, request_id: str) -> Observation:
        try:
            async with asyncio.timeout(settings.target_probe_timeout_seconds):
                async with slots:
                    observation = await registry.collect(request)
            if len(observation.raw.encode()) > settings.target_raw_max_bytes:
                raise HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "output_limited")
            if (
                len(observation.model_dump_json().encode())
                > settings.target_encoded_response_max_bytes
            ):
                raise HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "response_limited")
            fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
            async with cache_lock:
                cache[request_id] = (fingerprint, observation)
                while len(cache) > settings.target_cache_limit:
                    cache.popitem(last=False)
            return observation
        except TimeoutError as error:
            raise HTTPException(HTTPStatus.GATEWAY_TIMEOUT, "probe_timeout") from error
        except (OSError, ValueError) as error:
            raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, type(error).__name__) from error
        finally:
            async with cache_lock:
                current = inflight.get(request_id)
                if current is not None and current[1] is asyncio.current_task():
                    inflight.pop(request_id, None)

    @api.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "protocol": 3,
            "target_id": settings.target_id,
            "capabilities": registry.capabilities,
        }

    @api.post("/v1/probe")
    async def collect(
        request: ProbeRequest,
        request_id: str = Header(alias="Idempotency-Key", min_length=32, max_length=64),
    ) -> Observation:
        if not re.fullmatch(r"[a-f0-9]{32,64}", request_id):
            raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_request_id")
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        async with cache_lock:
            previous = cache.get(request_id)
            if previous:
                if previous[0] != fingerprint:
                    raise HTTPException(HTTPStatus.CONFLICT, "request_id_reused")
                cache.move_to_end(request_id)
                return previous[1]
            pending = inflight.get(request_id)
            if pending:
                if pending[0] != fingerprint:
                    raise HTTPException(HTTPStatus.CONFLICT, "request_id_reused")
                task = pending[1]
            else:
                if len(inflight) >= settings.target_inflight_limit:
                    raise HTTPException(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        "too_many_inflight_requests",
                    )
                task = asyncio.create_task(execute(request, request_id))
                inflight[request_id] = (fingerprint, task)
        # A disconnected client must not cancel the bounded target-side operation.
        return await asyncio.shield(task)

    return Boundary(
        api,
        secret_file("target_token", settings.secrets_dir),
        maximum=settings.target_request_max_bytes,
    )
