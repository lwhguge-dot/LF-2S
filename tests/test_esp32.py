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

    def request(self, host, port, method, route, limit):
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
        for name in ("bench/connect", "bench/focus"):
            assert client.post(route + name, json={}).status_code == 409
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
