"""配置加载：从 config/*.yaml 读取并校验为强类型配置对象。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class ModelConfig(BaseModel):
    """被测模型配置。不同 runner 用其中不同字段子集。"""

    name: str
    runner: str  # openai_compat | http | func | cli
    # 通用
    concurrency: int = 4
    temperature: float = 0.0
    max_tokens: int | None = None
    rpm: int | None = None  # 每分钟请求数上限
    tpm: int | None = None  # 每分钟 token 数上限
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 90.0
    total_timeout_s: float = 180.0
    max_attempts: int = 4
    retry_base_s: float = 1.0
    retry_max_s: float = 20.0
    stream_include_usage: bool = True
    # openai_compat
    base_url: str | None = None
    api_key_env: str | None = None  # 环境变量名
    model: str | None = None  # endpoint id / 模型名
    # http
    url: str | None = None
    method: str = "POST"
    prompt_field: str = "prompt"  # 请求体里 prompt 的字段名
    answer_jsonpath: str = "$.answer"  # 响应取回答的 jsonpath（简化：$.a.b）
    headers: dict[str, str] = Field(default_factory=dict)
    # func
    func_module: str | None = None  # e.g. "mypkg.agent:chat"
    # cli
    command: list[str] | None = None  # e.g. ["python", "-m", "myagent"]
    # 其余透传
    extra: dict[str, Any] = Field(default_factory=dict)

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class JudgeConfig(BaseModel):
    """裁判配置（多裁判）。"""

    name: str
    display: str | None = None  # 前端显示名（如中文"研发人员"），缺省回落 name
    runner: str = "openai_compat"
    base_url: str | None = None
    api_key_env: str | None = None
    api_key_value: str | None = Field(default=None, exclude=True, repr=False)
    model: str | None = None
    provider_id: str | None = None
    provider_name: str | None = None
    provider_revision: str | None = None
    persona: str | None = None  # strict_expert | end_user | safety_reviewer | ...
    enable_web_search: bool = False
    enable_fetch: bool = True  # 允许裁判抓取网页正文深入核实
    enable_calculate: bool = True  # 允许裁判用算术求值核查计算题
    enable_python: bool = False  # 允许裁判执行代码核查编程题（注意安全，默认关）
    temperature: float = 0.0
    concurrency: int = 4
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 90.0
    total_timeout_s: float = 180.0
    max_attempts: int = 5
    retry_base_s: float = 1.0
    retry_max_s: float = 20.0
    stream_include_usage: bool = True

    def api_key(self) -> str | None:
        if self.api_key_value is not None:
            return self.api_key_value
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class SubDim(BaseModel):
    name: str
    description: str = ""
    scale: int = 5


class RubricDim(BaseModel):
    name: str
    description: str
    weight: float = 1.0
    scale: int = 5
    criteria: list[str] = Field(default_factory=list)  # 仅作为评分检查项，不单独输出分数
    score_anchors: dict[int, str] = Field(default_factory=dict)  # 各分值对应的评分标准
    sub_dimensions: list[SubDim] = Field(default_factory=list)  # 一级下有二级则渲染二级，裁判按二级评分  # 满分


class EvalOptions(BaseModel):
    repeat: int = 1  # 同裁判重复采样次数（算稳定性）
    pairwise_bidirectional: bool = True  # A/B 双向比较抗位置偏差
    independent_then_compare: bool = True  # 先独立盲评再成对比较
    pairwise_for_ref: bool = False  # 有参考答案题是否也做成对比较
    search_provider: str | list[str] | None = None  # 单源(str)或多源(list)；与 search_providers 合并去重
    search_providers: list[str] = Field(default_factory=list)  # 多源聚合：配多个则并行汇总，缺 key 的源自动跳过
    search_topk: int = 3
    classify_model: str | None = None  # 轻量垂域分类专用模型（不填则用裁判自己的 model）
    classify_base_url: str | None = None  # 分类专用 base_url（不填则复用第一个裁判的）
    classify_api_key_env: str | None = None  # 分类专用 api_key 环境变量名（不填则复用第一个裁判的）

    def effective_providers(self) -> list[str]:
        """合并 search_providers + search_provider（后者可为 str 或 list），去重保序。"""
        out = list(self.search_providers or [])
        sp = self.search_provider
        if sp:
            for p in ([sp] if isinstance(sp, str) else sp):
                if p not in out:
                    out.append(p)
        return out


class EnsembleConfig(BaseModel):
    rubric: str = "trim_mean"  # trim_mean | mean
    correctness: str = "majority_vote"
    pairwise: str = "majority_vote"
    bootstrap_ci: bool = True
    n_bootstrap: int = 200
    flag_low_agreement: float = 0.6  # 一致率/稳定性低于此值 → 标红
    dim_problem_threshold: float = 2.0  # 维度分<=此值视为"问题"（按垂域维度问题分布用，满分通常5）


class OperationIssueType(BaseModel):
    """任务类问题类型；与整体 correctness 解耦。"""

    allowed_correctness: list[str]
    description: str

    @model_validator(mode="after")
    def validate_allowed_correctness(self):
        expected = {"ok", "nok", "no_support", "others"}
        allowed = set(self.allowed_correctness)
        if not allowed or not allowed <= expected:
            raise ValueError(
                "operation_policy.issue_types.allowed_correctness "
                "必须是 ok/nok/no_support/others 的非空子集"
            )
        self.allowed_correctness = list(dict.fromkeys(self.allowed_correctness))
        if not self.description.strip():
            raise ValueError("operation_policy.issue_types.description 不能为空")
        return self


class OperationRouteType(BaseModel):
    """一种可从任务类录屏中观察到的执行链路。"""

    name: str
    description: str
    positive_cues: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_route_type(self):
        self.name = self.name.strip()
        self.description = self.description.strip()
        if not self.name or not self.description:
            raise ValueError("operation_policy.route_policy.routes 的 name/description 不能为空")
        if not self.positive_cues:
            raise ValueError("operation_policy.route_policy.routes.positive_cues 不能为空")
        return self


class OperationRoutePolicy(BaseModel):
    """任务类执行链路的视觉识别政策，与 correctness 独立。"""

    routes: dict[str, OperationRouteType] = Field(default_factory=dict)
    rules: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_routes(self):
        expected = {"fast_system", "skill", "jarvis", "other"}
        if set(self.routes) != expected:
            raise ValueError(
                "operation_policy.route_policy.routes 必须完整定义 "
                "fast_system/skill/jarvis/other"
            )
        if not self.rules:
            raise ValueError("operation_policy.route_policy.rules 不能为空")
        return self


class OperationPolicy(BaseModel):
    """任务类专属判定政策；仅由 operation skill 渲染进视觉裁判 Prompt。"""

    prior_knowledge: list[str] = Field(default_factory=list)
    scope_rules: list[str] = Field(default_factory=list)
    query_image_rules: list[str] = Field(default_factory=list)
    evidence_rules: list[str] = Field(default_factory=list)
    correctness: dict[str, str] = Field(default_factory=dict)
    issue_types: dict[str, OperationIssueType] = Field(default_factory=dict)
    decision_order: list[str] = Field(default_factory=list)
    conditional_rules: list[str] = Field(default_factory=list)
    low_level_rules: list[str] = Field(default_factory=list)
    route_policy: OperationRoutePolicy | None = None

    @model_validator(mode="after")
    def validate_status_keys(self):
        expected = {"ok", "nok", "no_support", "others"}
        if set(self.correctness) != expected:
            raise ValueError("operation_policy.correctness 必须完整定义 ok/nok/no_support/others")
        if not self.issue_types:
            raise ValueError("operation_policy.issue_types 不能为空")
        covered = {
            correctness
            for issue in self.issue_types.values()
            for correctness in issue.allowed_correctness
        }
        if covered != expected:
            raise ValueError("operation_policy.issue_types 必须覆盖 ok/nok/no_support/others")
        return self


class ExpertKnowledgeCategory(BaseModel):
    """一组可由人工逐条维护的专家经验。"""

    key: str
    name: str
    description: str = ""
    rules: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_category(self):
        self.key = self.key.strip()
        self.name = self.name.strip()
        self.description = self.description.strip()
        self.rules = [str(rule).strip() for rule in self.rules if str(rule).strip()]
        if not self.key or not self.name:
            raise ValueError("专家经验类别的 key 和 name 不能为空")
        if not self.rules:
            raise ValueError(f"专家经验类别[{self.key}]至少需要一条规则")
        return self


class ExpertKnowledgeBase(BaseModel):
    """按类别组织的轻量专家经验库；只保存事实，不保存评测标签。"""

    name: str
    description: str = ""
    version: int = 1
    categories: list[ExpertKnowledgeCategory] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_knowledge_base(self):
        self.name = self.name.strip()
        self.description = self.description.strip()
        if not self.name:
            raise ValueError("专家经验库 name 不能为空")
        if self.version < 1:
            raise ValueError("专家经验库 version 必须大于等于 1")
        keys = [category.key for category in self.categories]
        if len(keys) != len(set(keys)):
            raise ValueError("专家经验类别 key 不能重复")
        if not self.categories:
            raise ValueError("专家经验库至少需要一个类别")
        return self


class DomainSkill(BaseModel):
    name: str = ""
    display: str = ""  # 分类候选展示名（如中文），缺失回落 name；不参与分类的 Skill（default）可留空
    matching_categories: list[str] = Field(default_factory=list)
    rubrics: list[RubricDim] = Field(default_factory=list)  # 该 Skill 自带的一级+二级维度
    rules: str = ""
    examples: list[str] = Field(default_factory=list)
    operation_policy: OperationPolicy | None = None


class VisualExtractionConfig(BaseModel):
    """视频视觉评估模式的抽帧与图片编码参数。"""

    algorithm_version: str
    default_start_time: float = 0.0
    max_frames: int = 20
    sample_fps: float = 1.5
    scene_threshold: float = 0.03
    scene_min_gap_s: float = 0.5
    state_layout_threshold: float = 0.025
    stable_min_duration_s: float = 0.8
    max_edge: int = 1280
    jpeg_quality: int = 85


class VisualModeProfile(BaseModel):
    """独立于垂域分类的视频视觉评估配置。"""

    name: str = ""
    display: str = ""
    card_types: dict[str, str] = Field(default_factory=dict)
    suitability_anchors: dict[int, str] = Field(default_factory=dict)
    extraction: VisualExtractionConfig


class AppConfig(BaseModel):
    models: list[ModelConfig]
    judges: list[JudgeConfig]
    rubrics: list[RubricDim]
    process_rubrics: list[RubricDim] = Field(default_factory=list)  # 过程盲评维度
    domain_skills: dict[str, DomainSkill] = Field(default_factory=dict)  # 垂域 Skill
    expert_knowledge: dict[str, ExpertKnowledgeBase] = Field(default_factory=dict)
    visual_modes: dict[str, VisualModeProfile] = Field(default_factory=dict)
    eval_options: EvalOptions = Field(default_factory=EvalOptions)
    ensemble: EnsembleConfig = Field(default_factory=EnsembleConfig)

    def model_names(self) -> list[str]:
        return [m.name for m in self.models]

    def judge_names(self) -> list[str]:
        return [j.name for j in self.judges]


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_rubrics_list(raw_list):
    out = []
    for source in (raw_list or []):
        raw = dict(source)
        subs_raw = raw.pop("sub_dimensions", None)
        subs = [SubDim(**s) for s in (subs_raw or [])]
        out.append(RubricDim(**raw, sub_dimensions=subs))
    return out


def _load_skills(config_dir):
    skills_dir = Path(config_dir) / "skills"
    if not skills_dir.is_dir():
        return {}
    skills = {}
    for f in sorted(skills_dir.glob("*.yaml")):
        data = _read_yaml(f)
        rubrics = _parse_rubrics_list(data.pop("rubrics", []))
        name = data.pop("name", f.stem)
        skills[name] = DomainSkill(name=name, rubrics=rubrics, **data)
    return skills


def _load_visual_modes(config_dir):
    profiles_dir = Path(config_dir) / "visual_modes"
    if not profiles_dir.is_dir():
        return {}
    profiles = {}
    for f in sorted(profiles_dir.glob("*.yaml")):
        data = dict(_read_yaml(f) or {})
        name = data.pop("name", f.stem)
        profiles[name] = VisualModeProfile(name=name, **data)
    return profiles


def _load_expert_knowledge(config_dir):
    knowledge_dir = Path(config_dir) / "knowledge"
    if not knowledge_dir.is_dir():
        return {}
    knowledge = {}
    for path in sorted(knowledge_dir.glob("*.yaml")):
        data = dict(_read_yaml(path) or {})
        knowledge[path.stem] = ExpertKnowledgeBase(**data)
    return knowledge


def load_config(config_dir: str | Path) -> AppConfig:
    """读取 config_dir 下的 models/judges/rubrics.yaml（eval_options/ensemble 内联在 judges.yaml）。"""
    config_dir = Path(config_dir)
    models_data = _read_yaml(config_dir / "models.yaml") or {}
    judges_data = _read_yaml(config_dir / "judges.yaml") or {}
    rubrics_data = _read_yaml(config_dir / "rubrics.yaml") or {}

    models = [ModelConfig(**m) for m in (models_data.get("models") or [])]
    judges = [JudgeConfig(**j) for j in (judges_data.get("judges") or [])]
    def _parse_rubrics(data):
        out = []
        for source in (data or []):
            raw = dict(source)
            subs_raw = raw.pop("sub_dimensions", None)
            subs = [SubDim(**s) for s in (subs_raw or [])]
            out.append(RubricDim(**raw, sub_dimensions=subs))
        return out

    rubrics = _parse_rubrics(rubrics_data.get("rubrics"))
    process_rubrics = _parse_rubrics(rubrics_data.get("process_rubrics"))
    domain_skills = _load_skills(config_dir)
    expert_knowledge = _load_expert_knowledge(config_dir)
    visual_modes = _load_visual_modes(config_dir)
    eval_options = EvalOptions(**(judges_data.get("eval_options") or {}))
    ensemble = EnsembleConfig(**(judges_data.get("ensemble") or {}))
    return AppConfig(
        models=models,
        judges=judges,
        rubrics=rubrics,
        process_rubrics=process_rubrics,
        domain_skills=domain_skills,
        expert_knowledge=expert_knowledge,
        visual_modes=visual_modes,
        eval_options=eval_options,
        ensemble=ensemble,
    )
