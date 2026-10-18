"""USB parser/provider/API checks with injected reports; these are not hardware evidence."""

import asyncio
import copy
import json
import queue
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]
from lf2s_protocol.wire import ProtocolError  # noqa: E402

from service.app import create_app  # noqa: E402
from service.fc_usb import FcUsbBench  # noqa: E402
from service.zin_hid import StreamParser, decode  # noqa: E402

DEVICE = {
    "vid": 0x0483,
    "pid": 0xA001,
    "product": "ZIN_QUAD",
    "manufacturer": "ZINXD_HID",
    "path": "injected-test-device",
    "device_revision": 0x0300,
    "serial": "S",
    "caps": {"UsagePage": 0x8C, "Usage": 1, "InputReportByteLength": 65},
}


def frame(function, payload):
    body = b"\xaa\xaa" + bytes((function, len(payload))) + payload
    return body + bytes((sum(body) & 255,))


def reports(stream):
    return [
        bytes((0, len(stream[index : index + 63])))
        + stream[index : index + 63].ljust(63, b"\x00")
        for index in range(0, len(stream), 63)
    ]


STATUS = frame(1, struct.pack(">hhhiBB", -1234, 2500, -17999, 0, 1, 0))
IMU = frame(2, struct.pack(">9h", -32768, 32767, 8192, -1, 0, 1, 0, 0, 0))
RC = frame(3, struct.pack(">10H", 1000, 1500, 1499, 1501, *([1000] * 6)))


class Journal:
    def __init__(self):
        self.events = []

    def add(self, *args, **kwargs):
        self.events.append((args, kwargs))


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


class Hid:
    def __init__(self, data=STATUS + IMU + RC, device=None):
        self.device = copy.deepcopy(device or DEVICE)
        self.queue = queue.Queue()
        self.closed, self.opened = [], []
        self.k = SimpleNamespace(CloseHandle=lambda handle: self.closed.append(handle))
        self.push(data)

    def push(self, data):
        for report in reports(data):
            self.queue.put(report)

    def enumerate(self):
        return [self.device]

    def open(self, path, *, read):
        assert read is True
        self.opened.append((path, read))
        return 123

    def read(self, handle, size, timeout_ms):
        assert (handle, size, timeout_ms) == (123, 65, 250)
        try:
            item = self.queue.get(timeout=0.02)
        except queue.Empty:
            return None
        if isinstance(item, Exception):
            raise item
        return item


async def wait_for(provider, predicate):
    for _ in range(200):
        state = provider.snapshot()
        if predicate(state):
            return state
        await asyncio.sleep(0.005)
    raise AssertionError(provider.snapshot())


def test_parser_every_split_and_signed_fields():
    stream = STATUS + IMU + RC
    for split in range(len(stream) + 1):
        parser = StreamParser()
        assert parser.feed(stream[:split]) + parser.feed(stream[split:]) == [
            STATUS,
            IMU,
            RC,
        ]
    data = decode(STATUS)
    assert (data["roll_deg"], data["pitch_deg"], data["yaw_deg"]) == (
        -12.34,
        25,
        -179.99,
    )
    assert (data["status_byte10_raw"], data["status_byte11_raw"]) == (1, 0)
    assert "unlock_raw" not in data
    assert decode(IMU)["acc_x_raw"] == -32768


def test_passive_capture_unknowns_and_raw_bytes(tmp_path):
    api, journal = Hid(), Journal()

    async def scenario():
        provider = FcUsbBench(journal, tmp_path, api_factory=lambda: api)
        try:
            await provider.connect()
            state = await wait_for(provider, lambda s: s["counters"].get("frames") == 3)
            assert state["attitude"]["value"]["roll_deg"] == -12.34
            assert state["imu_raw"]["value"]["acc_y_raw"] == 32767
            assert state["status_raw"]["value"]["status_byte10_raw"] == 1
            assert state["attitude"]["sample_age_ms"] is None
            assert state["attitude"]["measurement_qualified"] is False
            for key in (
                "receiver_link_valid",
                "battery_v",
                "height_m",
                "position_m",
                "firmware_version",
                "mcu_model",
                "boot_id",
                "session_id",
            ):
                assert state[key] is None
            assert state["hardware_control_allowed"] is False
            assert state["mavlink_session_established"] is False
        finally:
            closed = await provider.disconnect()
        assert closed["attitude"]["value"] is None and closed["history"] == []
        assert api.closed == [123] and api.opened == [(DEVICE["path"], True)]

    asyncio.run(scenario())
    raw = [
        json.loads(line)
        for line in (tmp_path / "fc-usb.jsonl").read_text().splitlines()
    ]
    assert [item["report_hex"] for item in raw if "report_hex" in item] == [
        report.hex() for report in reports(STATUS + IMU + RC)
    ]
    assert len({item["connection_id"] for item in raw}) == 1
    assert journal.events[0][1]["simulation"] is False


