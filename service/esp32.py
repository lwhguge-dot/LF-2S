"""Display-only HTTP provider for the paired, uncalibrated ESP32 bench."""

import asyncio
import http.client
import io
import ipaddress
import json
import re
import socket
import threading
import time

from lf2s_protocol.version import DIALECT_HASH, VERSION
from lf2s_protocol.wire import ProtocolError
from PIL import Image

MAX_JPEG = 262144
PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class TransportError(ProtocolError):
    """A finite HTTP transfer failed; it does not establish a device reboot."""

    def __init__(self, code, *, elapsed_ms=0, os_error=None):
        super().__init__(code)
        self.code = code
        self.elapsed_ms = elapsed_ms
        self.os_error = os_error


def target(host, port):
    try:
        address = ipaddress.IPv4Address(host)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("请输入 ESP32 热点的 IPv4 地址") from exc
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ProtocolError("仅允许局域网 IPv4 地址")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ProtocolError("端口必须在 1–65535 之间")
    return str(address), port


def unsigned(value, bits, *, text=False, positive=False):
    if text:
        if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,19}", value):
            raise ProtocolError("相机编号格式无效")
        number = int(value)
    else:
        if type(value) is not int:
            raise ProtocolError("相机数值格式无效")
        number = value
    if not (1 if positive else 0) <= number < 1 << bits:
        raise ProtocolError("相机编号超出范围")
    return number


def http_request(host, port, method, route, limit):
    """No environment proxy, redirect, configurable path or unbounded download."""
    connection = http.client.HTTPConnection(host, port, timeout=1)
    timer = None
    expired = threading.Event()
    started = time.monotonic()
    try:
        connection.connect()
        owned = connection.sock
        # A frame wait can take four seconds in esp32-camera. A display timeout
        # must cover capture plus transfer; it is not an exposure-age guarantee.
        duration = 6 if route.startswith("/preview?") else 5 if route == "/focus" else 2
        owned.settimeout(duration)

        def expire():
            expired.set()
            try:
                owned.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(duration, expire)
        timer.daemon = True
        timer.start()
        connection.request(method, route, headers={"Connection": "close", "Content-Length": "0"})
        response = connection.getresponse()
        pairs = response.getheaders()
        headers = {key.lower(): value for key, value in pairs}
        if len(headers) != len(pairs) or headers.get("transfer-encoding"):
            raise ProtocolError("ESP32 HTTP 响应头无效")
        length = headers.get("content-length", "")
        if not re.fullmatch(r"[0-9]{1,7}", length) or int(length) > limit:
            raise ProtocolError("ESP32 响应长度无效或超过上限")
        data = response.read(int(length))
        if expired.is_set():
            raise TransportError("response_timeout")
        if len(data) != int(length):
            raise TransportError("incomplete_response")
        return response.status, headers, data
    except (OSError, http.client.HTTPException, TransportError) as exc:
        code = "response_timeout" if expired.is_set() or isinstance(exc, TimeoutError) else (
            exc.code if isinstance(exc, TransportError) else "connection_interrupted"
        )
        raise TransportError(code, elapsed_ms=round((time.monotonic() - started) * 1000),
                             os_error=getattr(exc, "winerror", None) or getattr(exc, "errno", None)) from exc
    finally:
        if timer:
            timer.cancel()
        connection.close()


def health_data(headers, data):
    if headers.get("content-type") != "application/json":
        raise ProtocolError("ESP32 状态格式无效")
    try:
        health = json.loads(data)
        if not isinstance(health, dict):
            raise ValueError
        for field in ("simulation", "hardware_qualified", "flight_qualified", "control_forwarding",
                      "uart_ready", "physical_freshness_qualified"):
            if health[field] is not False:
                raise ValueError
        if (health["protocol"] != VERSION or health["dialect_hash"] != str(DIALECT_HASH)
                or not isinstance(health["firmware"], str)
                or not re.fullmatch(r"0\.1\.[0-9]{1,3}", health["firmware"])
                or int(health["firmware"].split(".")[2]) < 3
                or health["sensor_pid"] != 0x5640 or health["calibration_id"] != 0
                or health["camera_ready"] is not True):
            raise ValueError
        unsigned(health["camera_stream_id"], 64, text=True, positive=True)
        for field in ("preview_width", "preview_height"):
            if not 1 <= unsigned(health[field], 16, positive=True) <= 1600:
                raise ValueError
        for field in ("autofocus_initialized", "autofocus_focused", "autofocus_busy"):
            if type(health[field]) is not bool:
                raise ValueError
        unsigned(health["autofocus_status"], 8)
        for field in ("uptime_ms", "psram_bytes", "heap_free_bytes", "camera_failures",
                      "udp_received", "udp_validated", "udp_rejected"):
            unsigned(health[field], 53)
        if not 8 <= unsigned(health["jpeg_quality"], 8) <= 30:
            raise ValueError
        unsigned(health["autofocus_error"], 32)
    except (ValueError, KeyError, TypeError, ProtocolError) as exc:
        raise ProtocolError("板卡协议或台架配置不匹配；需要 0.1.3 相机台架固件") from exc
    # Only expose bounded, known diagnostics to the renderer.
    fields = ("firmware", "protocol", "dialect_hash", "sensor_pid", "camera_stream_id",
              "preview_width", "preview_height", "jpeg_quality", "autofocus_initialized",
              "autofocus_focused", "autofocus_busy", "autofocus_status", "autofocus_error",
              "uptime_ms", "psram_bytes", "heap_free_bytes", "camera_failures", "udp_received",
              "udp_validated", "udp_rejected", "uart_ready", "control_forwarding",
              "physical_freshness_qualified", "calibration_id")
    return {field: health.get(field) for field in fields}


