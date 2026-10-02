"""子进程脚本：在给定日志级别下跑一轮“模型调用工具 → 工具结果 → 最终回答”，输出全部日志。

用法：``python -m tests.sdk_core.sdk_log_canary <entry|control> <canary> <endpoint> <level>``
（``level`` 为 INFO 或 DEBUG）。

- ``entry`` 先导入正式入口 ``xiaowei.cli``（与 console script 和 ``python -m xiaowei`` 相同的
  第一步），再用正式入口的 ``configure_logging`` 配置日志，并写一条本产品的日志。
- ``control`` 不导入正式入口，用根 logger 的同一级别：证明外部预置 ``0`` / ``false`` 时 SDK 会把
  数据 canary 写进日志，且 httpx2 的成功请求日志会写出含 ``endpoint`` 的完整端点。

模型是真实 ``OpenAIResponsesModel`` + HTTP mock transport（返回成功响应），不建立网络连接。
不由 pytest 收集。
"""

import sys

if sys.argv[1] == "entry":
    import xiaowei.cli  # 正式入口的第一步：强制 SDK 日志开关

import asyncio
import json
import logging

import httpx2
from agents import Agent, RunConfig, Runner, function_tool
from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI

CANARY, ENDPOINT, LEVEL = sys.argv[2], sys.argv[3], sys.argv[4]


def _body(output: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "m",
        "status": "completed",
        "output": output,
        "tool_choice": "auto",
        "tools": [],
        "parallel_tool_calls": False,
    }


def _respond(request: httpx2.Request) -> httpx2.Response:
    sent = json.loads(request.content)
    if not any(item.get("type") == "function_call_output" for item in sent["input"]):
        call: dict[str, object] = {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": json.dumps({"key": f"arg-{CANARY}"}),
            "status": "completed",
        }
        return httpx2.Response(200, json=_body([call]))
    text = {"type": "output_text", "text": f"final-{CANARY}", "annotations": []}
    message: dict[str, object] = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [text],
    }
    return httpx2.Response(200, json=_body([message]))


@function_tool
def lookup(key: str) -> str:
    """返回合成记录。"""
    return f"tool-output-{CANARY}"


async def main() -> None:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(_respond))
    client = AsyncOpenAI(
        api_key="sk-canary-test", base_url=f"https://model.test/{ENDPOINT}/v1", http_client=http
    )
    agent = Agent(name="canary", model=OpenAIResponsesModel("m", client), tools=[lookup])
    config = RunConfig(tracing_disabled=True)
    result = await Runner.run(agent, f"user-{CANARY}", run_config=config)
    await client.close()
    print("final output received" if CANARY in str(result.final_output) else "no final output")


if sys.argv[1] == "entry":
    xiaowei.cli.configure_logging(LEVEL)
    logging.getLogger("xiaowei.canary").log(logging.getLevelNamesMapping()[LEVEL], "product event")
else:
    logging.basicConfig(level=LEVEL, stream=sys.stderr)
asyncio.run(main())
