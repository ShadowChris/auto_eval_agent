from auto_eval.analysis.operation_statistics import summarize_operation_results


def test_operation_statistics_uses_valid_results_as_rate_denominator() -> None:
    statistics = summarize_operation_results(
        [
            {"correctness": "ok", "issue_types": ["路径冗余", "路径冗余"]},
            {"correctness": "nok", "issue_types": ["未展示可验证结果", "路径冗余"]},
            {"correctness": "no_support", "issue_types": ["缺少必要外部条件"]},
            {"correctness": "others", "issue_types": ["未预期场景"]},
            {"error": "provider failed"},
        ],
        total_cases=6,
    )

    assert statistics["total_cases"] == 6
    assert statistics["valid_count"] == 4
    assert statistics["failed_count"] == 1
    assert statistics["pending_count"] == 1
    assert statistics["coverage_rate"] == 0.6667
    assert statistics["ok_rate_denominator"] == 4
    assert statistics["ok_rate"] == 0.25
    assert statistics["correctness_rows"] == [
        {"correctness": "ok", "count": 1, "rate": 0.25},
        {"correctness": "nok", "count": 1, "rate": 0.25},
        {"correctness": "no_support", "count": 1, "rate": 0.25},
        {"correctness": "others", "count": 1, "rate": 0.25},
    ]
    assert "1 条评估失败，已从有效评估数据及相关指标计算中排除" in statistics["conclusion"]


def test_operation_statistics_deduplicates_and_sorts_issue_types() -> None:
    statistics = summarize_operation_results([
        {"correctness": "ok", "issue_types": ["路径冗余", "路径冗余"]},
        {"correctness": "nok", "issue_types": ["未展示可验证结果", "路径冗余"]},
        {"correctness": "nok", "issue_types": "未展示可验证结果；文字回复严重异常"},
    ])

    assert statistics["issue_case_count"] == 3
    assert statistics["issue_type_rows"] == [
        {
            "issue_type": "未展示可验证结果",
            "case_count": 2,
            "rate": 0.6667,
        },
        {
            "issue_type": "路径冗余",
            "case_count": 2,
            "rate": 0.6667,
        },
        {
            "issue_type": "文字回复严重异常",
            "case_count": 1,
            "rate": 0.3333,
        },
    ]
    assert "OK 率 33.33%" in statistics["conclusion"]
    assert "最高频问题为“未展示可验证结果”" in statistics["conclusion"]


def test_operation_statistics_handles_no_valid_results() -> None:
    statistics = summarize_operation_results(
        [{"error": "timeout"}],
        total_cases=2,
    )

    assert statistics["valid_count"] == 0
    assert statistics["ok_rate"] is None
    assert statistics["failed_count"] == 1
    assert statistics["pending_count"] == 1
    assert "暂无有效判定" in statistics["conclusion"]


def test_operation_statistics_includes_all_valid_results_in_ok_rate() -> None:
    statistics = summarize_operation_results([
        {"correctness": "ok", "issue_types": []},
        {"correctness": "nok", "issue_types": ["应执行目标未执行"]},
        {"correctness": "no_support", "issue_types": ["缺少必要外部条件"]},
        {"correctness": "others", "issue_types": ["未预期场景"]},
    ])

    assert statistics["valid_count"] == 4
    assert statistics["ok_rate_denominator"] == 4
    assert statistics["ok_rate"] == 0.25
    assert statistics["correctness_rows"][0]["rate"] == 0.25
