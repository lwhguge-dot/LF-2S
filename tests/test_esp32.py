"""ESP32 HTTP contract, JPEG gate and late-response isolation; no hardware."""

import asyncio
import http.client
import io
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]
from lf2s_protocol.wire import ProtocolError  # noqa: E402

from service.app import create_app  # noqa: E402
from service.esp32 import (  # noqa: E402
    Esp32Bench,
    HttpSession,
    TransportError,
    health_data,
    http_request,
    preview_data,
    target,
)


def health():
    return {"firmware": "0.1.3", "protocol": "2.0.0-draft1", "dialect_hash": "5889088805303263",
            "simulation": False, "hardware_qualified": False, "flight_qualified": False,
            "control_forwarding": False, "uart_ready": False, "physical_freshness_qualified": False,
            "sensor_pid": 0x5640, "calibration_id": 0, "camera_ready": True,
            "camera_stream_id": "18446744073709551614", "preview_width": 800,
            "preview_height": 600, "jpeg_quality": 10, "autofocus_initialized": True,
            "autofocus_focused": False, "autofocus_busy": True, "autofocus_status": 32,
            "autofocus_error": 0, "uptime_ms": 5000, "psram_bytes": 8388608,
            "heap_free_bytes": 8000000, "camera_failures": 0, "udp_received": 0,
            "udp_validated": 0, "udp_rejected": 0}


def jpeg_reply(request_id=1, frame_id=1, **changes):
    image = Image.new("RGB", (800, 600), "green")
    output = io.BytesIO()
    image.save(output, format="JPEG")
    data = output.getvalue()
    headers = {"content-type": "image/jpeg", "content-length": str(len(data)),
               "x-lf2s-stream-id": health()["camera_stream_id"],
               "x-lf2s-request-id": str(request_id), "x-lf2s-display-only": "1",
               "x-lf2s-capture-after-request": "0", "x-lf2s-calibration-id": "0",
               "x-lf2s-frame-id": str(frame_id), "x-lf2s-capture-us": "9007199254740997",
               "x-lf2s-width": "800", "x-lf2s-height": "600"}
    headers.update(changes)
    return 200, headers, data


class Journal:
    def add(self, *args, **kwargs):
        pass


class Board:
    def __init__(self):
        self.health = health()
        self.calls = []
        self.frame = 0

    def request(self, host, port, method, route, limit, session=None):
        self.calls.append((host, port, method, route, limit))
        if route == "/health":
            return 200, {"content-type": "application/json"}, json.dumps(self.health).encode()
        if route.startswith("/preview?"):
            self.frame += 1
            request = int(route.split("request_id=")[1].split("&")[0])
            return jpeg_reply(request, self.frame)
        if route == "/focus":
            return 200, {"content-type": "application/json"}, b'{"accepted":true,"focused":false}'
        raise AssertionError(route)


def modern_health():
    return {**health(), "firmware": "0.1.5", "preview_colour": 0, "test_pattern": False,
            "capture_wait_us": 80000, "queue_wait_us": 1000, "send_us": 400000,
            "sensor_bench_frames": 20, "sensor_bench_us": 855000,
            "sensor_diagnostics": {"valid": True, "exposure_lines": 885, "gain_q4": 248,
                                   "gain_ceiling_q4": 248, "wb_r_q10": 1100, "wb_g_q10": 1024,
                                   "wb_b_q10": 2000, "manual_control": 0, "isp_control": 163,
                                   "sde_control": 6, "sde_u": 64, "sde_v": 64, "luminance": 50}}


class SettingsBoard(Board):
    def __init__(self):
        super().__init__()
        self.health = modern_health()
        self.offset = 1

    def request(self, host, port, method, route, limit, session=None):
        if not route.startswith("/preview/config?"):
            return super().request(host, port, method, route, limit, session)
        self.calls.append((host, port, method, route, limit))
        assert method == "POST" and "pattern=0" in route
        assert "stream_id=" + self.health["camera_stream_id"] in route
        size = route.split("size=")[1].split("&")[0]
        width, height, quality = {"vga": (640, 480, 12), "qvga": (320, 240, 16), "svga": (800, 600, 10)}[size]
        self.health.update(camera_stream_id=str(int(self.health["camera_stream_id"]) + self.offset),
                           preview_width=width, preview_height=height, jpeg_quality=quality,
                           preview_colour=2 if "colour=office" in route else 0)
        return 200, {"content-type": "application/json"}, json.dumps(
            {"accepted": True, "stream_id": self.health["camera_stream_id"], "test_pattern": False}
        ).encode()


