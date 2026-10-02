"""子进程脚本：用真实锁定版飞书 SDK 的端点探针制造 WARNING/ERROR，输出全部 stdout/stderr。

用法：``python -m tests.sdk_core.lark_log_canary <entry|control> <endpoint> <port> <level>``
（``level`` 为 INFO 或 DEBUG）。

- ``entry`` 先导入正式入口 ``xiaowei.cli``，再用正式入口的 ``configure_logging`` 配置日志，并写
  本产品的事件与失败原因码。
- ``control`` 不导入正式入口，用根 logger 的同一级别：证明飞书 SDK 自带的 stdout handler 与
  根 logger 都会写出原始异常（含主机、端口与带 ``endpoint`` 的完整路径）。

探针连接本机已关闭的端口，不访问真实飞书。不由 pytest 收集。
"""

import sys

if sys.argv[1] == "entry":
    import xiaowei.cli  # 正式入口的第一步

import logging

from lark_channel.ws import client as ws_client

ENDPOINT, PORT, LEVEL = sys.argv[2], sys.argv[3], sys.argv[4]

if sys.argv[1] == "entry":
    xiaowei.cli.configure_logging(LEVEL)
    product = logging.getLogger("xiaowei.canary")
    product.log(logging.getLevelNamesMapping()[LEVEL], "product event")
    product.warning("飞书长连接不可用 reason=feishu_unavailable")
else:
    logging.basicConfig(level=LEVEL, stream=sys.stderr)

probe = ws_client.Client(
    "cli_canary", f"secret-{ENDPOINT}", domain=f"http://127.0.0.1:{PORT}/{ENDPOINT}"
)
print(f"probe {probe.probe_endpoint(timeout=2)}")
# 依赖在失败与重连路径上的 ERROR，以及其他第三方的 WARNING，同样可能带端点与原始异常。
error = ConnectionError(f"http://127.0.0.1:{PORT}/{ENDPOINT}/callback/ws/endpoint")
logging.getLogger("Lark").error("reconnect failed, err: %s", error)
logging.getLogger("httpx").warning("request to %s failed", error)
