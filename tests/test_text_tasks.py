"""Model-output faults, original-unit evidence, cancellation, and API isolation."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]
from service.app import create_app  # noqa: E402
from service.text_contract import precheck, quantities, review, strict_json  # noqa: E402
from service.text_tasks import TextTasks  # noqa: E402

GOOD = {
    "intent": "takeoff_hold_land",
    "height_m": 0.5,
    "hold_s": 3,
    "target": None,
    "clarification": None,
    "requires_confirmation": True,
}
TEXT = "起飞到五十厘米，保持三秒，然后降落。"
ENV = {
    "LF2S_MODEL_BASE_URL": "http://127.0.0.1:8123/v1",
    "LF2S_MODEL_API_KEY": "private-test-token",
    "LF2S_MODEL_NAME": "qwen38",
}


class Journal:
    def __init__(self):
        self.events = []

    def add(self, *args, **kwargs):
        self.events.append((args, kwargs))


def response(value=GOOD, *, model="qwen38", reason="stop", reasoning=None):
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": reason,
                "message": {
                    "content": json.dumps(value),
                    "reasoning_content": reasoning,
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "text,height,duration",
    [
        (TEXT, 0.5, 3),
        ("起飞到0.8米，保持1500毫秒，然后落地", 0.8, 1.5),
        ("起飞到半米，保持两秒，然后降落", 0.5, 2),
        ("起飞到５００毫米，保持０．５分钟，然后降落", 0.5, 30),
        ("起飞到零点五米，保持一千五百毫秒，然后降落", 0.5, 1.5),
    ],
)
def test_quantities_are_backed_by_exact_original_spans(text, height, duration):
    value = {**GOOD, "height_m": height, "hold_s": duration}
    verdict = review(text, value, "stop")
    assert verdict["status"] == "review", verdict
    assert not verdict["executable"] and not verdict["permission_granted"]
    for item in verdict["source_evidence"]:
        assert text[item["start"] : item["end"]] == item["quote"]


@pytest.mark.parametrize(
    "text",
    [
        "起飞到50，保持两秒，再落地",
        "起飞后保持三秒再降落",
        "飞高一点",
        "起飞到一米半，保持三秒再降落",
        "起飞到零米，保持三秒再降落",
        "起飞到-0.5米，保持三秒再降落",
        "起飞到50厘米，再升到80厘米，保持三秒然后降落",
        "起飞到三五厘米，保持三秒再降落",
    ],
)
def test_missing_units_or_ambiguous_numbers_clarify_without_values(text):
    verdict = precheck(text)
    assert verdict and verdict["status"] == "clarify" and verdict["proposal"] is None
    assert review(text, GOOD, "stop")["status"] != "review"


@pytest.mark.parametrize(
    "text",
    [
        "如果允许，起飞到0.5米保持3秒然后降落",
        "不要起飞到0.5米保持3秒然后降落",
        "‘起飞到0.5米保持3秒然后降落’只是一个例子",
        "起飞到0.5米保持3秒再降落吗？",
        "起飞到0.5米，向前飞1米，保持3秒后降落",
    ],
)
def test_quoted_conditional_negated_or_extra_actions_never_become_reviewable_action(
    text,
):
    assert review(text, GOOD, "stop")["status"] != "review"


@pytest.mark.parametrize(
    "change",
    [
        {"height_m": 0.8},
        {"hold_s": 5},
        {"requires_confirmation": False},
        {"PERMIT": True},
        {"height_m": float("nan")},
        {"height_m": True},
        {"height_m": float("inf")},
        {"height_m": None},
    ],
)
def test_hallucinated_or_invalid_fields_never_pass(change):
    assert review(TEXT, {**GOOD, **change}, "stop")["status"] != "review"


@pytest.mark.parametrize(
    "raw",
    ['{"intent":"cancel","intent":"query_status"}', '{"x":NaN}', '{"x":Infinity}', "["],
)
def test_malformed_duplicate_or_nonfinite_json_rejected(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


def test_red_card_target_requires_source_evidence():
    value = {
        "intent": "find_target",
        "height_m": None,
        "hold_s": None,
        "target": "red_card",
        "clarification": None,
        "requires_confirmation": True,
    }
    assert review("寻找一张红色卡片，并降落到上面", value, "stop")["status"] == "review"
    assert review("寻找一张蓝色卡片", value, "stop")["status"] == "rejected"
    assert (
        quantities("“五十厘米”是例子，实际起飞到80厘米")["height_m"][0]["quote"]
        == "80厘米"
    )


@pytest.mark.parametrize(
    "text,height,duration",
    [
        ("起飞到一米五，保持三秒，然后降落。", 1, 3),
        ("起飞到.5米，保持三秒，然后降落。", 5, 3),
        ("起飞到1e3米，保持三秒，然后降落。", 3, 3),
        ("起飞到1/2米，保持三秒，然后降落。", 2, 3),
        ("起飞到三分之一米，保持三秒，然后降落。", 1, 3),
        ("起飞到五十厘米半，保持三秒，然后降落。", 0.5, 3),
        ("起飞到一米五厘米，保持三秒，然后降落。", 0.05, 3),
        ("起飞到半米，保持1e3秒，然后降落。", 0.5, 3),
    ],
)
def test_partial_numeric_matches_cannot_validate_a_different_quantity(
    text, height, duration
):
    assert precheck(text)["status"] == "clarify"
    assert (
        review(text, {**GOOD, "height_m": height, "hold_s": duration}, "stop")["status"]
        != "review"
    )


def test_model_call_is_explicit_non_thinking_and_carries_no_permission():
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer private-test-token"
        return httpx.Response(200, json=response())

    async def scenario():
        provider = TextTasks(Journal(), env=ENV, transport=httpx.MockTransport(handler))
        try:
            assert calls == [] and provider.snapshot()["thinking_enabled"] is False
            provider.start(TEXT)
            await provider.runner
            state = provider.snapshot()
            assert state["current"]["phase"] == "review"
            assert "private-test-token" not in json.dumps(state)
            assert (
                not state["hardware_control_allowed"]
                and not state["permission_granted"]
            )
            assert calls[0]["model"] == "qwen38"
            assert calls[0]["chat_template_kwargs"] == {"enable_thinking": False}
            assert calls[0]["stream"] is False and len(calls) == 1
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        response(reason="length"),
        response(model="qwen38-think"),
        response(reasoning="unexpected thinking"),
        [],
        {"model": "qwen38", "choices": []},
        {"model": "qwen38", "choices": [None]},
        {"model": "qwen38", "choices": [{"message": []}]},
        {"model": "qwen38", "choices": "x"},
        response(value={**GOOD, "requires_confirmation": False}),
    ],
)
def test_wrong_model_reasoning_truncation_and_response_shape_are_rejected(payload):
    async def scenario():
        provider = TextTasks(
            Journal(),
            env=ENV,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        )
        try:
            provider.start(TEXT)
            await provider.runner
            current = provider.snapshot()["current"]
            assert (
                current["phase"] == "rejected" and current["result"]["proposal"] is None
            )
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_local_missing_parameter_checks_do_not_contact_server():
    async def unexpected(_request):
        raise AssertionError("Missing units should not be sent to the model")

    async def scenario():
        provider = TextTasks(
            Journal(), env=ENV, transport=httpx.MockTransport(unexpected)
        )
        try:
            state = provider.start("起飞到50，保持两秒再落地")
            assert state["current"]["phase"] == "clarify" and not state["running"]
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_cancellation_and_deadline_do_not_block_other_loop_work_or_replay_old_result():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(_request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=response())

        provider = TextTasks(
            Journal(), env=ENV, transport=httpx.MockTransport(slow), timeout=0.05
        )
        try:
            provider.start(TEXT)
            await entered.wait()
            await provider.clear()
            release.set()
            await asyncio.sleep(0.01)
            assert provider.snapshot()["current"] is None and not provider.running
            entered.clear()
            release.clear()
            provider.start(TEXT)
            await provider.runner
            assert provider.snapshot()["current"]["phase"] == "error"
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"LF2S_MODEL_NAME": "qwen38-think"},
        {"LF2S_MODEL_BASE_URL": "http://127.0.0.1:8123/admin"},
    ],
)
def test_thinking_model_or_arbitrary_endpoint_cannot_be_configured(change):
    async def scenario():
        provider = TextTasks(Journal(), env={**ENV, **change})
        try:
            assert not provider.configured
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_text_api_has_no_action_side_effect_and_replay_clears_suggestion(tmp_path):
    app = create_app(ROOT, "text-test", 41234, tmp_path)
    provider = app.state.text_tasks
    headers = {"Authorization": "Bearer text-test"}
    with TestClient(
        app,
        base_url="http://127.0.0.1:41234",
        client=("127.0.0.1", 12345),
        headers=headers,
    ) as client:
        before = client.get("/api/v1/state").json()
        assert (
            client.post(
                "/api/v1/text-tasks/parse",
                json={"text": "起飞到50，保持两秒再落地", "confirmed": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/text-tasks/parse", json={"text": "起飞到50，保持两秒再落地"}
            ).json()["current"]["phase"]
            == "clarify"
        )
        after = client.get("/api/v1/state").json()
        assert after["counters"]["sent"] == before["counters"]["sent"] == 0
        assert after["task"]["current"] is None
        assert (
            client.post(
                "/api/v1/text-tasks/execute", json={"confirmed": True}
            ).status_code
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
        assert provider.snapshot()["current"] is None
        assert (
            client.post("/api/v1/text-tasks/parse", json={"text": TEXT}).status_code
            == 409
        )
