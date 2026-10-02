"""``python -m xiaowei``：与 console script 相同的入口。

先导入 ``xiaowei.cli``，由它在任何 ``agents`` 导入前强制 SDK 日志开关。
"""

from xiaowei.cli import main

raise SystemExit(main())
