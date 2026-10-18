"""One cancellable company-model request; returns suggestions only."""

import asyncio
import copy
import hashlib
import ipaddress
import os
import re
import secrets
import time
from urllib.parse import urlsplit

import httpx
from lf2s_protocol.wire import ProtocolError

from service.text_contract import SCHEMA, SYSTEM, precheck, result, review, strict_json


class TextTasks:
    def __init__(self, journal, *, env=None, transport=None, timeout=15):
        settings = os.environ if env is None else env
        self.journal = journal
        self.timeout = timeout
        self.model = settings.get("LF2S_MODEL_NAME", "qwen38")
        self.base = settings.get("LF2S_MODEL_BASE_URL", "").rstrip("/")
        self.key = settings.get("LF2S_MODEL_API_KEY", "")
        self.configuration_error = None
        self.configured = bool(self.base and self.key)
        if self.configured:
            try:
                address = urlsplit(self.base)
                ipaddress.IPv4Address(address.hostname)
                if (
                    address.scheme not in ("http", "https")
                    or address.path != "/v1"
                    or address.query
                    or address.fragment
                    or address.username
                    or address.password
                ):
                    raise ValueError("address")
                if (
                    self.model != "qwen38"
                    or not re.fullmatch(r"[\w./-]{1,128}", self.model)
                    or not 1 <= (address.port or 80) <= 65535
                ):
                    raise ValueError("model")
            except (ValueError, TypeError):
                self.configured = False
                self.configuration_error = (
                    "模型服务配置无效，请核对后台的地址与模型标识。"
                )
        self.client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=min(3, timeout)),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"Accept-Encoding": "identity"},
        )
        self.runner = None
        self.epoch = 0
        self.current = None
        self.closed = False

    @property
    def running(self):
        return self.runner is not None and not self.runner.done()

    def snapshot(self):
        return copy.deepcopy(
            {
                "configured": self.configured,
                "configuration_error": self.configuration_error,
                "model": self.model if self.configured else None,
                "thinking_enabled": False,
                "running": self.running,
                "current": self.current,
                "timeout_ms": int(self.timeout * 1000),
                "executable": False,
                "permission_granted": False,
                "hardware_control_allowed": False,
            }
        )

    def start(self, text):
        if self.closed:
            raise ProtocolError("文本建议服务已关闭")
        if self.running:
            raise ProtocolError("当前解析未结束，请先取消或清空")
        if not isinstance(text, str) or not text.strip() or len(text) > 512:
            raise ProtocolError("请输入 1～512 字的任务原文")
        self.epoch += 1
        job = {
            "id": secrets.token_hex(12),
            "epoch": self.epoch,
            "source_text": text,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "phase": "pending",
            "provider": "company_model",
            "elapsed_ms": None,
            "result": None,
            "model_output": None,
        }
        self.current = job
        preliminary = precheck(text)
        if preliminary:
            job.update(
                phase="clarify",
                provider="local_validation",
                elapsed_ms=0,
                result=preliminary,
            )
        elif not self.configured:
            self.current = None
            raise ProtocolError(
                self.configuration_error or "模型尚未配置，请按模型服务说明启动上位机"
            )
        else:
            self.runner = asyncio.create_task(self._run(job))
        return self.snapshot()

    async def clear(self, reason="建议已清空；新的输入需要重新解析"):
        self.epoch += 1
        if self.runner and not self.runner.done():
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        self.runner = None
        self.current = None
        return {**self.snapshot(), "reason": reason}

    async def close(self):
        self.closed = True
        await self.clear()
        await self.client.aclose()

    async def _request(self, text):
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": text},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "lf2s_text_review",
                    "strict": True,
                    "schema": SCHEMA,
                },
            },
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": False,
        }
        async with asyncio.timeout(self.timeout):
            async with self.client.stream(
                "POST",
                self.base + "/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer " + self.key},
            ) as response:
                if response.status_code != 200:
                    raise ValueError(
                        f"模型服务返回 HTTP {response.status_code}，未生成建议。"
                    )
                if (
                    response.headers.get("content-encoding", "identity").lower()
                    != "identity"
                ):
                    raise ValueError("模型响应压缩格式不受支持。")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    if len(data) + len(chunk) > 65536:
                        raise ValueError("模型响应超过 64 KiB 上限。")
                    data.extend(chunk)
        return strict_json(data.decode("utf-8"), max_chars=65536)

    async def _run(self, job):
        started = time.monotonic()
        try:
            response = await self._request(job["source_text"])
            if not isinstance(response, dict):
                raise ValueError("模型响应必须为对象")
            if (
                response.get("model") != self.model
                or not isinstance(response.get("choices"), list)
                or len(response.get("choices", [])) != 1
            ):
                raise ValueError("模型响应身份或结果数量不匹配。")
            choice = response["choices"][0]
            if not isinstance(choice, dict) or not isinstance(
                choice.get("message"), dict
            ):
                raise ValueError("模型结果结构不匹配")
            if choice["message"].get("reasoning_content") or choice["message"].get(
                "reasoning"
            ):
                raise ValueError("服务返回思考内容，与当前无思考设置不一致")
            content = choice["message"]["content"]
            verdict = review(
                job["source_text"], strict_json(content), choice.get("finish_reason")
            )
            raw = content
        except asyncio.CancelledError:
            return
        except (TimeoutError, httpx.TimeoutException):
            verdict, raw = (
                result("error", errors=["模型解析超时，旧请求不会自动重发。"]),
                None,
            )
        except httpx.HTTPError:
            verdict, raw = (
                result(
                    "error", errors=["模型服务连接失败，请检查公司服务和 Tailscale。"]
                ),
                None,
            )
        except (ValueError, UnicodeError, KeyError, TypeError, IndexError):
            verdict, raw = (
                result(
                    "rejected",
                    errors=["模型响应格式、身份或字段校验失败，请重新解析。"],
                ),
                None,
            )
        if job["epoch"] != self.epoch or self.closed:
            return
        job.update(
            phase=verdict["status"],
            result=verdict,
            model_output=raw,
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        )
        self.journal.add(
            "model",
            "文本建议：" + job["phase"],
            source="company_model",
            simulation=False,
            text_request_id=job["id"],
            source_sha256=job["source_sha256"],
            elapsed_ms=job["elapsed_ms"],
            permission_granted=False,
        )
