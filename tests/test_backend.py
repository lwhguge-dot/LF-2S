"""Real local HTTP + UDP + owned process checks; no hardware or archived test discovery."""

import concurrent.futures
import io
import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]
from lf2s_protocol.wire import Codec, decode_datagram  # noqa: E402

from service.serialization import clean, message_json  # noqa: E402


class Service:
    def __init__(self, directory):
        self.token = secrets.token_hex(32)
        self.directory = directory
        executable = os.environ.get("LF2S_TEST_BACKEND")
        self.command = (
            [executable]
            if executable
            else [sys.executable, "-B", str(ROOT / "service/entry.py")]
        )
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        self.process.stdin.write(
            json.dumps({"token": self.token, "user_data": str(directory)}) + "\n"
        )
        self.process.stdin.flush()
        answers = queue.Queue()
        threading.Thread(
            target=lambda: answers.put(self.process.stdout.readline()), daemon=True
        ).start()
        line = answers.get(timeout=15)
        assert line, self.process.stderr.read()
        self.ready = json.loads(line)
        self.base = f"http://127.0.0.1:{self.ready['port']}"
        self.http = httpx.Client(
            base_url=self.base,
            headers={"Authorization": "Bearer " + self.token},
            timeout=5,
            trust_env=False,
        )

    def get(self, route):
        response = self.http.get("/api/v1/" + route)
        response.raise_for_status()
        return response.json()

    def post(self, route, **kwargs):
        response = self.http.post("/api/v1/" + route, **kwargs)
        response.raise_for_status()
        return response.json()

    def until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state = self.get("state")
            if predicate(state):
                return state
            time.sleep(0.04)
        raise AssertionError(state)

    def connect(self):
        result = self.post("connect")
        assert result["status"] == "accepted", result
        return self.until(
            lambda s: (
                s["link"]["phase"] == "online"
                and all(x["valid"] for x in s["sensors"].values())
            )
        )

    def scenario(self, name):
        return self.post("simulation/scenario", json={"name": name})

    def close(self):
        self.http.close()
        if self.process.poll() is None:
            self.process.stdin.close()  # EOF, independent of HTTP or an explicit shutdown command
            self.process.wait(timeout=8)
        errors = self.process.stderr.read()
        assert self.process.returncode == 0, errors
        assert "Traceback" not in errors, errors