def test_settings_change_stream_only_after_explicit_verified_configuration(monkeypatch):
    board = SettingsBoard()
    monkeypatch.setattr("service.esp32.http_request", board.request)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        old_stream, old_epoch = bench.paired_stream, bench.epoch
        answer = await bench.settings("vga", "driver")
        assert answer["phase"] == "online" and bench.epoch > old_epoch
        assert int(bench.paired_stream) == int(old_stream) + 1 == 2**64 - 1
        assert bench.health["preview_width"] == 640 and bench.previous == (0, 0)
        assert sum("/preview/config?" in row[3] for row in board.calls) == 1

    asyncio.run(run())


def test_settings_cannot_adopt_an_unexpected_boot_generation(monkeypatch):
    board = SettingsBoard()
    board.health["camera_stream_id"] = "9007199254740993"
    board.offset = 2
    monkeypatch.setattr("service.esp32.http_request", board.request)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        with pytest.raises(ProtocolError):
            await bench.settings("vga", "driver")
        assert bench.phase == "error" and bench.health is None
        assert bench.paired_stream == "9007199254740993"

    asyncio.run(run())


def test_ambiguous_settings_are_never_automatically_replayed(monkeypatch):
    board = SettingsBoard()

    def request(*args):
        reply = board.request(*args)
        if args[3].startswith("/preview/config?"):
            raise TransportError("response_timeout", elapsed_ms=8000)
        return reply

    monkeypatch.setattr("service.esp32.http_request", request)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        with pytest.raises(ProtocolError):
            await bench.settings("vga", "driver")
        assert bench.phase == "error" and bench.health is None
        count = len(board.calls)
        await bench.refresh()
        assert len(board.calls) == count
        assert sum("/preview/config?" in row[3] for row in board.calls) == 1

    asyncio.run(run())


def test_late_settings_result_cannot_restore_a_disconnected_board(monkeypatch):
    board = SettingsBoard()
    entered, release = threading.Event(), threading.Event()

    def request(*args):
        if args[3].startswith("/preview/config?"):
            entered.set()
            assert release.wait(2)
        return board.request(*args)

    monkeypatch.setattr("service.esp32.http_request", request)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        pending = asyncio.create_task(bench.settings("vga", "driver"))
        assert await asyncio.to_thread(entered.wait, 1)
        bench.disconnect()
        release.set()
        with pytest.raises(ProtocolError):
            await pending
        assert bench.phase == "disconnected" and bench.health is None and not bench.active

    asyncio.run(run())


def test_sensor_generated_pattern_cannot_become_a_real_scene(monkeypatch):
    board = SettingsBoard()
    board.health["test_pattern"] = True
    monkeypatch.setattr("service.esp32.http_request", board.request)

    async def run():
        bench = Esp32Bench(Journal())
        with pytest.raises(ProtocolError):
            await bench.connect("192.168.4.1", 80)
        assert bench.phase == "error" and bench.health is None

    asyncio.run(run())
    _, headers, data = jpeg_reply(**{"x-lf2s-test-pattern": "1"})
    with pytest.raises(ProtocolError):
        preview_data(headers, data, health()["camera_stream_id"], 1, modern_health(), (0, 0))


@pytest.mark.parametrize("always_busy", [False, True])
def test_busy_preview_has_one_bounded_read_retry_with_new_id(monkeypatch, always_busy):
    board = Board()
    attempts = []

    def request(*args):
        if args[3].startswith("/preview?"):
            attempts.append(args[3])
            if always_busy or len(attempts) == 1:
                return 429, {"content-type": "application/json"}, b'{"error":"camera_busy"}'
        return board.request(*args)

    monkeypatch.setattr("service.esp32.http_request", request)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        if always_busy:
            with pytest.raises(ProtocolError, match="暂忙"):
                await bench.preview()
            assert bench.camera_busy_responses == 2
        else:
            data, headers = await bench.preview()
            assert data.startswith(b"\xff\xd8") and headers["X-LF2S-Request-Id"] == "2"
            assert bench.camera_busy_responses == 1
        assert len(attempts) == 2 and "request_id=1&" in attempts[0] and "request_id=2&" in attempts[1]
        assert bench.phase == "online"

    asyncio.run(run())


