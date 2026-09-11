"""分析层：统计与批次对比纯函数（自 dev_czm 同步的报告口径）。

本层专注于 Web 报告/对比分析消费的纯计算，不依赖旧离线分析链
（advisor/aggregate/cases 等仅被 cli.py 引用的历史模块）。
"""
from .operation_comparison import compare_operation_batches
from .operation_report import build_comparison_report, build_single_report
from .operation_statistics import summarize_operation_results

__all__ = [
    "compare_operation_batches",
    "build_comparison_report",
    "build_single_report",
    "summarize_operation_results",
]