"""Explicit, single in-flight, non-thinking qwen38 image observation requests."""

import asyncio
import base64
import ipaddress
from urllib.parse import urlsplit

import httpx

from .contracts import ObservationError, schema

SYSTEM = (
    "你是前向相机的画面观察器，只返回图片中的可见事实，不决定飞行或通行。"
    "signals逐个列出真实可辨认的信号灯组，不把普通彩色物体当灯。多色同亮为ambiguous，"
    "没亮为off，不能判断为unknown；不要从单张图片推断闪烁。"
    "box为图像左上原点、x向右y向下的[x0,y0,x1,y1]整数，归一化到0到1000。"
    "zebra_visible与checkout_visible分别表示可见斑马线与收银台，不确定填null。"
    "currency仅列出当前图片里可见的人民币纸币，每张一个框；denomination_fen用整数分，"
    "1元=100分，无法识读面额填null。不要输出合计金额。"
    "visible_notes_complete仅在当前可见范围的纸币全部枚举且完整清晰时为true，"
    "遗漏、裁切、遮挡或模糊时为false，不确定为null；不能由一张图保证柜台全景完整。"
    "只输出Schema要求的JSON，不输出思考、解释、动作、许可或额外字段。"
)


class VisionModel:
    def __init__(self, base_url, api_key, policy, *, transport=None):
        try:
            address = urlsplit(base_url)
            ipaddress.IPv4Address(address.hostname)
            if (
                address.scheme not in ("http", "https")
                or address.path.rstrip("/") != "/v1"
                or address.query
                or address.fragment
                or address.username
                or address.password
                or not 1 <= (address.port or 80) <= 65535
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise ObservationError("explicit_model_endpoint_required") from None
        if (
            not isinstance(api_key, str)
            or not 1 <= len(api_key) <= 4096
            or any(ord(c) < 32 for c in api_key)
        ):
            raise ObservationError("process_api_key_required")
        self.base_url, self.policy = base_url.rstrip("/"), policy
        self.busy = self.closed = False
        self.client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            timeout=httpx.Timeout(policy.request_timeout_ms / 1000),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={
                "Accept-Encoding": "identity",
                "Authorization": "Bearer " + api_key,
            },
        )

    async def close(self):
        self.closed = True
        await self.client.aclose()

    async def request(self, frame, image_info):
        if self.closed or self.busy:
            raise ObservationError("provider_closed_or_busy")
        self.busy = True
        payload = {
            "model": "qwen38",
            "temperature": 0,
            "max_tokens": 2048,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "lf2s_demo_observation",
                    "strict": True,
                    "schema": schema(self.policy),
                },
            },
            "messages": [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "观察图片中的灯组、斑马线、收银台和纸币。只返回JSON。",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:"
                                + image_info["mime"]
                                + ";base64,"
                                + base64.b64encode(frame.image).decode("ascii")
                            },
                        },
                    ],
                },
            ],
        }
        try:
            async with asyncio.timeout(self.policy.request_timeout_ms / 1000):
                async with self.client.stream(
                    "POST", self.base_url + "/chat/completions", json=payload
                ) as response:
                    if response.status_code != 200:
                        raise ObservationError(
                            "model_http_status_" + str(response.status_code)
                        )
                    if (
                        response.headers.get("content-encoding", "identity").lower()
                        != "identity"
                    ):
                        raise ObservationError("response_encoding")
                    result = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=4096):
                        if len(result) + len(chunk) > 65536:
                            raise ObservationError("response_size")
                        result.extend(chunk)
            return bytes(result)
        except (httpx.TimeoutException, TimeoutError):
            raise ObservationError("model_timeout") from None
        except httpx.ConnectError:
            raise ObservationError("model_connect") from None
        except httpx.HTTPError:
            raise ObservationError("model_transport") from None
        finally:
            self.busy = False
