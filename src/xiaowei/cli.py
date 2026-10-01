"""唯一正式命令 ``xiaowei``（``python -m xiaowei`` 同此入口）：serve、storage init/upgrade/cleanup、
requests resend。

本模块在导入任何可能加载 ``agents`` 的模块之前，强制关闭 SDK 的模型与工具数据日志
（``OPENAI_AGENTS_DONT_LOG_MODEL_DATA`` / ``OPENAI_AGENTS_DONT_LOG_TOOL_DATA``）：SDK 在导入时读取
这两个环境变量，外部预置的 ``0`` / ``false`` 因此不会生效。运行装配在解析参数后才惰性导入；
飞书 SDK 在导入时绑定模块级事件循环，所以装配模块在 ``asyncio.run`` 之前导入。

输出只含状态、计数与请求编号；错误只给固定说明或异常类型，不含配置值、凭据、连接串或上游
原文。退出码：0 成功，1 运行失败或请求被拒，2 参数或配置错误。
"""

import os

os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
os.environ["OPENAI_AGENTS_DONT_LOG_TOOL_DATA"] = "1"

# 以下只导入标准库；运行装配在 main() 中惰性导入。
import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xiaowei.runtime import ServeConfig

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xiaowei", description="小维：StarRocks 只读数据助手")
    parser.add_argument("--config", type=Path, required=True, help="JSON 配置文件路径")
    parser.add_argument("--log-level", choices=_LOG_LEVELS, default="INFO")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="启动 Web 与可选飞书入口（持有实例锁并执行启动恢复）")

    storage = commands.add_parser("storage", help="PostgreSQL 显式维护").add_subparsers(
        dest="action", required=True
    )
    storage.add_parser("init", help="初始化全新数据库（独占实例锁）")
    storage.add_parser("upgrade", help="把应用表升级到当前版本（独占实例锁）")
    cleanup = storage.add_parser("cleanup", help="清理一批已过期的会话、请求与证据")
    cleanup.add_argument("--batch-size", type=int, default=100)

    requests = commands.add_parser("requests", help="已保存请求的显式操作").add_subparsers(
        dest="action", required=True
    )
    resend = requests.add_parser("resend", help="重发一条飞书 failed/unknown 结果（只发送一次）")
    resend.add_argument("--subject", required=True, help="内部 subject")
    resend.add_argument("--chat", required=True, help="飞书单聊 chat_id")
    resend.add_argument("--message", required=True, help="原消息的 message_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "storage" and args.action == "cleanup" and not 0 < args.batch_size <= 10_000:
        print("--batch-size 必须在 1 到 10000 之间", file=sys.stderr)
        return 2
    configure_logging(args.log_level)
    # 惰性导入：此时 SDK 日志开关已强制关闭，且尚未进入事件循环。
    from xiaowei import runtime

    try:
        config = runtime.load_config(args.config)
    except runtime.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(config, args))
    except Exception as exc:
        print(_failure(exc), file=sys.stderr)
        return 1


async def _run(config: "ServeConfig", args: argparse.Namespace) -> int:
    from xiaowei import runtime as rt

    if args.command == "serve":
        return await _serve(config)
    if args.command == "storage":
        if args.action == "init":
            await rt.initialize(config)
            print("storage initialized")
        elif args.action == "upgrade":
            print(f"storage version {await rt.upgrade(config)}")
        else:
            report = await rt.cleanup(config, batch_size=args.batch_size)
            print(f"cleaned sessions={report.sessions} unregistered={report.unregistered}")
        return 0
    outcome = await rt.resend(
        config, subject_id=args.subject, chat_id=args.chat, message_id=args.message
    )
    if outcome is None:
        print(f"{args.message} not_resendable", file=sys.stderr)
        return 1
    print(f"{args.message} {outcome}")
    return 0 if outcome == "sent" else 1


async def _serve(config: "ServeConfig") -> int:
    from xiaowei import runtime as rt

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        return await rt.serve(config, stop=stop)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def configure_logging(level: str) -> None:
    """``--log-level`` 只作用于本产品的 ``xiaowei`` 日志；第三方依赖只输出 WARNING 及以上。

    第三方的 INFO/DEBUG 会写出完整端点、连接地址与请求选项（如 httpx2 的 ``HTTP Request: POST
    <url>``、httpcore2 的连接日志、``OPENAI_LOG`` 打开的 openai 请求日志）。库可以自行调高自己
    logger 的级别，所以由输出端按来源过滤，而不只依赖根 logger 的级别。
    """
    threshold = max(logging.WARNING, logging.getLevelNamesMapping()[level])
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(lambda record: _own(record.name) or record.levelno >= threshold)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(threshold)
    logging.getLogger("xiaowei").setLevel(level)


def _own(name: str) -> bool:
    return name == "xiaowei" or name.startswith("xiaowei.")


def _failure(exc: Exception) -> str:
    """本包错误的消息是固定说明；其余只给类型名，不复制可能含连接信息或上游原文的消息。"""
    if type(exc).__module__.startswith("xiaowei."):
        return f"失败：{exc}"
    return f"失败：{type(exc).__name__}"
