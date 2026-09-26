from http import HTTPStatus

import httpx
import pytest
from fastapi import FastAPI

from oncall.http_boundary import Boundary


@pytest.fixture
def boundary():
    app = FastAPI()

    @app.post("/probe")
    async def probe(payload: dict):
        return payload

    @app.post("/admin/start")
    async def start():
        return {"started": True}

    return Boundary(app, "agent-secret", "admin-secret", maximum=32)


async def test_agent_cannot_administer_or_send_oversized_body(boundary):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boundary), base_url="http://test"
    ) as client:
        assert (await client.post("/probe", json={})).status_code == HTTPStatus.UNAUTHORIZED
        headers = {"Authorization": "Bearer agent-secret"}
        assert (
            await client.post("/admin/start", headers=headers)
        ).status_code == HTTPStatus.UNAUTHORIZED
        assert (
            await client.post("/probe", json={"x": "y"}, headers=headers)
        ).status_code == HTTPStatus.OK
        assert (
            await client.post("/probe", content="x" * 33, headers=headers)
        ).status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
