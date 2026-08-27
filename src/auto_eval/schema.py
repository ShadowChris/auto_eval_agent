"""核心数据模型。

所有跨模块流转的结构都在这里定义，用 pydantic v2。
仅保留垂域视觉评测（rich_content）与垂域视觉对比评测（compare）所需模型。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AnswerCoverage = Literal["complete", "partial", "unclear"]
CompareWinner = Literal["answer1", "answer2", "tie"]
ConflictVerdict = Literal["yes", "no", "unclear"]


# --------------------------------------------------------------------------- #
# 评测集
# --------------------------------------------------------------------------- #
class EvalItem(BaseModel):
    """一条评测题。"""

    id: str
    question: str
    context: str | None = None  # 可选背景/多模态描述
    category: str = "default"  # 垂域（分组展示用）
    media: list[str] = Field(default_factory=list)  # 任务类评测：录屏/图片本地路径（裁判抽帧后以 image_url 多图盲评）
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 垂域视觉评测（rich_content）
# --------------------------------------------------------------------------- #
class RichContentCard(BaseModel):
    """视觉裁判识别到的一张结构化富内容挂卡。"""

    type: str
    entity: str = ""
    visible_content: str = ""
    answer_position: str = ""
    evidence_frames: list[int] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RichContentSuperlink(BaseModel):
    """回答区域中一处可点击蓝色文字；同一处跨帧重复只保留一条。"""

    text: str
    answer_position: str = ""
    surrounding_context: str = ""
    evidence_frames: list[int] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RichContentObservation(BaseModel):
    """一次垂域视觉评测视频识别结果。"""

    answer_coverage: AnswerCoverage = "unclear"
    visual_description: str = ""  # 纯客观视觉描述（Part 1），不包含评价性语言
    turn_summary: str = ""  # 本轮总结(≤120字)：用户意图+核心结果/关键实体+是否闭环，供多轮下一轮 context 用
    cards: list[RichContentCard] = Field(default_factory=list)
    superlinks: list[RichContentSuperlink] = Field(default_factory=list)
    needs_review: bool = False
    review_reason: str = ""
    card_suitability: str = ""  # Part 2：卡片是否合适（"ok"/"nok"/""）
    card_suitability_reason: str = ""  # Part 2：卡片是否合适的原因
    superlink_suitability: str = ""  # Part 2：Superlink是否合适（"ok"/"nok"/""）
    superlink_suitability_reason: str = ""  # Part 2：Superlink是否合适的原因
    problem_solved: str = ""  # Part 2：是否解决了用户问题（"ok"/"nok"/"need_review"）
    problem_solved_reason: str = ""  # Part 2：评价的原因
    answer_issues: str = ""  # Part 2：回答的内容有什么问题（分类标签：具体描述）
    rationale: str = ""


# --------------------------------------------------------------------------- #
# 垂域视觉对比评测（compare）
# --------------------------------------------------------------------------- #
class VisualCompareObservation(BaseModel):
    """一次多模态视觉对比评测的完整结果。

    对比两个产品回答录屏，从五个维度评判优劣，并检查内容冲突。
    """

    relevance: CompareWinner | None = None
    relevance_reason: str = ""
    safety: CompareWinner | None = None
    safety_reason: str = ""
    content_quality: CompareWinner | None = None
    content_quality_reason: str = ""
    need_closure: CompareWinner | None = None
    need_closure_reason: str = ""
    personalization: CompareWinner | None = None
    personalization_reason: str = ""

    has_conflict: ConflictVerdict = "unclear"
    conflict_reason: str = ""

    needs_review: bool = False
    review_reason: str = ""
    rationale: str = ""
