"""运行时配置。"""

from agents import set_trace_processors, set_tracing_disabled


def configure_runtime() -> None:
    """显式关闭 SDK tracing，并移除默认的 trace 导出处理器。

    SDK 默认开启 tracing，并在首次使用时注册向 OpenAI 后端导出的处理器。这里同时做两件事：
    关闭 trace 生成，并清空处理器列表，使默认导出器不再挂在 provider 上。
    """
    set_tracing_disabled(True)
    set_trace_processors([])
