"""核心数据模型。

所有跨模块流转的结构都在这里定义，用 pydantic v2。
注意 `EvalItem.reference` 仅用于元评测，禁止流入作答与盲评（见 dataset.to_prompt 的强制隔离）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Correctness = Literal["right", "wrong", "partial", "unclear"]
Winner = Literal["a", "b", "tie"]
Difficulty = Literal["easy", "medium", "hard"]


# --------------------------------------------------------------------------- #
# 评测集
# --------------------------------------------------------------------------- #
class EvalItem(BaseModel):
    """一条评测题。"""

    id: str
    question: str
    context: str | None = None  # 可选背景/多模态描述
    has_ref: bool = True
    reference: str | None = None  # ⚠️ 仅元评测使用，禁止进入作答/盲评
    key_points: list[str] = Field(default_factory=list)  # 可接受要点（仅元评测辅助）
    category: str | list[str] = "default"  # 类目（切片用）
    difficulty: Difficulty = "medium"
    tags: list[str] = Field(default_factory=list)
    trace: str | None = None  # 被测 agent 的推理/工具轨迹（仅过程盲评使用）
    metadata: dict[str, Any] = Field(default_factory=dict)

    def categories(self) -> list[str]:
        return [self.category] if isinstance(self.category, str) else list(self.category)


# --------------------------------------------------------------------------- #
# 模型作答
# --------------------------------------------------------------------------- #
class ModelOutput(BaseModel):
    """一个被测模型对一道题的盲答（不含 reference）。"""

    item_id: str
    model: str
    answer: str
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    error: str | None = None  # 调用失败时填错误信息，answer 为空
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.answer)


# --------------------------------------------------------------------------- #
# 盲评：单裁判原始记录（多裁判集成的输入）
# --------------------------------------------------------------------------- #
class SingleScore(BaseModel):
    """单个裁判对「一个答案」的一次 rubric 评分。"""

    item_id: str
    model: str
    judge: str
    persona: str | None = None
    run_idx: int = 0  # 重复采样索引
    rubric: dict[str, int] = Field(default_factory=dict)  # 各维度分（通常 1–5）
    rubric_reasons: dict[str, str] = Field(default_factory=dict)  # 各一级维度打分理由
    total: float = 0.0
    correctness: Correctness = "unclear"
    error_type: str | None = None
    rationale: str = ""
    analysis: str = ""  # 裁判深度思考过程（意图理解/理想画像/多角度分析）
    used_search: bool = False
    search_queries: list[str] = Field(default_factory=list)
    tool_trace: list[str] = Field(default_factory=list)  # 评测 agent 的工具调用轨迹
    truncated: bool = False  # 是否被 max_rounds 截断（已强制判定兜底）
    latency_ms: int = 0


class SinglePair(BaseModel):
    """单个裁判对「一对答案」的一次成对盲比较。

    `order` 记录呈现给裁判时 model_a/model_b 的左右顺序，用于位置偏差分析；
    `winner` 始终相对于固定的 (model_a, model_b) 归一化。
    """

    item_id: str
    model_a: str
    model_b: str
    judge: str
    run_idx: int = 0
    order: Literal["ab", "ba"] = "ab"
    winner: Winner = "tie"
    rationale: str = ""


# --------------------------------------------------------------------------- #
# 盲评：聚合结论（多裁判集成后）
# --------------------------------------------------------------------------- #
class Verdict(BaseModel):
    """一道题 × 一个模型的聚合盲评结论。"""

    item_id: str
    model: str
    rubric: dict[str, float] = Field(default_factory=dict)  # 各维度均分
    rubric_reasons: dict[str, str] = Field(default_factory=dict)  # 各一级维度打分理由（多裁判合并）
    total: float = 0.0
    correctness: Correctness = "unclear"
    error_type: str | None = None
    rationale: str = ""
    # —— 可靠性信号 —— #
    n_judges: int = 0
    judges_agreement: float | None = None  # 多裁判一致率（0–1）
    repeat_std: float | None = None  # 同裁判重复采样的总分标准差
    low_agreement: bool = False  # 一致率/稳定性低于阈值 → 标红人工复核
    single_scores: list[SingleScore] = Field(default_factory=list)
    # —— 主席仲裁（仅 low_agreement 触发；仲裁后 correctness/total/rubric 即主席最终结论）—— #
    arbitrated: bool = False
    arbitrator_confidence: float | None = None
    arbitrator_rationale: str | None = None


class PairResult(BaseModel):
    """一道题上 (model_a vs model_b) 的聚合成对比较结果。"""

    item_id: str
    model_a: str
    model_b: str
    a_wins: int = 0
    b_wins: int = 0
    ties: int = 0
    winner: Winner = "tie"  # 多数投票归一化结果
    win_rate_a: float = 0.0  # a 的胜率 = a_wins / (a_wins+b_wins+ties)，tie 计半
    rationale: str = ""
    agreement: float | None = None
    bidirectional_consistent: bool = True  # 双向比较是否一致
    low_agreement: bool = False
    single_pairs: list[SinglePair] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 元评测（用 reference 校验评测 agent 本身）
# --------------------------------------------------------------------------- #
class MetaResult(BaseModel):
    """一道题 × 一个模型的元评测：盲评结论 vs 参考答案客观真值。"""

    item_id: str
    model: str
    has_ref: bool
    category: str = "default"
    difficulty: Difficulty = "medium"
    # 客观真值（由 reference 派生）
    objective: dict[str, float] = Field(default_factory=dict)  # {em, f1, sim}
    objective_correct: Literal["right", "wrong", "partial", "na"] = "na"
    # 盲评结论
    judge_correctness: Correctness | None = None
    judge_total: float | None = None
    # 对照
    agree: bool | None = None  # 盲判对错 == 客观对错
    delta_total_vs_sim: float | None = None  # 盲评分(归一) − 语义相似度


# --------------------------------------------------------------------------- #
# 运行级配置与产物索引
# --------------------------------------------------------------------------- #
class RunManifest(BaseModel):
    """一次运行的元信息，写入 runs/<run_id>/manifest.json。"""

    run_id: str
    dataset: str
    n_items: int
    models: list[str]
    judges: list[str]
    started_at: str
    finished_at: str | None = None
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
