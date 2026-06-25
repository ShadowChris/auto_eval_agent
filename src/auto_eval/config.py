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
    sub_dimensions: list[SubDim] = Field(default_factory=list)  # 一级下有二级则渲染二级，裁判按二级评分  # 满分


class EvalOptions(BaseModel):
    repeat: int = 1  # 同裁判重复采样次数（算稳定性）
    pairwise_bidirectional: bool = True  # A/B 双向比较抗位置偏差
    independent_then_compare: bool = True  # 先独立盲评再成对比较
    pairwise_for_ref: bool = False  # 有参考答案题是否也做成对比较
    search_provider: str | None = None  # tavily | serpapi | bing
    search_topk: int = 3


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


class AppConfig(BaseModel):
    models: list[ModelConfig]
    judges: list[JudgeConfig]
    rubrics: list[RubricDim]
    process_rubrics: list[RubricDim] = Field(default_factory=list)  # 过程盲评维度
    domain_skills: dict[str, DomainSkill] = Field(default_factory=dict)  # 垂域 Skill
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
    for raw in (raw_list or []):
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
        for raw in (data or []):
            subs_raw = raw.pop("sub_dimensions", None)
            subs = [SubDim(**s) for s in (subs_raw or [])]
            out.append(RubricDim(**raw, sub_dimensions=subs))
        return out

    rubrics = _parse_rubrics(rubrics_data.get("rubrics"))
    process_rubrics = _parse_rubrics(rubrics_data.get("process_rubrics"))
    domain_skills = _load_skills(config_dir)
    eval_options = EvalOptions(**(judges_data.get("eval_options") or {}))
    ensemble = EnsembleConfig(**(judges_data.get("ensemble") or {}))
    return AppConfig(
        models=models,
        judges=judges,
        rubrics=rubrics,
        process_rubrics=process_rubrics,
        domain_skills=domain_skills,
        eval_options=eval_options,
        ensemble=ensemble,
    )
