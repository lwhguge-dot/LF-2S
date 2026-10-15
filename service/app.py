import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from lf2s_protocol.wire import ProtocolError
from pydantic import BaseModel, ConfigDict

from service.adapter import Adapter
from service.images import SyntheticImages
from service.journal import Journal


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal[
        "healthy",
        "unknown",
        "sensor_freeze",
        "link_pause",
        "bad_fingerprint",
        "wrong_source",
        "reboot",
    ]


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def create_app(root, token, port, user_data):
    journal = Journal(user_data / "logs")
    adapter = Adapter(root, journal)
    image_provider = SyntheticImages()
    image_lock = asyncio.Lock()
    image_frame = 0
    stream_id = str(secrets.randbits(64) | (1 << 63))

    @asynccontextmanager
    async def lifespan(app):
        try:
            await adapter.start()
            yield
        finally:
            await adapter.close()
            journal.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.adapter = adapter

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if (
            request.headers.get("host") != f"127.0.0.1:{port}"
            or request.headers.get("origin") is not None
            or request.client is None
            or request.client.host != "127.0.0.1"
        ):
            return JSONResponse({"error": "仅允许本机桌面服务调用"}, status_code=403)
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, f"Bearer {token}"):
            return JSONResponse({"error": "本地会话凭据无效"}, status_code=401)
        if request.method == "POST":
            try:
                if int(request.headers.get("content-length", "0")) > 4096:
                    return JSONResponse({"error": "请求过大"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "请求长度无效"}, status_code=400)
            if request.headers.get("transfer-encoding"):
                return JSONResponse({"error": "不支持流式请求"}, status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ProtocolError)
    async def protocol_error(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/api/v1/health")
    async def health():
        import os
        import sys

        return {
            "api_version": "1",
            "ready": True,
            "simulation": True,
            "read_only": True,
            "pid": os.getpid(),
            "frozen": bool(getattr(sys, "frozen", False)),
        }

    @app.get("/api/v1/state")
    async def state():
        return adapter.snapshot()

    @app.post("/api/v1/connect")
    async def connect(body: EmptyBody | None = None):
        return await adapter.connect()

    @app.post("/api/v1/disconnect")
    async def disconnect(body: EmptyBody | None = None):
        return adapter.disconnect()

    @app.post("/api/v1/profile")
    async def profile(body: EmptyBody | None = None):
        return await adapter.query_profile()

    @app.post("/api/v1/simulation/scenario")
    async def scenario(body: Scenario):
        return adapter.set_scenario(body.name)

    @app.get("/api/v1/events")
    async def events(after: int = 0):
        return {
            "events": [e for e in journal.history if e["id"] > after],
            "cursor": journal.sequence,
            "dropped": journal.dropped,
            "oldest_available": journal.history[0]["id"] if journal.history else None,
        }

    @app.get("/api/v1/image")
    async def image():
        nonlocal image_frame
        if adapter.snapshot()["link"]["phase"] != "online":
            raise ProtocolError("链路未在线，合成图像已停止")
        if image_lock.locked():
            return JSONResponse({"error": "已有图像请求"}, status_code=429)
        async with image_lock:
            if image_frame >= 0xFFFFFFFF:
                raise ProtocolError("合成帧计数已耗尽，请重启服务")
            image_frame += 1
            # Pillow and JPEG compression run outside the UDP/event loop.
            jpeg = await asyncio.to_thread(image_provider.render, image_frame)
            return Response(
                jpeg,
                media_type="image/jpeg",
                headers={
                    "X-LF2S-Synthetic": "1",
                    "X-LF2S-Frame-Id": str(image_frame),
                    "X-LF2S-Stream-Id": stream_id,
                    "X-LF2S-Hardware-Qualified": "0",
                    "X-LF2S-Width": "960",
                    "X-LF2S-Height": "540",
                },
            )

    # Deliberately no /action or generic message forwarding endpoint.
    return app
