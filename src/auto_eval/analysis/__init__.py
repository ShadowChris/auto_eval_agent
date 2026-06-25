"""分析层：聚合 / 对比 / case 挖掘 / 优化建议 / 逐条诊断 / 错因聚类。"""
from .advisor import advise, weaknesses
from .aggregate import model_overview, overview_by_slice, per_model
from .cases import disagreement_cases, focal_vs_competitors
from .cluster import cluster_weaknesses, normalize_error_type
from .compare import dim_gap, pairwise_by_category, pairwise_winrate
from .per_case import build_case_rows, cases_to_csv

__all__ = [
    "model_overview",
    "overview_by_slice",
    "per_model",
    "pairwise_winrate",
    "pairwise_by_category",
    "dim_gap",
    "focal_vs_competitors",
    "disagreement_cases",
    "weaknesses",
    "advise",
    "build_case_rows",
    "cases_to_csv",
    "cluster_weaknesses",
    "normalize_error_type",
]
