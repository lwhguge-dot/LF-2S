"""Reboot cannot be unlatched by traffic; actual saved compiled ARM frames."""

import json
import sys
from collections import deque
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]
from lf2s_protocol.wire import ProtocolError, decode_datagram  # noqa: E402

from service.adapter import Adapter  # noqa: E402

VECTORS = json.loads((ROOT / "tests/fixtures/fc-reboot-arm.json").read_text("utf-8"))["vectors"]


class Journal:
    def __init__(self):
        self.history = deque(maxlen=240)
        self.dropped = 0
        self.recorder = None

    def add(self, kind, text, **details):
        self.history.append({"kind": kind, "text": text, **details})


def raw(name):
    return bytes.fromhex(VECTORS[name][0])


def hello(adapter, name):
    adapter.connected = True
    message = decode_datagram(raw(name))
    request = adapter.client.hello(0, nonce=message.client_nonce)
    op = adapter.begin_operation("HELLO")
    op.update(request_id=1, request_msg=60000)
    adapter.known_requests[1] = (request, op["operation_id"])
    adapter.receive(raw(name), adapter.sim.esp_address)
    assert op["status"] == "accepted"


@pytest.fixture
def adapter():
    app = Adapter(ROOT, Journal())
    app.now = lambda: 1000
    app.sim.pc_address, app.sim.esp_address, app.sim.fc_address = (
        ("127.0.0.1", 1), ("127.0.0.1", 2), ("127.0.0.1", 3)
    )
    hello(app, "first_boot_hello")
    app.receive(raw("first_boot_state"), app.sim.esp_address)
    assert app.snapshot()["link"]["phase"] == "online"
    yield app
    app.sim.close()


@pytest.mark.parametrize("noise", ("malformed", "wrong_source", "old_result", "new_state"))
def test_reboot_latch_survives_noise_then_delayed_old_state(adapter, noise):
    adapter.receive(raw("second_boot_state"), adapter.sim.esp_address)
    assert adapter.snapshot()["link"]["phase"] == "blocked"
    received = adapter.received
    if noise == "malformed":
        adapter.receive(b"bad", adapter.sim.esp_address)
    elif noise == "wrong_source":
        adapter.receive(raw("first_boot_state"), ("127.0.0.1", 99))
    else:
        adapter.receive(raw("first_boot_hello" if noise == "old_result" else "second_boot_state"), adapter.sim.esp_address)
    adapter.receive(raw("delayed_old_boot_state"), adapter.sim.esp_address)
    state = adapter.snapshot()
    assert state["link"]["phase"] == "blocked" and state["link"]["reconnect_required"]
    assert "重启" in state["link"]["reason"] and adapter.received == received
    assert not adapter.client.pending and adapter.profile is None and adapter.last_state is None
    assert all(not row["valid"] and row["value"] is None for row in state["sensors"].values())
    assert state["read_only"] and not state["hardware_control_allowed"]


def test_only_explicit_disconnect_and_new_hello_recover(adapter):
    adapter.receive(raw("second_boot_state"), adapter.sim.esp_address)
    adapter.disconnect()
    state = adapter.snapshot()
    assert state["link"]["phase"] == "disconnected" and not state["link"]["reconnect_required"]
    assert state["identity"]["boot_id"] is None
    adapter.receive(raw("delayed_old_boot_state"), adapter.sim.esp_address)
    assert adapter.snapshot()["link"]["phase"] == "disconnected"
    hello(adapter, "second_boot_hello")
    adapter.receive(raw("second_boot_state"), adapter.sim.esp_address)
    state = adapter.snapshot()
    assert state["identity"]["boot_id"] == "101" and state["link"]["phase"] == "online"
    assert not state["link"]["reconnect_required"] and not state["hardware_control_allowed"]


def test_transient_bad_packet_without_reboot_can_recover(adapter):
    adapter.receive(b"bad", adapter.sim.esp_address)
    assert adapter.snapshot()["link"]["phase"] == "blocked"
    adapter.receive(raw("delayed_old_boot_state"), adapter.sim.esp_address)
    state = adapter.snapshot()
    assert state["link"]["phase"] == "online" and not state["link"]["reconnect_required"]


def test_reboot_latch_blocks_outgoing_request_before_transport(adapter):
    adapter.receive(raw("second_boot_state"), adapter.sim.esp_address)
    operation = adapter.begin_operation("QUERY_PROFILE")
    request = adapter.client.control("QUERY_PROFILE", 1000)
    sent = adapter.sent
    with pytest.raises(ProtocolError, match="重启"):
        adapter.transmit(request, operation)
    assert adapter.sent == sent and adapter.snapshot()["link"]["reconnect_required"]
