"""Bounded Mandarin transcription client. It never parses or sends a task."""

import asyncio
import copy
import hashlib
import io
import ipaddress
import math
import os
import re
import secrets
import time
import wave
from urllib.parse import urlsplit

import httpx
from lf2s_protocol.wire import ProtocolError

from service.text_contract import strict_json

MODEL_ID = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
MAX_AUDIO_BYTES = 500000


def validate_wav(raw):
    if not isinstance(raw, bytes) or not 44 <= len(raw) <= MAX_AUDIO_BYTES:
        raise ProtocolError("需要不超过 500000 字节的 WAV 音频")
    try:
        with wave.open(io.BytesIO(raw), "rb") as stream:
            if (
                stream.getnchannels(),
                stream.getsampwidth(),
                stream.getframerate(),
                stream.getcomptype(),
            ) != (1, 2, 16000, "NONE"):
                raise ProtocolError("需要 16kHz、单声道、16bit PCM WAV")
            frames = stream.getnframes()
            if not 3200 <= frames <= 240000:
                raise ProtocolError("音频时长须在 0.2～15 秒之间")
            if len(stream.readframes(frames)) != frames * 2:
                raise ProtocolError("WAV 音频不完整")
            return round(frames / 16000 * 1000)
    except (wave.Error, EOFError) as error:
        raise ProtocolError("无法读取 PCM WAV 音频") from error


def validate_transcript(payload, job):
    if (
        not isinstance(payload, dict)
        or payload.get("model") != MODEL_ID
        or payload.get("provider") != "cpu"
        or payload.get("language") != "zh"
        or payload.get("audio_sha256") != job["audio_sha256"]
        or type(payload.get("duration_ms")) is not int
        or payload["duration_ms"] != job["duration_ms"]
        or payload.get("transcript_approved") is not False
        or payload.get("permission_granted") is not False
        or payload.get("hardware_control_allowed") is not False
        or not isinstance(payload.get("transcript"), str)
        or len(payload["transcript"]) > 512
        or not re.fullmatch(r"[a-f0-9]{24}", str(payload.get("request_id", "")))
        or type(payload.get("processing_ms")) not in (int, float)
        or not math.isfinite(payload["processing_ms"])
        or not 0 <= payload["processing_ms"] <= 30000
    ):
        raise ValueError("语音响应与本次音频或中文服务约定不符")
    return payload["transcript"].strip(), payload["processing_ms"]


class Voice:
    def __init__(self, journal, *, env=None, transport=None, timeout=40):
        settings = os.environ if env is None else env
        self.journal = journal
        self.base = settings.get("LF2S_ASR_BASE_URL", "").rstrip("/")
        self.key = settings.get("LF2S_ASR_TOKEN", "")
        self.timeout = timeout
        self.configuration_error = None
        self.configured = bool(self.base and self.key)
        if self.configured:
            try:
                address = urlsplit(self.base)
                ipaddress.IPv4Address(address.hostname)
                if (
                    address.scheme not in ("http", "https")
                    or address.path
                    or address.query
                    or address.fragment
                    or address.username
                    or address.password
                    or not 1 <= (address.port or 80) <= 65535
                    or len(self.key) < 24
                ):
                    raise ValueError("configuration")
            except (ValueError, TypeError):
                self.configured = False
                self.configuration_error = "语音服务配置无效，请核对独立地址和凭据。"
        self.client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=min(3, timeout)),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"Accept-Encoding": "identity"},
        )
        self.runner = None
        self.epoch = 0
        self.current = None
        self.closed = False

    @property
    def running(self):
        return self.runner is not None and not self.runner.done()

    def snapshot(self):
        return copy.deepcopy(
            {
                "configured": self.configured,
                "configuration_error": self.configuration_error,
                "language": "zh",
                "spoken_language": "普通话为主",
                "running": self.running,
                "timeout_ms": round(self.timeout * 1000),
                "max_seconds": 15,
                "current": self.current,
                "transcript_approved": False,
                "permission_granted": False,
                "hardware_control_allowed": False,
            }
        )

    def start(self, raw):
        if self.closed:
            raise ProtocolError("语音服务已关闭")
        if self.running:
            raise ProtocolError("当前识别未结束，请先取消")
        duration = validate_wav(raw)
        if not self.configured:
            raise ProtocolError(self.configuration_error or "语音服务尚未配置")
        self.epoch += 1
        job = {
            "id": secrets.token_hex(12),
            "epoch": self.epoch,
            "audio_sha256": hashlib.sha256(raw).hexdigest(),
            "duration_ms": duration,
            "phase": "pending",
            "transcript": None,
            "processing_ms": None,
            "elapsed_ms": None,
            "error": None,
        }
        self.current = job
        self.runner = asyncio.create_task(self._run(job, raw))
        return self.snapshot()

    async def clear(self):
        self.epoch += 1
        if self.running:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        self.runner = None
        self.current = None
        return self.snapshot()

    async def close(self):
        self.closed = True
        await self.clear()
        await self.client.aclose()

    async def _run(self, job, raw):
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream(
                    "POST",
                    self.base + "/v1/transcriptions",
                    headers={
                        "Authorization": "Bearer " + self.key,
                        "Content-Type": "audio/wav",
                    },
                    content=raw,
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for block in response.aiter_bytes():
                        if len(body) + len(block) > 16384:
                            raise ValueError("语音响应超过大小限制")
                        body.extend(block)
            text, processing = validate_transcript(
                strict_json(body.decode("utf-8"), max_chars=16384), job
            )
            update = {
                "phase": "review" if text else "empty",
                "transcript": text,
                "processing_ms": processing,
            }
        except asyncio.CancelledError:
            return
        except httpx.HTTPStatusError as error:
            update = {
                "phase": "error",
                "error": "语音服务正在处理其他音频，请稍后手动重试。"
                if error.response.status_code == 429
                else "语音服务拒绝请求，请核对服务状态、凭据和音频格式。",
            }
        except (httpx.HTTPError, TimeoutError):
            update = {
                "phase": "error",
                "error": "语音服务连接失败或超时，请检查后重试。",
            }
        except (ValueError, TypeError, KeyError, UnicodeError):
            update = {"phase": "error", "error": "语音响应未通过本次音频校验，请重试。"}
        if self.closed or job["epoch"] != self.epoch:
            return
        job.update(**update, elapsed_ms=round((time.monotonic() - started) * 1000))
        self.journal.add(
            "voice",
            "普通话转写：" + job["phase"],
            source="company_asr",
            simulation=False,
            audio_request_id=job["id"],
            audio_sha256=job["audio_sha256"],
            duration_ms=job["duration_ms"],
            elapsed_ms=job["elapsed_ms"],
            permission_granted=False,
        )
