"""外部协议与核心 DTO 之间的技术适配器。"""

from .runtime_session import build_runtime_session, game_context_from_runtime, vision_selection_from_runtime

__all__ = ["build_runtime_session", "game_context_from_runtime", "vision_selection_from_runtime"]
