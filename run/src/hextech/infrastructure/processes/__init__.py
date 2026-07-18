"""受 DataService 所有权约束的隔离子进程设施。"""

from .isolated import IsolatedProcessResult, run_isolated_process

__all__ = ["IsolatedProcessResult", "run_isolated_process"]
