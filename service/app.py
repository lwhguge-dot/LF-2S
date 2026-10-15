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
from service.recordings import Recordings


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
        "task_bench",
        "manual_takeover",
        "position_reset",
        "result_loss",
    ]


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed: bool


class ReplayPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recording_id: str
    index: int = 0


def create_app(root, token, port, user_data):
    journal = Journal(user_data / "logs")
    recordings = Recordings(user_data / "recordings")
    journal.recorder = recordings
    adapter = Adapter(root, journal)
    replay_frame = None
    replay_lock = asyncio.Lock()
    image_provider = SyntheticImages()
    image_lock = asyncio.Lock()
    image_frame = 0
    image_log_at = -1000
    stream_id = str(secrets.randbits(64) | (1 << 63))

    @asynccontextmanager
    async def lifespan(app):
        try:
            await adapter.start()
            yield
        finally:
            await adapter.close()
            journal.close()
            await asyncio.to_thread(recordings.close)

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
            "read_only": False,
            "hardware_control_allowed": False,
            "pid": os.getpid(),
            "frozen": bool(getattr(sys, "frozen", False)),
        }

    @app.get("/api/v1/state")
    async def state():
        return {
            **adapter.snapshot(),
            "replay": replay_frame,
            "recording": recordings.stats(),
        }

    @app.post("/api/v1/connect")
    async def connect(body: EmptyBody | None = None):
        return await adapter.connect()

    @app.post("/api/v1/disconnect")
    async def disconnect(body: EmptyBody | None = None):
        return adapter.disconnect()

    @app.post("/api/v1/profile")
    async def profile(body: EmptyBody | None = None):
        if adapter.replaying or adapter.tasks.running:
            raise ProtocolError("请先退出回放或结束任务，再查询配置")
        return await adapter.query_profile()

    @app.post("/api/v1/tasks/start")
    async def task_start(body: Confirmation):
        return adapter.tasks.start(body.confirmed)

    @app.post("/api/v1/tasks/cancel")
    async def task_cancel(body: EmptyBody | None = None):
        if adapter.replaying:
            raise ProtocolError("回放通道只读")
        return await adapter.tasks.cancel()

    @app.post("/api/v1/tasks/query")
    async def task_query(body: EmptyBody | None = None):
        if adapter.replaying:
            raise ProtocolError("回放通道只读")
        return await adapter.tasks.query()

    @app.get("/api/v1/recordings")
    async def recording_list():
        return {"recordings": await asyncio.to_thread(recordings.list)}

    @app.post("/api/v1/replay/open")
    async def replay_open(body: ReplayPosition):
        nonlocal replay_frame
        async with replay_lock:
            loaded = await asyncio.to_thread(
                recordings.frame, body.recording_id, body.index
            )
            adapter.disconnect()
            adapter.replaying = True
            replay_frame = loaded
            journal.add(
                "replay",
                "进入只读回放；已断开实时会话，旧任务不恢复",
                recording_id=body.recording_id,
            )
            return replay_frame

    @app.post("/api/v1/replay/seek")
    async def replay_seek(body: ReplayPosition):
        nonlocal replay_frame
        async with replay_lock:
            if not adapter.replaying:
                raise ProtocolError("请先打开只读回放")
            replay_frame = await asyncio.to_thread(
                recordings.frame, body.recording_id, body.index
            )
            return replay_frame

    @app.post("/api/v1/replay/close")
    async def replay_close(body: EmptyBody | None = None):
        nonlocal replay_frame
        async with replay_lock:
            replay_frame = None
            adapter.replaying = False
            journal.add("replay", "回放已退出；仍保持断开，需显式新建会话")
            return {"closed": True}

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
        nonlocal image_frame, image_log_at
        if adapter.replaying:
            raise ProtocolError("回放不读取实时合成图像")
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
            if adapter.now() - image_log_at >= 1000:
                image_log_at = adapter.now()
                journal.add(
                    "image",
                    "合成图像仅记录元数据，不保存 JPEG 字节",
                    stream_id=stream_id,
                    frame_id=image_frame,
                    hardware_freshness_qualified=False,
                )
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