def preview_data(headers, data, stream, request_id, health, previous):
    expected = {
        "content-type": "image/jpeg", "x-lf2s-stream-id": stream,
        "x-lf2s-request-id": str(request_id), "x-lf2s-display-only": "1",
        "x-lf2s-capture-after-request": "0", "x-lf2s-calibration-id": "0",
    }
    if any(headers.get(key) != value for key, value in expected.items()):
        raise ProtocolError("实物预览身份或显示用途标记不匹配")
    try:
        frame = unsigned(headers["x-lf2s-frame-id"], 32, text=True, positive=True)
        captured = unsigned(headers["x-lf2s-capture-us"], 64, text=True, positive=True)
        width = unsigned(headers["x-lf2s-width"], 16, text=True, positive=True)
        height = unsigned(headers["x-lf2s-height"], 16, text=True, positive=True)
        if (frame <= previous[0] or captured <= previous[1]
                or width != health["preview_width"] or height != health["preview_height"]
                or len(data) < 4 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9")):
            raise ValueError
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or image.size != (width, height):
                raise ValueError
            image.load()
    except (ValueError, KeyError, OSError, Image.DecompressionBombError) as exc:
        raise ProtocolError("实物预览 JPEG、尺寸或递增编号无效") from exc
    return frame, captured, width, height


class Esp32Bench:
    def __init__(self, journal):
        self.journal = journal
        self.active = False
        self.phase = "disconnected"
        self.reason = None
        self.host, self.port = "192.168.4.1", 80
        self.epoch = 0
        self.health = None
        self.health_at = 0
        self.request_id = 0
        self.previous = (0, 0)
        self.lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()
        self.paired_stream = None
        self.retry_at = 0
        self.transport_failures = 0
        self.recoveries = 0
        self.failure_streak = 0
        self.last_error = None

    def snapshot(self):
        age = round((time.monotonic() - self.health_at) * 1000) if self.health else None
        return {"active": self.active, "phase": self.phase, "reason": self.reason,
                "host": self.host, "port": self.port, "epoch": self.epoch,
                "health": self.health if age is not None and age < 4000 else None,
                "health_age_ms": age, "display_only": True, "hardware_control_allowed": False,
                "transport_failures": self.transport_failures, "recoveries": self.recoveries,
                "last_error": self.last_error}

    def disconnect(self):
        self.epoch += 1
        self.active = False
        self.phase = "disconnected"
        self.reason = None
        self.health = None
        self.previous = (0, 0)
        self.paired_stream = None
        self.failure_streak = 0
        self.last_error = None
        return self.snapshot()

    def fail(self, reason):
        self.epoch += 1
        self.phase = "error"
        self.reason = reason
        self.health = None
        self.previous = (0, 0)
        self.journal.add("warning", reason, source="esp32_bench", simulation=False)

    def recover(self, method, route, error):
        self.transport_failures += 1
        self.failure_streak += 1
        self.last_error = {"code": error.code, "endpoint": route.split("?")[0],
                           "elapsed_ms": error.elapsed_ms, "os_error": error.os_error}
        self.journal.add("warning", "ESP32 HTTP 传输暂时失败", source="esp32_bench", simulation=False,
                         **self.last_error, method=method, attempt=self.failure_streak)
        if self.failure_streak >= 3:
            self.fail("连续三次无法读取 ESP32；请确认电脑仍连接板卡热点，再断开并重新连接")
        else:
            self.phase = "recovering"
            self.health = None
            label = "响应超时" if error.code == "response_timeout" else "传输中断"
            self.reason = f"ESP32 {label}；正在重新核对当前板卡（{self.failure_streak}/3）"
            self.retry_at = time.monotonic() + .75 * self.failure_streak

    async def exchange(self, method, route, limit, epoch):
        try:
            answer = await asyncio.to_thread(http_request, self.host, self.port, method, route, limit)
        except (OSError, http.client.HTTPException, TransportError) as exc:
            if epoch != self.epoch:
                raise ProtocolError("实物连接已改变，丢弃迟到响应") from exc
            error = exc if isinstance(exc, TransportError) else TransportError("connection_interrupted")
            self.recover(method, route, error)
            raise ProtocolError(self.reason) from exc
        except ProtocolError as exc:
            if epoch == self.epoch:
                self.fail(str(exc))
            raise
        if epoch != self.epoch or not self.active:
            raise ProtocolError("实物连接已改变，丢弃迟到响应")
        return answer

    async def connect(self, host, port):
        if self.active or self.lock.locked():
            raise ProtocolError("请先断开实物连接，再建立新连接")
        self.host, self.port = target(host, port)
        self.disconnect()
        self.active = True
        self.phase = "connecting"
        self.request_id = 0
        async with self.lock:
            await self.read_health(self.epoch)
        self.journal.add("connection", "ESP32 实物台架已连接；仅图像和板卡诊断", source="esp32_bench", simulation=False)
        return self.snapshot()

    async def read_health(self, epoch):
        code, headers, data = await self.exchange("GET", "/health", 8192, epoch)
        try:
            if code != 200:
                raise ProtocolError("ESP32 状态请求失败")
            health = health_data(headers, data)
            if self.paired_stream and health["camera_stream_id"] != self.paired_stream:
                raise ProtocolError("摄像头启动代数已改变；请断开并重新连接")
        except ProtocolError as exc:
            self.fail(str(exc))
            raise
        if self.phase == "recovering":
            self.recoveries += 1
            self.journal.add("connection", "同一相机流已恢复响应，预览可继续", source="esp32_bench", simulation=False)
        self.paired_stream = health["camera_stream_id"]
        self.health = health
        self.health_at = time.monotonic()
        self.phase = "online"
        self.reason = None
        self.failure_streak = 0

    async def refresh(self):
        if (not self.active or self.phase not in ("online", "recovering") or self.lock.locked()
                or self.command_lock.locked() or (self.phase == "recovering" and time.monotonic() < self.retry_at)):
            return
        async with self.lock:
            await self.read_health(self.epoch)

    def require_online(self):
        if not self.active or self.phase != "online" or not self.health:
            raise ProtocolError("ESP32 实物台架未在线")
        if self.command_lock.locked():
            raise ProtocolError("摄像头正在处理另一请求")

    async def preview(self):
        self.require_online()
        epoch = self.epoch
        async with self.command_lock, self.lock:
            if epoch != self.epoch:
                raise ProtocolError("实物连接已改变，丢弃排队图像请求")
            if self.phase != "online":
                raise ProtocolError(self.reason or "ESP32 正在恢复响应")
            if time.monotonic() - self.health_at > 1.5:
                await self.read_health(epoch)
            if self.request_id >= 0xFFFFFFFF:
                self.fail("相机请求编号已耗尽；请断开并重新连接")
                raise ProtocolError(self.reason)
            self.request_id += 1
            request_id = self.request_id
            stream = self.health["camera_stream_id"]
            route = f"/preview?request_id={request_id}&stream_id={stream}"
            code, headers, data = await self.exchange("GET", route, MAX_JPEG, epoch)
            if code == 429:
                raise ProtocolError("摄像头暂忙，请稍后刷新")
            if code in (503, 504):
                self.recover("GET", route, TransportError("camera_frame_unavailable"))
                raise ProtocolError(self.reason)
            try:
                if code != 200:
                    raise ProtocolError("摄像头拒绝当前流；请断开并重新连接")
                meta = await asyncio.to_thread(
                    preview_data, headers, data, stream, request_id, self.health, self.previous
                )
                if epoch != self.epoch:
                    raise ProtocolError("实物连接已改变，丢弃迟到图像")
            except ProtocolError as exc:
                if epoch == self.epoch:
                    self.fail(str(exc))
                raise
            self.previous = meta[:2]
            frame, captured, width, height = meta
            return data, {"X-LF2S-Synthetic": "0", "X-LF2S-Display-Only": "1",
                          "X-LF2S-Hardware-Qualified": "0", "X-LF2S-Capture-After-Request": "0",
                          "X-LF2S-Stream-Id": stream, "X-LF2S-Frame-Id": str(frame),
                          "X-LF2S-Capture-Us": str(captured), "X-LF2S-Request-Id": str(request_id),
                          "X-LF2S-Width": str(width), "X-LF2S-Height": str(height),
                          "X-LF2S-Epoch": str(epoch)}

    async def focus(self):
        self.require_online()
        if self.health["autofocus_initialized"] is not True:
            raise ProtocolError("此摄像头未提供可用的自动对焦")
        epoch = self.epoch
        async with self.command_lock, self.lock:
            if epoch != self.epoch:
                raise ProtocolError("实物连接已改变，丢弃排队对焦请求")
            if self.phase != "online":
                raise ProtocolError(self.reason or "ESP32 正在恢复响应")
            code, headers, data = await self.exchange("POST", "/focus", 1024, epoch)
            try:
                accepted = code == 200 and headers.get("content-type") == "application/json" and json.loads(data).get("accepted") is True
            except (ValueError, AttributeError):
                accepted = False
            if not accepted:
                raise ProtocolError("重新对焦请求未被接受")
            self.health = {**self.health, "autofocus_focused": False, "autofocus_busy": True}
            self.journal.add("camera", "已请求重新对焦；完成状态以传感器回报为准", source="esp32_bench", simulation=False)
            return {"accepted": True, "focused": False}
