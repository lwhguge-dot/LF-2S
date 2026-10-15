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
    assert service.ready["simulation"] and service.ready["read_only"]
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
