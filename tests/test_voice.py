"""Audio fault injection and review isolation; no microphone or remote service."""

import asyncio
import hashlib
import io
import json
import sys
import time
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]
from lf2s_protocol.wire import ProtocolError  # noqa: E402
from service.app import create_app  # noqa: E402
from service.voice import MODEL_ID, Voice, validate_wav  # noqa: E402

TOKEN = "isolated-asr-test-token-with-32-characters"
ENV = {"LF2S_ASR_BASE_URL": "http://127.0.0.1:8124", "LF2S_ASR_TOKEN": TOKEN}
TEXT = "不要起飞，我只是举个例子：起飞到五十厘米，保持三秒后降落。"


def wav(*, rate=16000, channels=1, width=2, seconds=1, frames=None):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(bytes((frames or int(rate * seconds)) * channels * width))
    return output.getvalue()


def response(raw, **change):
    return {
        "request_id": "a" * 24,
        "model": MODEL_ID,
        "provider": "cpu",
        "language": "zh",
        "audio_sha256": hashlib.sha256(raw).hexdigest(),
        "duration_ms": validate_wav(raw),
        "processing_ms": 75.5,
        "transcript": TEXT,
        "transcript_approved": False,
        "permission_granted": False,
        "hardware_control_allowed": False,
        **change,
    }


class Journal:
    def __init__(self):
        self.events = []

    def add(self, *args, **kwargs):
        self.events.append((args, kwargs))


@pytest.mark.parametrize(
    "raw",
    [
        b"bad",
        bytes(500001),
        wav(rate=8000),
        wav(channels=2),
        wav(width=1),
        wav(seconds=0.1),
        wav(seconds=15.1),
        wav()[:-2],
    ],
    ids=[
        "bad",
        "oversized",
        "wrong_rate",
        "stereo",
        "wrong_width",
        "short",
        "long",
        "truncated",
    ],
)
def test_invalid_audio_is_rejected_before_any_request(raw):
    with pytest.raises(ProtocolError):
        validate_wav(raw)