@pytest.mark.parametrize("fault", [None, "timeout", "disconnect"])
def test_persistent_http_reuses_one_socket_and_discards_a_timed_out_read(fault):
    stall_second = fault is not None
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(3)
    observed = []
    second_entered = threading.Event()

    def serve():
        owned, _ = server.accept()
        owned.settimeout(3)
        with owned:
            for index in range(2):
                request = b""
                while b"\r\n\r\n" not in request:
                    part = owned.recv(4096)
                    if not part:
                        return
                    request += part
                observed.append(request)
                if index == 1:
                    second_entered.set()
                head = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n"
                try:
                    owned.sendall(head + (b"{" if index == 1 and stall_second else b"{}"))
                except OSError:
                    return
                if index == 1 and stall_second:
                    time.sleep(2.3)
                    try:
                        owned.sendall(b"}")
                    except OSError:
                        pass
            # Successful requests keep the socket until the caller disconnects.
            if not stall_second:
                assert owned.recv(1) == b""

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    session = HttpSession()
    try:
        args = ("127.0.0.1", server.getsockname()[1], "GET", "/health", 8192, session)
        assert http_request(*args)[2] == b"{}"
        connection = session.connection
        if fault == "disconnect":
            def disconnect():
                assert second_entered.wait(2)
                session.close()
            closer = threading.Thread(target=disconnect, daemon=True)
            closer.start()
        if stall_second:
            with pytest.raises(TransportError) as failed:
                http_request(*args)
            assert failed.value.code == ("response_timeout" if fault == "timeout" else "connection_interrupted")
            assert session.connection is None
        else:
            assert http_request(*args)[2] == b"{}" and session.connection is connection
        assert len(observed) == 2
        assert all(b"Connection: keep-alive\r\n" in request for request in observed)
    finally:
        session.close()
        server.close()
        thread.join(3)
    assert not thread.is_alive()
    if fault == "disconnect":
        closer.join(1)
        assert not closer.is_alive()


@pytest.mark.parametrize("host,port", [("127.0.0.1", 80), ("0.0.0.0", 80),
    ("169.254.169.254", 80), ("224.0.0.1", 80), ("8.8.8.8", 80), ("localhost", 80),
    ("http://192.168.4.1", 80), ("192.168.4.1/path", 80), ("192.168.4.1", True),
    ("192.168.4.1", 65536)])
def test_target_is_literal_private_ipv4_only(host, port):
    with pytest.raises(ProtocolError):
        target(host, port)


@pytest.mark.parametrize("key,value", [("dialect_hash", "1"), ("simulation", True),
    ("control_forwarding", True), ("uart_ready", True), ("calibration_id", 7),
    ("camera_stream_id", "18446744073709551616"), ("camera_stream_id", 123),
    ("preview_width", 65535), ("sensor_pid", 0x2640), ("firmware", "0.1.2")])
def test_mismatched_board_rejected(key, value):
    data = {**health(), key: value}
    with pytest.raises(ProtocolError):
        health_data({"content-type": "application/json"}, json.dumps(data).encode())


@pytest.mark.parametrize("key,value", [("x-lf2s-stream-id", "2"), ("x-lf2s-request-id", "2"),
    ("x-lf2s-frame-id", "0"), ("x-lf2s-calibration-id", "1"), ("x-lf2s-display-only", "0"),
    ("x-lf2s-capture-after-request", "1"), ("x-lf2s-width", "640"),
    ("x-lf2s-capture-us", "18446744073709551616")])
def test_preview_metadata_rejections(key, value):
    _, headers, data = jpeg_reply(**{key: value})
    with pytest.raises(ProtocolError):
        preview_data(headers, data, health()["camera_stream_id"], 1, health(), (0, 0))