def test_stale_measurements_cannot_be_refreshed_by_other_frames(tmp_path):
    api, clock = Hid(), Clock()

    async def scenario():
        provider = FcUsbBench(Journal(), tmp_path, api_factory=lambda: api, clock=clock)
        try:
            await provider.connect()
            await wait_for(provider, lambda s: s["counters"].get("frames") == 3)
            clock.value += 1.001
            state = provider.snapshot()
            assert state["phase"] == "stale" and state["attitude"]["value"] is None
            api.push(RC)
            state = await wait_for(provider, lambda s: s["rc_raw"]["available"])
            assert state["phase"] == "online"
            assert state["attitude"]["value"] is None
            assert state["imu_raw"]["value"] is None
            assert state["receiver_link_valid"] is None
            api.push(STATUS)
            state = await wait_for(provider, lambda s: s["attitude"]["available"])
            assert state["attitude"]["received_age_ms"] == 0
        finally:
            await provider.disconnect()

    asyncio.run(scenario())


def test_bad_crc_and_report_format_do_not_refresh_measurements(tmp_path):
    api = Hid(data=b"")
    api.queue.put(bytes((1, 0)) + bytes(63))
    bad = STATUS[:-1] + bytes((STATUS[-1] ^ 1,))
    api.push(bad + STATUS)

    async def scenario():
        provider = FcUsbBench(Journal(), tmp_path, api_factory=lambda: api)
        try:
            await provider.connect()
            state = await wait_for(provider, lambda s: s["attitude"]["available"])
            assert state["counters"]["report_errors"] == 1
            assert state["counters"]["checksum_failures"] == 1
            assert state["counters"]["frames"] == 1
        finally:
            await provider.disconnect()

    asyncio.run(scenario())


def test_usb_removal_invalidates_without_automatic_reconnect(tmp_path):
    api = Hid()

    async def scenario():
        provider = FcUsbBench(Journal(), tmp_path, api_factory=lambda: api)
        try:
            await provider.connect()
            await wait_for(provider, lambda s: s["attitude"]["available"])
            api.queue.put(OSError("injected device removed"))
            state = await wait_for(provider, lambda s: s["phase"] == "error")
            assert state["attitude"]["value"] is None and state["history"] == []
            assert state["counters"]["read_errors"] == 1
            with pytest.raises(ProtocolError, match="先断开"):
                await provider.connect()
        finally:
            await provider.disconnect()
        assert api.closed == [123]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"product": "other"},
        {"vid": 1},
        {"caps": {"UsagePage": 0x8C, "Usage": 1, "InputReportByteLength": 64}},
    ],
)
def test_wrong_identity_or_caps_never_open_reader(tmp_path, change):
    api = Hid(device={**DEVICE, **change})

    async def scenario():
        provider = FcUsbBench(Journal(), tmp_path, api_factory=lambda: api)
        try:
            with pytest.raises(ProtocolError):
                await provider.connect()
            assert api.opened == [] and provider.snapshot()["attitude"]["value"] is None
        finally:
            await provider.disconnect()

    asyncio.run(scenario())


def test_api_mode_isolation_and_no_output_endpoint(tmp_path):
    api = Hid()
    app = create_app(ROOT, "usb-test-token", 41234, tmp_path)
    app.state.usb_fc.api_factory = lambda: api
    headers = {"Authorization": "Bearer usb-test-token"}
    with TestClient(
        app,
        base_url="http://127.0.0.1:41234",
        client=("127.0.0.1", 12345),
        headers=headers,
    ) as client:
        assert (
            client.post("/api/v1/fc-usb/connect", json={"bytes": "00"}).status_code
            == 422
        )
        assert client.post("/api/v1/fc-usb/connect").status_code == 200
        state = client.get("/api/v1/state").json()
        assert state["mode"] == "hardware_bench" and state["simulation"] is False
        assert state["read_only"] is True and state["hardware_control_allowed"] is False
        assert state["link"]["phase"] == "disconnected"
        assert state["identity"]["boot_id"] is None
        assert all(sensor["value"] is None for sensor in state["sensors"].values())
        assert client.get("/api/v1/health").json()["simulation"] is False
        for route, body in (
            ("connect", None),
            ("profile", None),
            ("tasks/start", {"confirmed": True}),
            ("simulation/scenario", {"name": "healthy"}),
        ):
            assert client.post("/api/v1/" + route, json=body).status_code == 409
        assert client.get("/api/v1/image").status_code == 409
        assert (
            client.post("/api/v1/fc-usb/write", json={"data": "00"}).status_code == 404
        )
        # A known recording can open only after the hardware reader is closed.
        app.state.adapter.journal.recorder.frame = lambda *_: {
            "historical": True,
            "state": {},
        }
        assert (
            client.post(
                "/api/v1/replay/open", json={"recording_id": "a" * 24}
            ).status_code
            == 200
        )
        state = client.get("/api/v1/state").json()
        assert (
            state["usb_fc"]["active"] is False
            and state["usb_fc"]["attitude"]["value"] is None
        )
        assert client.post("/api/v1/fc-usb/connect").status_code == 409
        assert client.post("/api/v1/replay/close").status_code == 200
        assert client.get("/api/v1/state").json()["usb_fc"]["active"] is False
    assert api.closed == [123]
