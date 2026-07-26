"""Desktop 子进程输出管道辅助。

此模块只负责安全消费后台服务的 stdout/stderr，并保留有限错误尾部；
不参与进程启动、生命周期或业务状态判断，避免运行时进程模块继续膨胀。
"""

from __future__ import annotations


def drain_process_stream(
    stream,
    *,
    tail: list[str] | None = None,
) -> None:
    """持续消费子进程文本管道，避免刷新日志填满 Windows pipe。"""

    try:
        while True:
            raw_line = stream.readline()
            if not raw_line:
                return
            line = raw_line.rstrip("\r\n")
            if tail is not None and line:
                tail.append(line)
                del tail[:-20]
    except (OSError, ValueError):
        return


def pipe_tail_text(lines: list[str]) -> str:
    """将有限尾部压缩为可诊断但不会无限增长的错误文本。"""

    return "\n".join(lines)[-500:]
