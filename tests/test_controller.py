"""Current diagnostic reports, saved compiled C bytes and lifecycle isolation."""

import json
import sys
from collections import deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]
from lf2s_protocol.model import ACTION, CONTROL  # noqa: E402
from lf2s_protocol.wire import Codec, decode_datagram  # noqa: E402

from service.adapter import Adapter  # noqa: E402
from service.app import create_app  # noqa: E402
from service.controller import FIELDS, controller_snapshot  # noqa: E402

CASES = json.loads((ROOT / "tests/fixtures/fc-estimator-fault-arm.json").read_text("utf-8"))["cases"]


class Journal:
    def __init__(self):
        self.history = deque(maxlen=240)
        self.recorder = None
        self.dropped = 0

    def add(self, kind, text, **details):
        self.history.append({"kind": kind, "text": text, **details})


def ingest_case(adapter, case, clock):
    adapter.connected = True
    for row in case["timeline"]:
        clock[0] = 1000 + row["now_ms"]
        if row.get("request_hex"):
            raw = bytes.fromhex(row["request_hex"])
            request = decode_datagram(raw)
            name = (CONTROL if request.get_msgId() == 60000 else ACTION)[request.op]
            if name == "HELLO":
                adapter.client.hello(clock[0], nonce=request.client_nonce)
            operation = adapter.begin_operation(name)
            operation.update(request_id=request.request_id, request_msg=request.get_msgId())
            adapter.known_requests[request.request_id] = raw, operation["operation_id"]
            if name == "QUERY_PROFILE":
                adapter.query_request = request.request_id
        for raw in row["frames_hex"]:
            before = adapter.rejected
            adapter.receive(bytes.fromhex(raw), adapter.sim.esp_address)
            assert adapter.rejected == before


@pytest.fixture(params=CASES, ids=lambda case: case["name"])
def current_fault(request):
    adapter = Adapter(ROOT, Journal())
    clock = [1000]
    adapter.now = lambda: clock[0]
    adapter.sim.clock = adapter.now
    adapter.sim.pc_address, adapter.sim.esp_address, adapter.sim.fc_address = ("127.0.0.1", 1), ("127.0.0.1", 2), ("127.0.0.1", 3)
    ingest_case(adapter, request.param, clock)
    yield adapter, clock
    adapter.sim.close()


def assert_unavailable(snapshot):
    assert not snapshot["available"] and snapshot["source"] == "unavailable"
    assert all(snapshot[name] is None for name in FIELDS)


def test_saved_C_timer_fault_has_current_report_without_permission(current_fault):
    adapter, _ = current_fault
    view = adapter.snapshot()
    report = view["controller"]
    assert report["available"] and report["source"] == "mavlink_control_state"
    assert report["mode_code"] == 7 and report["mode_name"] == "FAULT"
    assert report["failsafe_reason_code"] == 12 and report["failsafe_reason_name"] == "SENSOR"
    assert report["preflight"] is False and report["lease_id"] == report["active_task"] == report["owner_system"] == report["owner_component"] == 0
    assert view["hardware_control_allowed"] is view["qualification"]["control_allowed"] is False
    assert all(view["sensors"][kind]["value"] is None for kind in ("attitude", "position", "height"))
    assert adapter.sent == 0


def test_expired_fault_cannot_look_current(current_fault):
    adapter, clock = current_fault
    clock[0] += 1000
    view = adapter.snapshot()
    assert view["link"]["phase"] == "stale"
    assert_unavailable(view["controller"])
    assert "过期" in view["controller"]["reason"]


def test_disconnect_and_delayed_state_cannot_restore_diagnostics(current_fault):
    adapter, _ = current_fault
    old = adapter.last_state
    encoded = Codec(1, 1).encode("LF2S_CONTROL_STATE", **{name: getattr(old, name) for name in old.fieldnames})
    adapter.disconnect()
    adapter.receive(encoded, adapter.sim.esp_address)
    assert adapter.snapshot()["link"]["phase"] == "disconnected"
    assert_unavailable(adapter.snapshot()["controller"])


def test_new_boot_then_noise_hides_old_fault(current_fault):
    adapter, _ = current_fault
    old = adapter.last_state
    fields = {name: getattr(old, name) for name in old.fieldnames}
    fields["boot_id"] += 1
    adapter.receive(Codec(1, 1).encode("LF2S_CONTROL_STATE", **fields), adapter.sim.esp_address)
    adapter.receive(b"bad", adapter.sim.esp_address)
    view = adapter.snapshot()
    assert view["link"]["phase"] == "blocked" and view["link"]["reconnect_required"]
    assert_unavailable(view["controller"])
    assert "重启" in view["controller"]["reason"]


def test_replay_live_channel_has_no_current_controller_state(current_fault):
    adapter, _ = current_fault
    adapter.replaying = True
    assert_unavailable(adapter.snapshot()["controller"])


def test_zero_codes_and_unknown_enum_are_preserved_as_reports(current_fault):
    adapter, _ = current_fault
    fields = {name: getattr(adapter.last_state, name) for name in adapter.last_state.fieldnames}
    for mode, reason in ((0, 0), (255, 254)):
        fields.update(mode=mode, failsafe_reason=reason)
        wire = decode_datagram(Codec(1, 1).encode("LF2S_CONTROL_STATE", **fields))
        report = controller_snapshot(wire, available=True, age_ms=0)
        assert report["available"] and report["mode_code"] == mode and report["failsafe_reason_code"] == reason
        assert report["mode_name"] == ("IDLE" if mode == 0 else None)
        assert report["failsafe_reason_name"] == ("OK" if reason == 0 else None)
        assert report["preflight"] is False


@pytest.mark.parametrize("bench_name", ("bench", "usb_fc"))
def test_hardware_bench_masks_any_old_simulated_controller_state(tmp_path, bench_name):
    app = create_app(ROOT, "test-token", 41234, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:41234", client=("127.0.0.1", 12345),
                    headers={"Authorization": "Bearer test-token"}) as client:
        assert client.post("/api/v1/connect", json={}).status_code == 200
        assert app.state.adapter.snapshot()["controller"]["available"]
        bench = getattr(app.state, bench_name)
        bench.active = True  # only a mode flag; no physical connection or reader
        try:
            view = client.get("/api/v1/state").json()
            assert not view["simulation"] and view["read_only"] and not view["hardware_control_allowed"]
            assert_unavailable(view["controller"])
            assert "未建立" in view["controller"]["reason"]
        finally:
            bench.active = False