def test_raw_audio_is_explicit_bound_to_response_and_never_becomes_a_task():
    calls = []
    raw = wav(frames=8008)

    async def handler(request):
        calls.append(request)
        assert request.url.path == "/v1/transcriptions"
        assert request.headers["Content-Type"] == "audio/wav"
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        assert request.content == raw
        return httpx.Response(200, json=response(raw))

    async def scenario():
        journal = Journal()
        provider = Voice(journal, env=ENV, transport=httpx.MockTransport(handler))
        try:
            assert not calls
            state = provider.start(raw)
            assert state["current"]["phase"] == "pending"
            await provider.runner
            state = provider.snapshot()
            assert state["current"]["phase"] == "review"
            assert state["current"]["transcript"] == TEXT
            assert state["language"] == "zh"
            assert (
                state["permission_granted"]
                is state["hardware_control_allowed"]
                is False
            )
            assert len(calls) == 1
            assert TOKEN not in json.dumps(state)
            assert TEXT not in json.dumps(journal.events, ensure_ascii=False)
            assert "RIFF" not in json.dumps(journal.events)
            assert not state["transcript_approved"]
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"audio_sha256": "0" * 64},
        {"language": "en"},
        {"model": "wrong-model"},
        {"provider": "cuda"},
        {"duration_ms": 999},
        {"duration_ms": True},
        {"transcript_approved": True},
        {"permission_granted": True},
        {"hardware_control_allowed": True},
        {"transcript": None},
        {"transcript": "字" * 513},
        {"processing_ms": True},
        {"processing_ms": float("inf")},
        {"processing_ms": -1},
    ],
)
def test_mismatched_identity_audio_permission_or_malformed_result_is_rejected(change):
    raw = wav()

    async def scenario():
        payload = json.dumps(response(raw, **change))
        provider = Voice(
            Journal(),
            env=ENV,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=payload)
            ),
        )
        try:
            provider.start(raw)
            await provider.runner
            current = provider.snapshot()["current"]
            assert current["phase"] == "error" and current["transcript"] is None
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_silence_and_blank_text_are_not_reviewable():
    raw = wav()

    async def scenario():
        provider = Voice(
            Journal(),
            env=ENV,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=response(raw, transcript="  "))
            ),
        )
        try:
            provider.start(raw)
            await provider.runner
            assert provider.current["phase"] == "empty"
            assert provider.current["transcript"] == ""
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_http_errors_do_not_redirect_retry_or_retain_text(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"Location": "http://127.0.0.1:9999/elsewhere"}
        )

    async def scenario():
        provider = Voice(Journal(), env=ENV, transport=httpx.MockTransport(handler))
        try:
            provider.start(wav())
            await provider.runner
            assert provider.current["phase"] == "error"
            assert provider.current["transcript"] is None
            assert len(calls) == 1
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_cancel_timeout_and_single_inflight_drop_old_audio_result():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=response(request.content))

        provider = Voice(
            Journal(), env=ENV, transport=httpx.MockTransport(handler), timeout=0.04
        )
        try:
            provider.start(wav())
            await entered.wait()
            with pytest.raises(ProtocolError):
                provider.start(wav())
            await provider.clear()
            release.set()
            await asyncio.sleep(0)
            assert provider.current is None and not provider.running
            entered.clear()
            release.clear()
            provider.start(wav())
            await provider.runner
            assert provider.current["phase"] == "error"
            assert provider.current["transcript"] is None
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "content", [b"x" * 16385, b'{"transcript":1,"transcript":2}', b"[]"]
)
def test_response_size_duplicate_json_and_wrong_root_are_rejected(content):
    async def scenario():
        provider = Voice(
            Journal(),
            env=ENV,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=content)
            ),
        )
        try:
            provider.start(wav())
            await provider.runner
            assert provider.current["phase"] == "error"
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"LF2S_ASR_BASE_URL": "http://127.0.0.1:8124/v1"},
        {"LF2S_ASR_TOKEN": "short"},
        {"LF2S_ASR_BASE_URL": "http://user:pass@127.0.0.1:8124"},
    ],
)
def test_invalid_configuration_is_not_used(change):
    async def scenario():
        provider = Voice(Journal(), env={**ENV, **change})
        try:
            assert not provider.configured
            with pytest.raises(ProtocolError):
                provider.start(wav())
            assert provider.current is None
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_api_audio_does_not_parse_text_and_replay_or_shutdown_invalidates_it(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "service.app.Voice",
        lambda journal: Voice(
            journal,
            env=ENV,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=response(request.content))
            ),
        ),
    )
    app = create_app(ROOT, "voice-local-test", 41234, tmp_path)
    with TestClient(
        app,
        base_url="http://127.0.0.1:41234",
        client=("127.0.0.1", 12345),
        headers={"Authorization": "Bearer voice-local-test"},
    ) as client:
        assert (
            client.post("/api/v1/voice/transcribe", json={"text": "fake"}).status_code
            == 415
        )
        assert (
            client.post(
                "/api/v1/voice/transcribe",
                content=bytes(500001),
                headers={"Content-Type": "audio/wav"},
            ).status_code
            == 413
        )
        assert (
            client.post("/api/v1/text-tasks/parse", content=bytes(4097)).status_code
            == 413
        )
        assert (
            client.post(
                "/api/v1/voice/transcribe",
                content=wav(),
                headers={"Content-Type": "audio/wav"},
            ).status_code
            == 200
        )
        deadline = time.monotonic() + 2
        while True:
            state = client.get("/api/v1/state").json()
            if state["voice"]["current"]["phase"] != "pending":
                break
            assert time.monotonic() < deadline
        assert state["voice"]["current"]["transcript"] == TEXT
        assert state["text_tasks"]["current"] is None
        assert state["task"]["current"] is None and state["counters"]["sent"] == 0
        assert (
            client.post("/api/v1/voice/execute", json={"confirmed": True}).status_code
            == 404
        )
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
        assert app.state.voice.current is None
        assert (
            client.post(
                "/api/v1/voice/transcribe",
                content=wav(),
                headers={"Content-Type": "audio/wav"},
            ).status_code
            == 409
        )
    assert app.state.voice.closed and app.state.voice.current is None
    assert all(
        TEXT not in path.read_text("utf-8")
        for path in (tmp_path / "logs").glob("*.jsonl")
    )