@pytest.fixture
def service(tmp_path):
    instance = Service(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def test_local_credentials_origin_host_and_no_action_entry(service):
    assert (
        service.ready["simulation"]
        and service.ready["hardware_control_allowed"] is False
    )
    assert (
        httpx.get(service.base + "/api/v1/health", trust_env=False).status_code == 401
    )
    assert (
        service.http.get(
            "/api/v1/health", headers={"Origin": "https://example.com"}
        ).status_code
        == 403
    )
    assert (
        service.http.get("/api/v1/health", headers={"Host": "localhost"}).status_code
        == 403
    )
    assert (
        service.http.post("/api/v1/action", json={"op": "TAKEOFF"}).status_code == 404
    )
    assert (
        service.http.post(
            "/api/v1/connect", json={"address": "192.168.1.1"}
        ).status_code
        == 422
    )
    assert service.get("state")["link"]["transport"].startswith("127.0.0.1")


def test_query_u64_configuration_and_correlated_udp_journal(service):
    state = service.connect()
    identity = state["identity"]
    assert (
        isinstance(identity["session_id"], str) and int(identity["session_id"]) > 2**53
    )
    assert (
        isinstance(identity["client_nonce"], str)
        and int(identity["client_nonce"]) > 2**53
    )
    result = service.post("profile")
    assert result["status"] == "accepted" and result["protocol_phase"] == "ACCEPTED"
    state = service.get("state")
    assert (
        state["profile"]["fields"]["config_valid"]
        == state["profile"]["fields"]["validated_mask"]
        == 0
    )
    assert state["profile"]["fields"]["boot_id"] == identity["boot_id"]
    assert state["qualification"]["control_allowed"] is False
    events = service.get("events")["events"]
    assert any(
        e.get("operation_id") == result["operation_id"] and e["kind"] == "tx"
        for e in events
    )
    assert any(
        e.get("operation_id") == result["operation_id"] and e["kind"] == "rx"
        for e in events
    )
    tx = [
        decode_datagram(bytes.fromhex(e["raw_hex"]))
        for e in events
        if e["kind"] == "tx"
    ]
    assert all(m.get_msgId() == 60000 and m.op in (1, 8) for m in tx)
    assert len(set(state["link"]["ports"])) == 3


def test_sensor_freeze_expires_while_link_keeps_receiving(service):
    before = service.connect()
    service.scenario("sensor_freeze")
    state = service.until(lambda s: not s["sensors"]["height"]["valid"])
    assert state["link"]["phase"] == "online"
    assert state["counters"]["received"] > before["counters"]["received"]
    assert state["sensors"]["height"]["value"] is None
    assert "过期" in state["sensors"]["height"]["reason"]


def test_unknown_measurements_are_null_even_with_old_cache(service):
    service.connect()
    service.scenario("unknown")
    state = service.until(lambda s: all(not x["valid"] for x in s["sensors"].values()))
    assert state["link"]["phase"] == "online"
    assert all(x["value"] is None for x in state["sensors"].values())


def test_link_pause_expires_and_does_not_replay_requests(service):
    service.connect()
    sent = service.get("state")["counters"]["sent"]
    service.scenario("link_pause")
    state = service.until(lambda s: s["link"]["phase"] == "stale")
    assert state["link"]["state_age_ms"] >= 1000
    assert all(x["value"] is None for x in state["sensors"].values())
    assert service.http.post("/api/v1/profile").status_code == 409
    assert service.http.get("/api/v1/image").status_code == 409
    service.scenario("healthy")
    state = service.until(lambda s: s["link"]["phase"] == "online")
    assert state["counters"]["sent"] == sent


@pytest.mark.parametrize(
    "scenario,reason", [("bad_fingerprint", "指纹"), ("wrong_source", "来源")]
)
def test_wrong_identity_never_refreshes_validity(service, scenario, reason):
    service.connect()
    service.scenario(scenario)
    state = service.until(lambda s: s["link"]["phase"] == "blocked")
    assert reason in state["link"]["reason"]
    assert all(x["value"] is None for x in state["sensors"].values())
    service.scenario("healthy")
    service.until(lambda s: s["link"]["phase"] == "online")


def test_reboot_requires_explicit_new_session(service):
    old = service.connect()
    service.post("profile")
    service.scenario("reboot")
    state = service.until(lambda s: s["link"]["phase"] == "blocked")
    assert "重启" in state["link"]["reason"] and state["profile"] is None
    sent = state["counters"]["sent"]
    time.sleep(0.4)
    assert service.get("state")["counters"]["sent"] == sent
    service.post("disconnect")
    new = service.connect()
    assert new["identity"]["session_id"] != old["identity"]["session_id"]
    assert new["identity"]["client_nonce"] != old["identity"]["client_nonce"]


def test_retry_preserves_exact_identity_and_is_bounded(service):
    service.scenario("link_pause")
    result = service.post("connect")
    assert result["status"] == "unknown"
    events = service.get("events")["events"]
    tx = [e for e in events if e["kind"] == "tx"]
    assert len(tx) == 3 and len({e["raw_hex"] for e in tx}) == 1
    time.sleep(0.5)
    assert service.get("state")["counters"]["sent"] == 3


def test_jpeg_decode_and_queries_can_run_together(service):
    service.connect()

    def images():
        ids = []
        for _ in range(5):
            response = service.http.get("/api/v1/image")
            response.raise_for_status()
            assert response.headers["x-lf2s-synthetic"] == "1"
            assert response.headers["x-lf2s-hardware-qualified"] == "0"
            with Image.open(io.BytesIO(response.content)) as jpeg:
                jpeg.load()
                assert jpeg.size == (960, 540) and jpeg.format == "JPEG"
            ids.append(int(response.headers["x-lf2s-frame-id"]))
        assert len(set(ids)) == len(ids)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(images)
        for _ in range(5):
            assert service.post("profile")["status"] == "accepted"
        future.result(timeout=10)


def test_backend_profile_lock_prevents_second_owner(service):
    duplicate = subprocess.Popen(
        service.command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    out, err = duplicate.communicate(
        json.dumps({"token": service.token, "user_data": str(service.directory)})
        + "\n",
        timeout=10,
    )
    assert duplicate.returncode != 0 and not out and "已有本地服务" in err
    assert service.get("health")["ready"]


def test_stdin_eof_releases_udp_http_and_rotating_journal(tmp_path):
    service = Service(tmp_path)
    state = service.connect()
    ports = [service.ready["port"], *state["link"]["ports"]]
    service.close()
    for index, port in enumerate(ports):
        with socket.socket(
            socket.AF_INET, socket.SOCK_STREAM if index == 0 else socket.SOCK_DGRAM
        ) as channel:
            channel.bind(("127.0.0.1", port))
    text = (tmp_path / "logs/events.jsonl").read_text("utf-8")
    assert "HELLO" in text and service.token not in text


def test_json_nonfinite_and_full_u64_ranges():
    assert clean({"unknown": [float("nan"), float("inf"), -float("inf"), 0]}) == {
        "unknown": [None, None, None, 0]
    }
    raw = Codec(1, 1).encode(
        "LF2S_RESULT",
        boot_id=(1 << 64) - 1,
        session_id=(1 << 64) - 2,
        client_nonce=(1 << 64) - 3,
    )
    record = message_json(decode_datagram(raw))
    assert record["fields"]["boot_id"] == "18446744073709551615"
    assert record["fields"]["session_id"] == "18446744073709551614"
    json.dumps(record, allow_nan=False)


def task_ready(service):
    service.scenario("task_bench")
    service.connect()
    service.post("profile")
    return service.get("state")


def start_task(service):
    service.post("tasks/start", json={"confirmed": True})
    return service.until(lambda s: s["task"]["current"]["stage"] == "TAKEOFF")


def test_simulated_task_needs_explicit_bench_context_and_confirmation(service):
    service.connect()
    service.post("profile")
    assert (
        service.http.post("/api/v1/tasks/start", json={"confirmed": True}).status_code
        == 409
    )
    task_ready(service)
    assert (
        service.http.post("/api/v1/tasks/start", json={"confirmed": False}).status_code
        == 409
    )
    assert (
        service.http.post("/api/v1/tasks/start", json={"confirmed": "true"}).status_code
        == 422
    )
    assert (
        service.http.post(
            "/api/v1/tasks/start", json={"confirmed": True, "z_mm": 900}
        ).status_code
        == 422
    )


def test_task_full_udp_loop_completes_from_samples_and_distinct_results(service):
    initial = task_ready(service)
    state = start_task(service)
    assert state["task"]["current"]["status"] == "in_progress"
    assert state["qualification"]["control_allowed"] is False
    assert initial["profile"]["fields"]["config_valid"] == 1 and initial["task_fixture"]
    completed = service.until(
        lambda s: s["task"]["current"]["status"] == "completed", timeout=16
    )
    assert completed["sensors"]["height"]["value"] == 0
    assert (
        completed["qualification"]["lease_id"]
        == completed["qualification"]["task_id"]
        == 0
    )
    steps = {o["name"]: o for o in completed["operations"]}
    assert all(
        steps[n]["protocol_phase"] == "COMPLETED"
        for n in ("BEGIN", "TAKEOFF", "HOLD", "LAND", "END")
    )
    assert (
        steps["PERMIT"]["protocol_phase"]
        == steps["ACQUIRE"]["protocol_phase"]
        == "ACCEPTED"
    )
    assert "RENEW" in steps
    events = service.get("events")["events"]
    ordered = [
        e["wire"]["fields"]["op"]
        for e in events
        if e["kind"] == "tx" and e["wire"]["message_id"] == 60001
    ]
    assert ordered == [1, 2, 5, 6, 8]
    assert any(
        e["kind"] == "rx" and e["wire"]["fields"].get("phase") == 2 for e in events
    )
    assert all(
        "session_id" in e and "received_at_ms" in e and e["source"] == "synthetic"
        for e in events
    )


@pytest.mark.parametrize(
    "scenario",
    ["sensor_freeze", "link_pause", "position_reset", "manual_takeover", "reboot"],
)
def test_task_faults_stop_sequence_and_never_resume(service, scenario):
    task_ready(service)
    start_task(service)
    service.scenario(scenario)
    stopped = service.until(lambda s: not s["task"]["running"], timeout=5)
    assert stopped["task"]["current"]["status"] != "completed"
    if scenario == "link_pause":
        assert stopped["task"]["current"]["status"] == "unknown"
        assert (
            stopped["task"]["unknown_request_id"]
            == stopped["task"]["current"]["request_id"]
        )
    service.scenario("healthy")
    time.sleep(0.6)
    state = service.get("state")
    assert not state["task"]["running"]
    assert not any(o["name"] == "LAND" for o in state["operations"])
    if scenario == "manual_takeover":
        assert state["qualification"]["manual_latched"] is True
        service.post("disconnect")
        hello = service.post("connect")
        if hello["status"] == "accepted":
            reconnected = service.until(lambda s: s["link"]["phase"] == "online")
            assert reconnected["qualification"]["manual_latched"] is True
            service.post("profile")
        else:
            assert hello["status"] == "rejected" and hello["reason"] == "BUSY"
        assert (
            service.http.post(
                "/api/v1/tasks/start", json={"confirmed": True}
            ).status_code
            == 409
        )


def test_missing_action_result_retries_same_id_then_only_queries_original(service):
    task_ready(service)
    service.scenario("result_loss")
    service.post("tasks/start", json={"confirmed": True})
    stopped = service.until(
        lambda s: (
            s["task"]["current"]["status"] == "unknown" and not s["task"]["running"]
        )
    )
    original_id = stopped["task"]["unknown_request_id"]
    tx = [
        e
        for e in stopped["events"]
        if e["kind"] == "tx" and e["request_id"] == original_id
    ]
    assert 2 <= len(tx) <= 3 and len({e["raw_hex"] for e in tx}) == 1
    assert (
        service.http.post("/api/v1/tasks/start", json={"confirmed": True}).status_code
        == 409
    )
    service.scenario("healthy")
    service.post("tasks/query")
    final = service.get("state")
    assert (
        not final["task"]["running"]
        and final["task"]["current"]["status"] != "completed"
    )
    queries = [
        e
        for e in final["events"]
        if e["kind"] == "tx"
        and e["wire"]["fields"]["op"] == 6
        and e["wire"]["message_id"] == 60000
    ]
    assert queries[-1]["wire"]["fields"]["query_id"] == original_id
    assert len([o for o in final["operations"] if o["name"] == "TAKEOFF"]) == 1


def test_cancel_and_replay_disconnect_live_mode_and_never_transmit(service):
    task_ready(service)
    start_task(service)
    cancelled = service.post("tasks/cancel")
    assert cancelled["current"]["status"] == "cancelled"
    state = service.until(lambda s: s["recording"]["frames"] >= 2)
    recording_id = state["recording"]["id"]
    sent = state["counters"]["sent"]
    frame = service.post("replay/open", json={"recording_id": recording_id, "index": 1})
    assert frame["historical"] and frame["read_only"]
    assert service.get("state")["link"]["phase"] == "disconnected"
    for route in ("connect", "profile", "tasks/cancel", "tasks/query"):
        assert service.http.post("/api/v1/" + route).status_code == 409
    assert (
        service.http.post("/api/v1/tasks/start", json={"confirmed": True}).status_code
        == 409
    )
    assert (
        service.http.post(
            "/api/v1/simulation/scenario", json={"name": "healthy"}
        ).status_code
        == 409
    )
    assert service.http.get("/api/v1/image").status_code == 409
    service.post("replay/seek", json={"recording_id": recording_id, "index": 0})
    assert service.get("state")["counters"]["sent"] == sent
    assert (
        service.http.post(
            "/api/v1/replay/open", json={"recording_id": "../events", "index": 0}
        ).status_code
        == 409
    )
    service.post("replay/close")
    assert service.get("state")["link"]["phase"] == "disconnected"


def test_recording_caps_retention_and_crash_partial_frame(tmp_path):
    from service.recordings import Recordings

    for _ in range(4):
        recorder = Recordings(tmp_path, max_bytes=4000, max_files=2)
        for i in range(40):
            recorder.add(
                "snapshot",
                {"service_time_ms": i * 500, "simulation": True, "padding": "x" * 100},
            )
        recorder.close()
        assert recorder.stats()["truncated"] and recorder.stats()["dropped"] > 0
        assert recorder.bytes <= 4000 and recorder.queue.maxsize == 256
    assert (
        len(list(tmp_path.glob("*.jsonl")))
        == len(list(tmp_path.glob("*.summary.json")))
        == 2
    )
    assert recorder.frame(recorder.id, 0)["state"]["service_time_ms"] == 0


def test_terminal_action_result_survives_late_ack_and_wrong_task(tmp_path):
    import asyncio

    from service.adapter import Adapter
    from service.journal import Journal

    async def run():
        journal = Journal(tmp_path / "logs")
        a = Adapter(ROOT, journal)
        await a.start()
        a.set_scenario("task_bench")
        await a.connect()
        await asyncio.sleep(0.15)
        await a.query_profile()
        raw = a.client.action(
            "BEGIN", a.now(), task_id=7, duration_ms=1000, valid_ms=500
        )
        op = a.begin_operation("BEGIN")
        a.known_requests[a.client.request_id] = (raw, op["operation_id"])
        op.update(request_id=a.client.request_id, request_msg=60001)
        result = dict(
            boot_id=a.client.boot,
            session_id=a.client.session,
            request_id=a.client.request_id,
            request_msg=60001,
            task_id=7,
            lease_id=0,
            echo_op=1,
            target_system=255,
            target_component=190,
        )
        a.receive(
            Codec(1, 1).encode("LF2S_RESULT", **result, phase=3, event_id=9),
            a.sim.esp_address,
        )
        assert op["status"] == "completed"
        a.receive(
            Codec(1, 1).encode("LF2S_RESULT", **result, phase=1), a.sim.esp_address
        )
        assert op["status"] == "completed" and op["event_id"] == 9
        bad = {**result, "task_id": 8}
        before = a.rejected
        a.receive(
            Codec(1, 1).encode("LF2S_RESULT", **bad, phase=3, event_id=9),
            a.sim.esp_address,
        )
        assert a.rejected == before + 1 and op["status"] == "completed"
        await a.close()
        journal.close()

    asyncio.run(run())


def test_explicit_simulator_reset_clears_old_task_binding_and_needs_new_permit(service):
    initial = task_ready(service)
    start_task(service)
    assert service.post("tasks/cancel")["current"]["status"] == "cancelled"
    fresh = task_ready(service)
    assert fresh["identity"]["boot_id"] != initial["identity"]["boot_id"]
    assert fresh["task"]["current"] is None and not fresh["task"]["running"]
    assert not fresh["qualification"]["manual_latched"]
    started = start_task(service)
    assert started["task"]["current"]["task_id"] == 2
    assert len([o for o in started["operations"] if o["name"] == "PERMIT"]) == 2
    service.post("tasks/cancel")


def test_recording_survives_service_restart_and_stays_readonly(tmp_path):
    first = Service(tmp_path)
    try:
        state = task_ready(first)
        session = state["identity"]["session_id"]
        state = first.until(lambda s: s["recording"]["frames"] >= 3)
        recording_id = state["recording"]["id"]
    finally:
        first.close()
    second = Service(tmp_path)
    try:
        prior = next(
            r for r in second.get("recordings")["recordings"] if r["id"] == recording_id
        )
        assert prior["complete"] and prior["frames"] >= 3
        frame = second.post(
            "replay/open", json={"recording_id": recording_id, "index": 2}
        )
        assert frame["state"]["identity"]["session_id"] == session
        assert isinstance(frame["state"]["identity"]["session_id"], str)
        assert frame["historical"] and second.get("state")["counters"]["sent"] == 0
        assert not second.get("state")["task"]["running"]
    finally:
        second.close()
