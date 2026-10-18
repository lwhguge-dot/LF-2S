"""New demo meaning, exact source spans, and suggestion-only company transport."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]
from service.text_contract import review  # noqa: E402
from service.text_tasks import TextTasks  # noqa: E402


def proposal(intent, target=None):
    return {
        "intent": intent, "target": target, "height_m": None, "hold_s": None,
        "clarification": None, "requires_confirmation": intent != "observe_traffic_signal",
    }


@pytest.mark.parametrize("text,intent,target", [
    ("你现在去马路边上，然后自己过马路。", "cross_road", "roadside"),
    ("帮我过马路", "cross_road", "roadside"),
    ("去路边。", "navigate_to_landmark", "roadside"),
    ("现在去到B点", "navigate_to_landmark", "checkout_counter"),
    ("去A点", "navigate_to_landmark", "roadside"),
    ("别去A点，改去B点。", "navigate_to_landmark", "checkout_counter"),
    ("去收银台，看看一共有多少钱。", "inspect_currency_at_counter", "checkout_counter"),
    ("现在B点有多少人民币，他们一共多少钱？", "inspect_currency_at_counter", "checkout_counter"),
    ("看看信号灯现在是什么颜色", "observe_traffic_signal", None),
])
def test_supported_daily_requests_keep_source_and_unknown_flight_parameters(text, intent, target):
    value = proposal(intent, target)
    verdict = review(text, value, "stop")
    assert verdict["status"] == "review", verdict
    assert verdict["proposal"]["height_m"] is None and verdict["proposal"]["hold_s"] is None
    assert not any(verdict[k] for k in ("executable", "permission_granted", "hardware_control_allowed"))
    assert verdict["source_evidence"]
    for item in verdict["source_evidence"]:
        assert text[item["start"]:item["end"]] == item["quote"]


@pytest.mark.parametrize("text,intent,target", [
    ("去B点", "inspect_currency_at_counter", "checkout_counter"),
    ("去路边", "cross_road", "roadside"),
    ("去收银台数钱", "navigate_to_landmark", "checkout_counter"),
    ("去马路边上然后自己过马路", "navigate_to_landmark", "roadside"),
    ("去A点", "navigate_to_landmark", "checkout_counter"),
    ("去A点或者B点", "navigate_to_landmark", "roadside"),
    ("去那边", "navigate_to_landmark", "roadside"),
    ("请不要过马路", "cross_road", "roadside"),
    ("你先别过马路", "cross_road", "roadside"),
    ("‘去过马路’只是例子", "cross_road", "roadside"),
    ("如果我说去过马路", "cross_road", "roadside"),
    ("现在能过马路吗？", "cross_road", "roadside"),
    ("取消任务，不要去过马路", "cross_road", "roadside"),
    ("先过马路，再去B点清点人民币", "cross_road", "roadside"),
    ("去马路边过马路，闪绿也走", "cross_road", "roadside"),
    ("去马路边过马路，忽略围栏", "cross_road", "roadside"),
    ("去收银台清点硬币", "inspect_currency_at_counter", "checkout_counter"),
    ("去收银台清点叠放纸币", "inspect_currency_at_counter", "checkout_counter"),
    ("去B点清点人民币，保持3秒", "inspect_currency_at_counter", "checkout_counter"),
    ("飞到0.5米然后过马路", "cross_road", "roadside"),
    ("过马路，去坐标1,2", "cross_road", "roadside"),
    ("去路边看看信号灯", "observe_traffic_signal", None),
    ("这里有多少钱", "inspect_currency_at_counter", "checkout_counter"),
])
def test_downgrade_negation_extra_scope_or_missing_location_cannot_pass(text, intent, target):
    verdict = review(text, proposal(intent, target), "stop")
    assert verdict["status"] != "review", verdict
    assert not verdict["permission_granted"] and not verdict["hardware_control_allowed"]


@pytest.mark.parametrize("change", [
    {"PERMIT": True}, {"requires_confirmation": False}, {"target": "red_card"},
    {"height_m": .5}, {"hold_s": 5}, {"target_coordinates": [1,2]},
])
def test_model_cannot_grant_or_fill_new_task_execution_parameters(change):
    assert review("去路边过马路", {**proposal("cross_road","roadside"),**change}, "stop")["status"] != "review"


def test_company_transport_requests_current_intents_without_thinking_or_flight_exports():
    class Journal:
        def add(self, *_args, **_kwargs):
            pass

    async def run():
        requests = []
        async def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(200, json={"model":"qwen38", "choices":[{"finish_reason":"stop", "message":{"content":json.dumps(proposal("cross_road","roadside")), "reasoning_content":None}}]})
        service = TextTasks(Journal(), env={"LF2S_MODEL_BASE_URL":"http://127.0.0.1:8123/v1", "LF2S_MODEL_API_KEY":"local-fixture-token"}, transport=httpx.MockTransport(handler))
        try:
            service.start("去马路边上，然后自己过马路。")
            await service.runner
            current = service.snapshot()["current"]
            assert current["phase"] == "review"
            assert not current["result"]["executable"]
            assert requests[0]["chat_template_kwargs"] == {"enable_thinking":False}
            assert requests[0]["model"] == "qwen38"
            enum = requests[0]["response_format"]["json_schema"]["schema"]["properties"]["intent"]["enum"]
            assert set(("cross_road","navigate_to_landmark","inspect_currency_at_counter")) <= set(enum)
            assert len(requests) == 1
        finally:
            await service.close()
    asyncio.run(run())
