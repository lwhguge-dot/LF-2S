import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from lf2s_protocol.wire import ProtocolError
from pydantic import BaseModel, ConfigDict, Field

from service.adapter import Adapter
from service.controller import controller_snapshot
from service.esp32 import Esp32Bench, target
from service.fc_usb import FcUsbBench
from service.images import SyntheticImages
from service.journal import Journal
from service.recordings import Recordings
from service.text_tasks import TextTasks
from service.voice import MAX_AUDIO_BYTES, Voice


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


class BoardTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: str = "192.168.4.1"
    port: int = 80


class BoardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    size: Literal["qvga", "vga", "svga"]
    colour: Literal["driver", "office"]


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=512)


def create_app(root, token, port, user_data):
    journal = Journal(user_data / "logs")
    recordings = Recordings(user_data / "recordings")
    journal.recorder = recordings
    adapter = Adapter(root, journal)
    bench = Esp32Bench(journal)
    usb_fc = FcUsbBench(journal, user_data / "logs")
    text_tasks = TextTasks(journal)
    voice = Voice(journal)
    mode_lock = asyncio.Lock()
    replay_frame = None
    replay_lock = asyncio.Lock()
    image_provider = SyntheticImages()
    image_lock = asyncio.Lock()
    image_frame = 0
    image_log_at = -1000
    stream_id = str(secrets.randbits(64) | (1 << 63))

    def simulation_only():
        if bench.active or usb_fc.active:
            raise ProtocolError(
                "实物台架占用中；请先断开相机和飞控 USB，再使用模拟功能"
            )

    async def poll_board():
        while True:
            await asyncio.sleep(1)
            try:
                await bench.refresh()
            except ProtocolError:
                pass  # The provider bounds transient recovery and pins the paired stream.

    @asynccontextmanager
    async def lifespan(app):
        board_poller = None
        try:
            await adapter.start()
            board_poller = asyncio.create_task(poll_board())
            yield
        finally:
            try:
                await voice.close()
                await text_tasks.close()
                await usb_fc.disconnect()
            finally:
                bench.disconnect()
                if board_poller:
                    board_poller.cancel()
                    await asyncio.gather(board_poller, return_exceptions=True)
                await adapter.close()
                journal.close()
                await asyncio.to_thread(recordings.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.adapter = adapter
    app.state.bench = bench
    app.state.usb_fc = usb_fc
    app.state.text_tasks = text_tasks
    app.state.voice = voice

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
                limit = (
                    MAX_AUDIO_BYTES
                    if request.url.path == "/api/v1/voice/transcribe"
                    else 4096
                )
                if int(request.headers.get("content-length", "0")) > limit:
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
            "simulation": not (bench.active or usb_fc.active),
            "read_only": bench.active or usb_fc.active,
            "hardware_control_allowed": False,
            "pid": os.getpid(),
            "frozen": bool(getattr(sys, "frozen", False)),
        }

    @app.get("/api/v1/state")
    async def state():
        snapshot = {
            **adapter.snapshot(),
            "replay": replay_frame,
            "recording": recordings.stats(),
            "mode": "replay" if adapter.replaying else "simulation",
            "bench": bench.snapshot(),
            "usb_fc": usb_fc.snapshot(),
            "text_tasks": text_tasks.snapshot(),
            "voice": voice.snapshot(),
        }
        if bench.active or usb_fc.active:
            snapshot.update(
                simulation=False,
                read_only=True,
                task_fixture=False,
                mode="hardware_bench" if usb_fc.active else "esp32_bench",
            )
            snapshot["link"] = {
                "phase": "disconnected",
                "reason": "未建立真实飞控 MAVLink 会话；USB 厂商数据独立显示",
                "state_age_ms": None,
                "transport": "MAVLink 未接入",
                "ports": [],
            }
            snapshot["sensors"] = {
                name: {
                    **sensor,
                    "valid": False,
                    "value": None,
                    "age_ms": None,
                    "reason": "缺少已核实坐标与采样有效性的 MAVLink 测量",
                    "source": "unavailable",
                }
                for name, sensor in snapshot["sensors"].items()
            }
            snapshot["qualification"].update(
                configuration="unknown",
                configuration_reason="未读取或签认生产配置",
                control_reason="独立硬件台架仅供显示与诊断；飞行动作关闭",
                manual_latched=None,
                lease_id=None,
                task_id=None,
            )
            snapshot["identity"].update(
                boot_id=None,
                session_id=None,
                client_nonce=None,
                observed_dialect_hash=None,
                sample_id=None,
            )
            snapshot["controller"] = controller_snapshot(reason="未建立真实飞控 MAVLink 会话")
            snapshot["profile"] = None
            snapshot["operations"] = []
            snapshot["task"] = {
                "running": False,
                "unknown_request_id": None,
                "current": None,
                "target_height_mm": 500,
            }
            snapshot["counters"].update(sent=0, received=0, rejected=0)
        return snapshot

    @app.post("/api/v1/voice/transcribe")
    async def voice_transcribe(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "audio/wav":
            return JSONResponse({"error": "请提交 PCM WAV 音频"}, status_code=415)
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for block in request.stream():
                    if len(raw) + len(block) > MAX_AUDIO_BYTES:
                        return JSONResponse(
                            {"error": "音频超过大小上限"}, status_code=413
                        )
                    raw.extend(block)
        except TimeoutError:
            return JSONResponse({"error": "音频提交超时"}, status_code=408)
        async with mode_lock:
            if adapter.replaying:
                raise ProtocolError("回放期间不识别实时语音")
            return voice.start(bytes(raw))

    @app.post("/api/v1/voice/clear")
    async def voice_clear(body: EmptyBody | None = None):
        async with mode_lock:
            return await voice.clear()

    @app.post("/api/v1/text-tasks/parse")
    async def text_parse(body: TextInput):
        async with mode_lock:
            if adapter.replaying:
                raise ProtocolError("回放期间不解析实时文本")
            return text_tasks.start(body.text)

    @app.post("/api/v1/text-tasks/clear")
    async def text_clear(body: EmptyBody | None = None):
        async with mode_lock:
            return await text_tasks.clear()

    @app.post("/api/v1/fc-usb/connect")
    async def fc_usb_connect(body: EmptyBody | None = None):
        async with mode_lock:
            if adapter.replaying:
                raise ProtocolError("请先退出只读回放")
            adapter.disconnect()
            return await usb_fc.connect()

    @app.post("/api/v1/fc-usb/disconnect")
    async def fc_usb_disconnect(body: EmptyBody | None = None):
        async with mode_lock:
            return await usb_fc.disconnect()

    @app.post("/api/v1/bench/connect")
    async def bench_connect(body: BoardTarget):
        target(body.host, body.port)
        async with mode_lock:
            if adapter.replaying:
                raise ProtocolError("请先退出只读回放")
            if bench.active or bench.lock.locked():
                raise ProtocolError("请先断开 ESP32 实物台架")
            adapter.disconnect()
            return await bench.connect(body.host, body.port)

    @app.post("/api/v1/bench/disconnect")
    async def bench_disconnect(body: EmptyBody | None = None):
        async with mode_lock:
            return bench.disconnect()

    @app.get("/api/v1/bench/preview")
    async def bench_preview():
        if adapter.replaying:
            raise ProtocolError("回放不读取实物图像")
        jpeg, headers = await bench.preview()
        return Response(jpeg, media_type="image/jpeg", headers=headers)

    @app.post("/api/v1/bench/focus")
    async def bench_focus(body: EmptyBody | None = None):
        if adapter.replaying:
            raise ProtocolError("回放禁止实物对焦")
        return await bench.focus()

    @app.post("/api/v1/bench/settings")
    async def bench_settings(body: BoardSettings):
        if adapter.replaying:
            raise ProtocolError("回放禁止调整实物预览")
        return await bench.settings(body.size, body.colour)

    @app.post("/api/v1/connect")
    async def connect(body: EmptyBody | None = None):
        async with mode_lock:
            simulation_only()
            return await adapter.connect()

    @app.post("/api/v1/disconnect")
    async def disconnect(body: EmptyBody | None = None):
        async with mode_lock:
            simulation_only()
            return adapter.disconnect()

    @app.post("/api/v1/profile")
    async def profile(body: EmptyBody | None = None):
        simulation_only()
        if adapter.replaying or adapter.tasks.running:
            raise ProtocolError("请先退出回放或结束任务，再查询配置")
        return await adapter.query_profile()

    @app.post("/api/v1/tasks/start")
    async def task_start(body: Confirmation):
        simulation_only()
        return adapter.tasks.start(body.confirmed)

    @app.post("/api/v1/tasks/cancel")
    async def task_cancel(body: EmptyBody | None = None):
        simulation_only()
        if adapter.replaying:
            raise ProtocolError("回放通道只读")
        return await adapter.tasks.cancel()

    @app.post("/api/v1/tasks/query")
    async def task_query(body: EmptyBody | None = None):
        simulation_only()
        if adapter.replaying:
            raise ProtocolError("回放通道只读")
        return await adapter.tasks.query()

    @app.get("/api/v1/recordings")
    async def recording_list():
        return {"recordings": await asyncio.to_thread(recordings.list)}

    @app.post("/api/v1/replay/open")
    async def replay_open(body: ReplayPosition):
        nonlocal replay_frame
        async with mode_lock, replay_lock:
            loaded = await asyncio.to_thread(
                recordings.frame, body.recording_id, body.index
            )
            bench.disconnect()
            await usb_fc.disconnect()
            await text_tasks.clear("进入回放，文本建议已清空")
            await voice.clear()
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
        simulation_only()
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
        simulation_only()
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
