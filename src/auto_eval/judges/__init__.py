"""评测引擎：垂域视觉评测 / 垂域视觉对比评测（裁判单轮直出）。"""
from .base import JudgeClient
from .rich_content_judge import RichContentJudge, rich_content_result_fields
from .visual_compare_judge import VisualCompareJudge, visual_compare_result_fields

__all__ = [
    "JudgeClient",
    "RichContentJudge",
    "rich_content_result_fields",
    "VisualCompareJudge",
    "visual_compare_result_fields",
]
