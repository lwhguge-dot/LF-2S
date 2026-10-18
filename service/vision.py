"""Explicit, single-frame Qwen observations for the read-only workbench."""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

from lf2s_protocol.wire import ProtocolError

from service.demo_vision import Frame, ObservationError, Pipeline, Policy
from service.demo_vision.model import VisionModel


class VisionObservations:
    def __init__(self, journal, *, env=None, model=None, clock=None):
        settings = os.environ if env is None else env
        self.journal = journal
        self.policy = Policy.from_dict(
            json.loads(Path(__file__).with_name("vision-policy.json").read_text("utf-8"))
        )
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self.session_id = str(secrets.randbits(64) | (1 << 63))
        self.clock_id = "station:" + self.session_id
        self.pipeline = Pipeline(self.policy)
        self.model = model
        self.configuration_error = None
        self.configured = model is not None
        if model is None:
            base = settings.get("LF2S_MODEL_BASE_URL", "")
            key = settings.get("LF2S_MODEL_API_KEY", "")
            if settings.get("LF2S_MODEL_NAME", "qwen38") != "qwen38":
                self.configuration_error = "视觉观察仅允许配置的 qwen38 模型。"
            elif not base or not key:
                self.configuration_error = "请配置本机模型地址和密钥后再分析画面。"
            else:
                try:
                    self.model = VisionModel(base, key, self.policy)
                    self.configured = True
                except ObservationError:
                    self.configuration_error = "模型服务地址或密钥配置无效。"
        self.current = None
        self.runner = None
        self.closed = False

    @property
    def running(self):
        return self.runner is not None and not self.runner.done()

    def snapshot(self):
        return {
            "configured": self.configured,
            "configuration_error": self.configuration_error,
            "model": "qwen38" if self.configured else None,
            "thinking_enabled": False,
            "running": self.running,
            "timeout_ms": self.policy.request_timeout_ms,
            "current": self.current,
            "hardware_control_allowed": False,
            "crossing_allowed": False,
            "physical_accuracy_verified": False,
        }

    def start(
        self,
        image,
        *,
        source,
        frame_id,
        stream_id,
        epoch,
        received_ms,
        capture_us=None,
    ):
        if self.closed:
            raise ProtocolError("画面观察服务已关闭")
        if not self.configured:
            raise ProtocolError(self.configuration_error or "视觉模型未配置")
        if self.running:
            raise ProtocolError("当前单帧分析尚未结束")
        if source not in ("synthetic", "esp32_preview"):
            raise ProtocolError("只允许分析当前工作台图像来源")
        if type(epoch) is not int or not 0 <= epoch <= 0xFFFFFFFF:
            raise ProtocolError("图像连接代数无效")
        if source == "esp32_preview" and epoch < 1:
            raise ProtocolError("实物相机连接代数无效")
        if source == "synthetic" and (epoch != 0 or capture_us is not None):
            raise ProtocolError("本机模拟帧关联信息无效")
        try:
            frame = Frame(
                image=image,
                frame_id=frame_id,
                stream_id=stream_id,
                received_ms=received_ms,
                clock_id=self.clock_id,
                source=source,
                session_id=self.session_id,
                reset_seq=epoch if source == "esp32_preview" else None,
                source_capture_us=capture_us,
                source_clock_id=(f"esp32:{epoch}" if capture_us is not None else None),
                frame_id_source="source_reported",
            )
        except ObservationError as error:
            raise ProtocolError("画面元数据无效：" + str(error)) from None
        request_id = secrets.token_hex(12)
        self.current = {
            "request_id": request_id,
            "phase": "pending",
            "frame": frame.provenance(),
            "observation": self.pipeline.empty("pending", frame),
            "error": None,
        }
        self.journal.add(
            "vision",
            "用户显式请求单帧模型观察",
            request_id=request_id,
            source=source,
            frame_id=frame_id,
            stream_id=stream_id,
            image_sha256=frame.provenance()["image_sha256"],
            model="qwen38",
            thinking_enabled=False,
        )
        self.runner = asyncio.create_task(self._run(request_id, frame))
        return self.snapshot()

    async def _run(self, request_id, frame):
        task = asyncio.current_task()
        try:
            observation = await self.pipeline.analyze(frame, self.model, self.clock)
            if self.current and self.current["request_id"] == request_id:
                self._finish(request_id, observation)
        except asyncio.CancelledError:
            raise
        except ObservationError as error:
            self.pipeline.invalidate(str(error), frame)
            if self.current and self.current["request_id"] == request_id:
                self._finish(request_id, self.pipeline.current)
        except Exception:
            self.pipeline.invalidate("provider_failed", frame)
            if self.current and self.current["request_id"] == request_id:
                self._finish(request_id, self.pipeline.current)
        finally:
            if self.runner is task:
                self.runner = None

    def _finish(self, request_id, observation):
        self.current = {
            **self.current,
            "phase": "ready" if observation["phase"] == "ready" else "error",
            "observation": observation,
            "error": observation.get("reason")
            if observation["phase"] != "ready"
            else None,
        }
        self.journal.add(
            "vision",
            "单帧观察完成" if observation["phase"] == "ready" else "单帧观察不可用",
            request_id=request_id,
            phase=self.current["phase"],
            reason=self.current["error"],
            model_elapsed_ms=observation.get("model_elapsed_ms"),
        )

    async def cancel(self, request_id):
        if self.current is not None and request_id != self.current["request_id"]:
            raise ProtocolError("单帧观察请求编号已变化")
        if self.runner and not self.runner.done():
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        self.runner = None
        frame = None
        if self.current:
            frame_source = self.current.get("observation", {}).get("frame")
            if frame_source:
                self.pipeline.invalidate("cancelled")
            frame = self.current.get("frame")
        else:
            self.pipeline.invalidate("cancelled")
        self.current = None
        self.journal.add(
            "vision", "单帧观察已取消或清除", request_id=request_id, frame=frame
        )
        return self.snapshot()

    async def invalidate(self, reason):
        if self.runner and not self.runner.done():
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        self.runner = None
        self.pipeline.invalidate(reason)
        self.current = None

    async def close(self):
        self.closed = True
        await self.invalidate("closed")
        if self.model is not None and hasattr(self.model, "close"):
            await self.model.close()
