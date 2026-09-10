"""与展示技术无关的统一推荐核心。"""

from .service import RecommendationPolicy, RecommendationService
from .stage_projection import apply_scoped_stage_stats

__all__ = ["RecommendationPolicy", "RecommendationService", "apply_scoped_stage_stats"]
