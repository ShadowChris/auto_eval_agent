"""配置加载：从 config/*.yaml 读取并校验为强类型配置对象。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


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
    model: str | None = None
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


class DomainSkill(BaseModel):
    name: str = ""
    display: str = ""  # 分类候选展示名（如中文），缺失回落 name；不参与分类的 Skill（default）可留空
    matching_categories: list[str] = Field(default_factory=list)
    rubrics: list[RubricDim] = Field(default_factory=list)  # 该 Skill 自带的一级+二级维度
    rules: str = ""
    examples: list[str] = Field(default_factory=list)


class VisualExtractionConfig(BaseModel):
    """视频视觉评估模式的抽帧与图片编码参数。"""

    algorithm_version: str
    default_start_time: float = 0.0
    max_frames: int = 16
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
    visual_modes = _load_visual_modes(config_dir)
    eval_options = EvalOptions(**(judges_data.get("eval_options") or {}))
    ensemble = EnsembleConfig(**(judges_data.get("ensemble") or {}))
    return AppConfig(
        models=models,
        judges=judges,
        rubrics=rubrics,
        process_rubrics=process_rubrics,
        domain_skills=domain_skills,
        visual_modes=visual_modes,
        eval_options=eval_options,
        ensemble=ensemble,
    )
