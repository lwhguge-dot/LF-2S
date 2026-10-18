"""Read-only real-transport isolation and freshness; synthetic wire fixtures."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]

from lf2s_protocol.wire import Codec, ProtocolError, decode_datagram  # noqa: E402
from test_esp32 import station_health  # noqa: E402
from test_reboot_latch import Journal, hello, raw  # noqa: E402

from service.adapter import Adapter  # noqa: E402
from service.esp32 import health_data  # noqa: E402
from service.gateway import Gateway  # noqa: E402


class PairedGateway:
    peer = ("192.168.137.138", 12700)
    local = ("192.168.137.1", 12701)

    def __init__(self):
        self.closed = False
        self.sent = []

    def send(self, packet):
        self.sent.append(packet)

    def close(self):
        self.closed = True


@pytest.fixture
def adapter():
    adapter = Adapter(ROOT, Journal())
    adapter.now = lambda: 1000
    adapter.sim.pc_address, adapter.sim.esp_address, adapter.sim.fc_address = (
        ("127.0.0.1", 1), ("127.0.0.1", 2), ("127.0.0.1", 3)
    )
    hello(adapter, "first_boot_hello")
    adapter.gateway = PairedGateway()
    adapter.receive(raw("first_boot_state"), adapter.gateway.peer)
    yield adapter
    adapter.disconnect()
    adapter.sim.close()


def test_uart_firmware_health_remains_readonly():
    value = {**station_health(), "firmware": "0.1.7", "uart_ready": True}
    clean = health_data({"content-type": "application/json"}, json.dumps(value).encode())
    assert clean["uart_ready"] is True and clean["control_forwarding"] is False
    for changed in ({"firmware": "0.1.6"}, {"control_forwarding": True}, {"uart_ready": 1}):
        with pytest.raises(ProtocolError):
            health_data({"content-type": "application/json"}, json.dumps({**value, **changed}).encode())


def test_live_snapshot_has_no_synthetic_or_action_qualification(adapter):
    adapter.sim.task_bench = True
    state = adapter.snapshot()
    assert state["simulation"] is False and state["task_fixture"] is False
    assert state["read_only"] is True and state["hardware_control_allowed"] is False
    assert all(value["source"] == "uart1" for value in state["sensors"].values())
    assert state["link"]["ports"] == [12701, 12700]
    assert state["qualification"]["control_allowed"] is False


@pytest.mark.parametrize("operation", ("PERMIT", "ACQUIRE", "RENEW", "RELEASE", "CONFIRM_RESULT"))
def test_live_transport_rejects_control_even_if_simulator_was_prepared(adapter, operation):
    adapter.sim.task_bench = True
    message = adapter.client.control(operation, 1000)
    with pytest.raises(ProtocolError):
        adapter.transmit(message, adapter.begin_operation(operation))
    assert not adapter.gateway.sent


def test_wrong_udp_peer_cannot_refresh_session(adapter):
    before = adapter.received
    adapter.receive(raw("first_boot_state"), adapter.sim.esp_address)
    assert adapter.received == before
    assert adapter.snapshot()["link"]["phase"] == "blocked"


def test_queued_simulator_traffic_is_detached_from_real_receiver(adapter):
    before = (adapter.received, adapter.rejected)
    adapter.receive_simulation(raw("first_boot_state"), adapter.sim.esp_address)
    assert (adapter.received, adapter.rejected) == before
    assert adapter.snapshot()["link"]["phase"] == "online"


def attitude(timestamp):
    return Codec(1, 1).encode("ATTITUDE", time_boot_ms=timestamp, roll=0.2, pitch=0.1, yaw=-0.3,
                              rollspeed=0.0, pitchspeed=0.0, yawspeed=0.0)


def test_duplicate_backward_and_stale_standard_telemetry_never_refresh_age(adapter):
    adapter.receive(attitude(123), adapter.gateway.peer)
    assert adapter.telemetry[30][1] == 1000
    adapter.now = lambda: 1100
    adapter.receive(attitude(123), adapter.gateway.peer)
    adapter.receive(attitude(122), adapter.gateway.peer)
    assert adapter.telemetry[30][1] == 1000
    adapter.now = lambda: 2101
    adapter.receive(attitude(124), adapter.gateway.peer)
    assert adapter.telemetry[30][1] == 1000
    assert not adapter.snapshot()["sensors"]["attitude"]["valid"]


def test_fc_reboot_invalidates_current_real_telemetry_and_latches(adapter):
    adapter.receive(attitude(123), adapter.gateway.peer)
    adapter.receive(raw("second_boot_state"), adapter.gateway.peer)
    assert not adapter.telemetry
    assert adapter.reconnect_required
    adapter.receive(raw("delayed_old_boot_state"), adapter.gateway.peer)
    adapter.receive(attitude(124), adapter.gateway.peer)
    assert not adapter.telemetry and adapter.snapshot()["link"]["phase"] == "blocked"


@pytest.mark.parametrize("changed", ("session_id", "client_nonce"))
def test_same_boot_identity_change_latches_even_before_sample_order_check(adapter, changed):
    adapter.receive(attitude(123), adapter.gateway.peer)
    fields = decode_datagram(raw("first_boot_state")).to_dict()
    fields.pop("mavpackettype")
    fields[changed] += 1
    fields["sample_id"] = 0
    adapter.receive(Codec(1, 1).encode("LF2S_CONTROL_STATE", **fields), adapter.gateway.peer)
    assert adapter.reconnect_required and not adapter.telemetry
    assert adapter.last_state is None and adapter.profile is None and not adapter.client.pending
    adapter.receive(raw("delayed_old_boot_state"), adapter.gateway.peer)
    adapter.receive(attitude(124), adapter.gateway.peer)
    assert adapter.snapshot()["link"]["phase"] == "blocked" and not adapter.telemetry


def test_disconnect_closes_udp_and_clears_data(adapter):
    gateway = adapter.gateway
    adapter.receive(attitude(123), gateway.peer)
    adapter.disconnect()
    assert gateway.closed and adapter.gateway is None
    assert not adapter.telemetry and adapter.snapshot()["link"]["phase"] == "disconnected"


def test_transport_enforces_readonly_independently(adapter):
    gateway = Gateway("192.168.137.138", lambda *_: None)
    gateway.active, gateway.transport = True, object()
    with pytest.raises(ProtocolError):
        gateway.send(adapter.client.control("PERMIT", 1000))


def test_live_schedule_maintains_peer_without_faking_new_state(adapter):
    async def run():
        current = [1000]
        adapter.now = lambda: current[0]
        adapter.sim.tick = lambda: None
        task = asyncio.create_task(adapter.schedule())
        try:
            await asyncio.sleep(.02)
            current[0] = 1600
            await asyncio.sleep(.12)
            current[0] = 2200
            await asyncio.sleep(.12)
            sent = [decode_datagram(packet) for packet in adapter.gateway.sent]
            heartbeats = [msg for msg in sent if msg.get_msgId() == 0]
            assert len(heartbeats) >= 3 and all(msg.type == 6 for msg in heartbeats)
            assert all(msg.get_msgId() in (0, 60000) for msg in sent)
            assert adapter.snapshot()["link"]["phase"] == "stale"
            assert adapter.state_at == 1000
            assert not adapter.snapshot()["sensors"]["attitude"]["valid"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_recovery_reuses_original_hello_and_stops_after_three_attempts(adapter):
    async def run():
        original = adapter.known_requests[1][0]
        current = [1800]
        adapter.now = lambda: current[0]
        adapter.sim.tick = lambda: None
        task = asyncio.create_task(adapter.schedule())
        try:
            for _ in range(4):
                await asyncio.sleep(.12)
                current[0] += 600
            packets = [packet for packet in adapter.gateway.sent if decode_datagram(packet).get_msgId() == 60000]
            assert packets == [original] * 3
            assert adapter.gateway_recovery_tries == 3
            assert adapter.failure and adapter.snapshot()["link"]["phase"] == "blocked"
            assert adapter.client.boot and adapter.client.session
            adapter.receive(raw("delayed_old_boot_state"), adapter.gateway.peer)
            assert adapter.reconnect_required and adapter.snapshot()["link"]["phase"] == "blocked"
            assert "恢复失败" in adapter.failure and adapter.last_state is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_retry_hello_cannot_adopt_reboot_or_other_session(adapter):
    adapter.receive(raw("second_boot_hello"), adapter.gateway.peer)
    assert adapter.reconnect_required
    assert adapter.snapshot()["link"]["phase"] == "blocked"
    assert not adapter.telemetry


def test_live_journal_context_never_labels_physical_samples_as_synthetic(adapter):
    context = adapter.journal.context()
    assert context["simulation"] is False and context["source"] == "uart1"
