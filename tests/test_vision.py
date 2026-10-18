"""Explicit image-observation requests; no company API or physical camera."""

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]
from lf2s_protocol.wire import ProtocolError  # noqa: E402
from service.vision import VisionObservations  # noqa: E402


class Journal:
    def __init__(self):
        self.events = []

    def add(self, *args, **kwargs):
        self.events.append((args, kwargs))


def jpeg():
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), "white").save(stream, "JPEG")
    return stream.getvalue()


def response(*, model="qwen38", light="green"):
    observation = {
        "signals": [{"box": [100, 120, 200, 500], "light": light}],
        "zebra_visible": True,
        "checkout_visible": False,
        "currency": {"visible_notes_complete": None, "notes": []},
    }
    return json.dumps(
        {
            "model": model,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(observation),
                        "reasoning_content": None,
                    },
                }
            ],
        }
    )


class Model:
    def __init__(self, raw=None, *, blocked=False):
        self.raw = raw or response()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.frames = []

    async def request(self, frame, image_info):
        self.frames.append((frame, image_info))
        self.entered.set()
        await self.release.wait()
        return self.raw

    async def close(self):
        pass


def start(service, *, frame_id="1", image=None, source="synthetic"):
    return service.start(
        jpeg() if image is None else image,
        source=source,
        frame_id=frame_id,
        stream_id="123456789",
        epoch=0,
        received_ms=10000,
    )


def test_no_model_request_is_made_until_explicit_analysis_and_frame_is_bound():
    async def scenario():
        model = Model()
        service = VisionObservations(Journal(), model=model, clock=lambda: 10000)
        assert service.snapshot()["current"] is None
        assert model.frames == []

        pending = start(service)
        assert pending["running"]
        assert pending["current"]["phase"] == "pending"
        assert model.frames == []
        await model.entered.wait()
        for _ in range(20):
            if not service.running:
                break
            await asyncio.sleep(0)

        result = service.snapshot()["current"]
        assert result["phase"] == "ready"
        assert result["frame"]["frame_id"] == "1"
        assert result["frame"]["image_sha256"]
        assert result["observation"]["current_light"] == "green"
        assert result["observation"]["zebra_visible"] is True
        assert result["observation"]["crossing_allowed"] is False
        assert result["observation"]["currency"]["scene_total_fen"] is None
        assert model.frames[0][1] == {"width": 12, "height": 8, "mime": "image/jpeg"}
        await service.close()

    asyncio.run(scenario())


def test_missing_configuration_and_wrong_source_fail_before_model_request():
    async def scenario():
        model = Model()
        unconfigured = VisionObservations(Journal(), env={}, clock=lambda: 10000)
        assert not unconfigured.snapshot()["configured"]
        with pytest.raises(ProtocolError, match="配置"):
            start(unconfigured)

        service = VisionObservations(Journal(), model=model, clock=lambda: 10000)
        with pytest.raises(ProtocolError, match="来源"):
            start(service, source="offline_image")
        assert not service.snapshot()["running"]
        assert model.frames == []
        await service.close()
        await unconfigured.close()

    asyncio.run(scenario())


def test_pending_request_can_be_cancelled_and_stale_reply_cannot_restore_it():
    async def scenario():
        model = Model(blocked=True)
        service = VisionObservations(Journal(), model=model, clock=lambda: 10000)
        pending = start(service)
        request_id = pending["current"]["request_id"]
        await model.entered.wait()
        cleared = await service.cancel(request_id)
        model.release.set()
        await asyncio.sleep(0)

        assert cleared["current"] is None
        assert service.snapshot()["current"] is None
        assert not service.running
        await service.close()

    asyncio.run(scenario())


def test_model_error_is_displayed_as_unavailable_without_action_qualification():
    async def scenario():
        model = Model(response(model="qwen38-think"))
        service = VisionObservations(Journal(), model=model, clock=lambda: 10000)
        start(service)
        await model.entered.wait()
        for _ in range(20):
            if not service.running:
                break
            await asyncio.sleep(0)

        result = service.snapshot()["current"]
        assert result["phase"] == "error"
        assert result["error"] == "response_model"
        assert result["observation"]["hardware_control_allowed"] is False
        assert result["observation"]["crossing_allowed"] is False
        await service.close()

    asyncio.run(scenario())