def test_preview_decode_and_progress_are_required():
    _, headers, data = jpeg_reply()
    meta = preview_data(headers, data, health()["camera_stream_id"], 1, health(), (0, 0))
    assert meta[:2] == (1, 9007199254740997)
    for bad_data, previous in ((b"\xff\xd8invalid\xff\xd9", (0, 0)), (data, meta[:2])):
        with pytest.raises(ProtocolError):
            preview_data(headers, bad_data, health()["camera_stream_id"], 1, health(), previous)


def test_late_jpeg_discarded_after_disconnect(monkeypatch):
    board = Board()
    entered, release = threading.Event(), threading.Event()

    def delayed(*args):
        if args[3].startswith("/preview?"):
            entered.set()
            assert release.wait(3)
        return board.request(*args)

    monkeypatch.setattr("service.esp32.http_request", delayed)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        pending = asyncio.create_task(bench.preview())
        assert await asyncio.to_thread(entered.wait, 2)
        bench.disconnect()
        release.set()
        with pytest.raises(ProtocolError, match="迟到"):
            await pending
        assert not bench.active and bench.health is None

    asyncio.run(run())


def test_reboot_clears_health_and_requires_manual_new_connection(monkeypatch):
    board = Board()
    monkeypatch.setattr("service.esp32.http_request", board.request)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        await bench.preview()
        board.health["camera_stream_id"] = "18446744073709551615"
        with pytest.raises(ProtocolError, match="代数"):
            await bench.refresh()
        assert bench.phase == "error" and bench.health is None and bench.active
        with pytest.raises(ProtocolError):
            await bench.preview()

    asyncio.run(run())


def test_focus_waits_for_health_poll_and_rejects_second_command(monkeypatch):
    board = Board()
    entered, release = threading.Event(), threading.Event()

    def polling(*args):
        if args[3] == "/health" and board.calls:
            entered.set()
            assert release.wait(3)
        return board.request(*args)

    monkeypatch.setattr("service.esp32.http_request", polling)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        refresh = asyncio.create_task(bench.refresh())
        assert await asyncio.to_thread(entered.wait, 2)
        focus = asyncio.create_task(bench.focus())
        await asyncio.sleep(0)
        assert not focus.done()
        with pytest.raises(ProtocolError, match="另一请求"):
            await bench.preview()
        release.set()
        await refresh
        assert await focus == {"accepted": True, "focused": False}

    asyncio.run(run())


@pytest.mark.parametrize("endpoint", ["preview", "focus"])
def test_transient_failure_recovers_same_stream_without_replaying_focus(monkeypatch, endpoint):
    board = Board()
    failed = False

    def request(*args):
        nonlocal failed
        if args[3].startswith("/" + endpoint) and not failed:
            failed = True
            board.calls.append(args)
            raise TransportError("response_timeout", elapsed_ms=6000)
        return board.request(*args)

    monkeypatch.setattr("service.esp32.http_request", request)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        paired = bench.paired_stream
        with pytest.raises(ProtocolError):
            await (bench.preview() if endpoint == "preview" else bench.focus())
        assert bench.phase == "recovering" and bench.health is None and bench.paired_stream == paired
        assert bench.snapshot()["last_error"]["elapsed_ms"] == 6000
        bench.retry_at = 0
        await bench.refresh()
        assert bench.phase == "online" and bench.recoveries == 1
        jpeg, headers = await bench.preview()
        assert jpeg.startswith(b"\xff\xd8") and headers["X-LF2S-Stream-Id"] == paired
        if endpoint == "focus":
            assert len([call for call in board.calls if call[2] == "POST"]) == 1

    asyncio.run(run())


def test_recovery_stops_after_three_transport_failures(monkeypatch):
    board = Board()
    monkeypatch.setattr("service.esp32.http_request", board.request)

    def unavailable(*args):
        raise TransportError("connection_interrupted", os_error=10054)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        monkeypatch.setattr("service.esp32.http_request", unavailable)
        for index in range(3):
            bench.retry_at = 0
            with pytest.raises(ProtocolError):
                await bench.refresh()
            assert bench.phase == ("recovering" if index < 2 else "error")
        await bench.refresh()
        assert bench.transport_failures == 3 and bench.health is None
        with pytest.raises(ProtocolError):
            await bench.preview()

    asyncio.run(run())


