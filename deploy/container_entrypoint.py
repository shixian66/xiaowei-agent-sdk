#!/usr/bin/env python3
"""小维运行镜像的固定入口；容器模式不能由配置或环境变量开启。"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from xiaowei.cli import container_main
from xiaowei.runtime import ConfigError, load_config

_CONFIG = Path("/etc/xiaowei/xiaowei.json")


def _healthcheck() -> int:
    try:
        config = load_config(_CONFIG)
        url = f"http://127.0.0.1:{config.listen_port}/readyz"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as response:
            body = json.load(response)
        return 0 if response.status == 200 and body.get("status") == "ready" else 1
    except (ConfigError, OSError, ValueError, urllib.error.URLError):
        return 1


def main() -> int:
    if sys.argv[1:] == ["healthcheck"]:
        return _healthcheck()
    return container_main()


if __name__ == "__main__":
    raise SystemExit(main())
