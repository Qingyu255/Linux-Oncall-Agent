"""Small ASGI authentication/body-size boundary shared by lab services."""

import secrets
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oncall.config import DEFAULT_RUNTIME_CONFIG


def secret_file(name: str, directory: Path = DEFAULT_RUNTIME_CONFIG.secrets_dir) -> str:
    """Read one mounted secret from the configured secrets directory."""
    value = (directory / name).read_text().strip()
    if len(value) < 24:
        raise RuntimeError(f"Missing or short {name} secret")
    return value


class Boundary:
    def __init__(
        self,
        app: ASGIApp,
        token: str,
        admin_token: str | None = None,
        maximum: int = 1024 * 1024,
    ) -> None:
        self.app, self.token, self.admin_token, self.maximum = app, token, admin_token, maximum

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path != "/health":
            token = self.admin_token if path.startswith("/admin/") else self.token
            provided = dict(scope["headers"]).get(b"authorization", b"").decode()
            if token is None or not secrets.compare_digest(provided, f"Bearer {token}"):
                return await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > self.maximum:
                return await JSONResponse({"error": "body_limit"}, 413)(scope, receive, send)
            if not event.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