def test_recovery_does_not_adopt_rebooted_stream(monkeypatch):
    board = Board()
    monkeypatch.setattr("service.esp32.http_request", board.request)

    async def run():
        bench = Esp32Bench(Journal())
        await bench.connect("192.168.4.1", 80)
        original = bench.paired_stream
        bench.recover("GET", "/preview?request_id=1", TransportError("response_timeout"))
        board.health["camera_stream_id"] = "18446744073709551615"
        bench.retry_at = 0
        with pytest.raises(ProtocolError, match="代数"):
            await bench.refresh()
        assert bench.phase == "error" and bench.paired_stream == original and bench.health is None

    asyncio.run(run())


def test_preview_transfer_can_exceed_two_seconds_without_dropping_connection():
    code, headers, jpeg = jpeg_reply()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def slow_frame():
        owned, _ = server.accept()
        with owned:
            owned.recv(4096)
            head = f"HTTP/1.1 {code} OK\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
            owned.sendall(head.encode() + jpeg[:100])
            time.sleep(2.2)
            owned.sendall(jpeg[100:])

    thread = threading.Thread(target=slow_frame, daemon=True)
    thread.start()
    try:
        status, observed, data = http_request("127.0.0.1", server.getsockname()[1], "GET", "/preview?request_id=1", 262144)
        assert status == 200 and data == jpeg
        assert preview_data(observed, data, health()["camera_stream_id"], 1, health(), (0, 0))[:2] == (1, 9007199254740997)
    finally:
        server.close()
        thread.join(3)


def test_api_simulation_replay_and_real_board_isolation(tmp_path, monkeypatch):
    board = Board()
    monkeypatch.setattr("service.esp32.http_request", board.request)
    app = create_app(ROOT, "test-token", 41234, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:41234", client=("127.0.0.1", 12345),
                    headers={"Authorization": "Bearer test-token"}) as client:
        route = "/api/v1/"
        response = client.post(route + "bench/connect", json={"host": "127.0.0.1", "port": 80})
        assert response.status_code == 409 and not board.calls
        assert client.post(route + "bench/connect", json={}).status_code == 200
        state = client.get(route + "state").json()
        assert state["simulation"] is False and state["read_only"] and state["mode"] == "esp32_bench"
        assert state["hardware_control_allowed"] is False
        assert all(not x["valid"] and x["value"] is None for x in state["sensors"].values())
        for name, body in (("connect", {}), ("profile", {}), ("tasks/start", {"confirmed": True}),
                           ("simulation/scenario", {"name": "healthy"})):
            assert client.post(route + name, json=body).status_code == 409
        assert client.get(route + "image").status_code == 409
        response = client.get(route + "bench/preview")
        assert response.status_code == 200 and response.headers["x-lf2s-synthetic"] == "0"
        assert response.headers["x-lf2s-stream-id"] == "18446744073709551614"
        assert response.headers["x-lf2s-capture-us"] == "9007199254740997"
        assert client.post(route + "bench/focus", json={}).json() == {"accepted": True, "focused": False}
        app.state.adapter.replaying = True
        count = len(board.calls)
        for name, body in (("bench/connect", {}), ("bench/focus", {}),
                           ("bench/settings", {"size": "vga", "colour": "driver"})):
            assert client.post(route + name, json=body).status_code == 409
        assert client.get(route + "bench/preview").status_code == 409
        assert len(board.calls) == count
        app.state.adapter.replaying = False
        assert client.post(route + "bench/disconnect").status_code == 200
        assert client.get(route + "state").json()["simulation"] is True
        assert client.get(route + "bench/preview").status_code == 409


def test_http_absolute_deadline_interrupts_slow_headers():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def slow():
        owned, _ = server.accept()
        with owned:
            owned.recv(4096)
            try:
                for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                    owned.sendall(bytes([byte]))
                    time.sleep(0.1)
            except OSError:
                pass

    thread = threading.Thread(target=slow, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises((OSError, http.client.HTTPException, ProtocolError)):
            http_request("127.0.0.1", server.getsockname()[1], "GET", "/health", 8192)
        assert time.monotonic() - started < 3.5
    finally:
        server.close()
        thread.join(3)
