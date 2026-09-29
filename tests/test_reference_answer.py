"""竞品答案（competitor_answer）→ 事实冲突（factual_conflict）维度。

覆盖：CSV/JSONL 导入读取（含旧键 reference_answer/「事实参考答案」兼容）、裁判侧
稀疏输出（仅冲突时 yes）、未提供竞品答案强制省略（防幻觉）、结果层归一为密集
yes/no、导出列 是/否 翻译、汇总计数、前端隐形往返与静态断言；冲突内容
（factual_conflict_detail）与判定绑定输出。
"""
import json
from pathlib import Path

from auto_eval.config import (
    JudgeConfig,
    VisualExtractionConfig,
    VisualModeProfile,
)
from auto_eval.judges.prompts import RICH_CONTENT_SYSTEM, RICH_CONTENT_USER
from auto_eval.judges.rich_content_judge import (
    RichContentJudge,
    rich_content_result_fields,
)
from auto_eval.schema import RichContentObservation
from auto_eval.web.history import _rich_content_export_rows
from auto_eval.web.parse_input import parse_csv, parse_jsonl
from auto_eval.web.runner import _summarize_rich_content
from auto_eval.web.tasks import Task

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_JS = PROJECT_ROOT / "src/auto_eval/web/static/app.js"
INDEX_HTML = PROJECT_ROOT / "src/auto_eval/web/static/index.html"


def _csv(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


# ---------- parse_csv：「竞品答案」列 ----------

def test_parse_csv_reads_competitor_answer_column():
    content = _csv([
        "query,is_start,is_end,video_path,竞品答案",
        "第一轮,true,true,/tmp/a.mp4,  竞品A  ",
    ])
    items, errors = parse_csv(content, "rich_content")
    assert not errors
    assert items[0]["competitor_answer"] == "竞品A"  # _csv_clean 已 trim


def test_parse_csv_empty_or_placeholder_omits_key():
    content = _csv([
        "query,is_start,is_end,video_path,竞品答案",
        "第一轮,true,false,/tmp/a.mp4,",
        "第二轮,false,true,,N/A",
    ])
    items, errors = parse_csv(content, "rich_content")
    assert not errors
    assert "competitor_answer" not in items[0]
    assert "competitor_answer" not in items[1]


def test_parse_csv_column_missing_no_error():
    content = _csv([
        "query,is_start,is_end,video_path",
        "第一轮,true,true,/tmp/a.mp4",
    ])
    items, errors = parse_csv(content, "rich_content")
    assert not errors
    assert "competitor_answer" not in items[0]


def test_parse_csv_header_priority_and_backward_compat():
    # 仅英文列头 competitor_answer → 命中
    content = _csv([
        "query,is_start,is_end,video_path,competitor_answer",
        "第一轮,true,true,/tmp/a.mp4,EN-NEW",
    ])
    items, _ = parse_csv(content, "rich_content")
    assert items[0]["competitor_answer"] == "EN-NEW"
    # 双列并存 → 新中文列「竞品答案」优先
    content = _csv([
        "query,is_start,is_end,video_path,competitor_answer,竞品答案",
        "第一轮,true,true,/tmp/a.mp4,EN-NEW,CN-NEW",
    ])
    items, _ = parse_csv(content, "rich_content")
    assert items[0]["competitor_answer"] == "CN-NEW"
    # 旧英文列头 reference_answer → 回退兼容
    content = _csv([
        "query,is_start,is_end,video_path,reference_answer",
        "第一轮,true,true,/tmp/a.mp4,EN-OLD",
    ])
    items, _ = parse_csv(content, "rich_content")
    assert items[0]["competitor_answer"] == "EN-OLD"
    # 旧中文列「事实参考答案」→ 回退兼容；并与新列并存时新列优先
    content = _csv([
        "query,is_start,is_end,video_path,事实参考答案,竞品答案",
        "第一轮,true,true,/tmp/a.mp4,CN-OLD,CN-NEW",
    ])
    items, _ = parse_csv(content, "rich_content")
    assert items[0]["competitor_answer"] == "CN-NEW"


def test_parse_csv_source_data_keeps_raw_row():
    content = _csv([
        "query,is_start,is_end,video_path,竞品答案,分享链接",
        "第一轮,true,true,/tmp/a.mp4,竞品A,https://x",
    ])
    items, _ = parse_csv(content, "rich_content")
    assert items[0]["source_data"]["竞品答案"] == "竞品A"
    assert items[0]["source_data"]["分享链接"] == "https://x"


# ---------- parse_csv：组边界归一化（is_start/is_end 一致性） ----------


def _group_sizes(items):
    groups: list[list[int]] = []
    for it in items:
        if it["turn_index"] == 0:
            groups.append([])
        groups[-1].append(it["turn_index"])
    return groups


def test_parse_csv_group_boundary_missing_is_end():
    # 组尾缺 is_end：倒数第二条 (False,False)，下一条 is_start=True 开启新组
    # → 倒数第二条应强制为组尾，第三行起为新组。
    content = _csv([
        "query,is_start,is_end,video_path",
        "1,true,false,/tmp/a.mp4",
        "2,false,false,/tmp/a.mp4",
        "3,true,true,/tmp/b.mp4",
    ])
    items, errors = parse_csv(content, "rich_content")
    assert not errors
    assert _group_sizes(items) == [[0, 1], [0]]
    assert items[1]["turn_index"] == 1
    assert items[2]["turn_index"] == 0
    assert items[2]["session_group"] != items[1]["session_group"]


def test_parse_csv_group_boundary_missing_is_start():
    # 组头缺 is_start：上一条 (False,True) 已是组尾，下一条 is_start 非 True
    # → 下一条应强制为新组开头（turn_index=0）。
    content = _csv([
        "query,is_start,is_end,video_path",
        "1,true,false,/tmp/a.mp4",
        "2,false,true,/tmp/a.mp4",
        "3,false,true,/tmp/b.mp4",
    ])
    items, errors = parse_csv(content, "rich_content")
    assert not errors
    assert _group_sizes(items) == [[0, 1], [0]]
    assert items[2]["turn_index"] == 0
    assert items[2]["session_group"] != items[1]["session_group"]


def test_parse_csv_group_boundary_wellformed_unchanged():
    # 正常组边界 (True,False)→(False,True) 不被拆开，也不产生新组。
    content = _csv([
        "query,is_start,is_end,video_path",
        "1,true,false,/tmp/a.mp4",
        "2,false,true,/tmp/a.mp4",
        "3,true,false,/tmp/b.mp4",
        "4,false,true,/tmp/b.mp4",
    ])
    items, errors = parse_csv(content, "rich_content")
    assert not errors
    assert _group_sizes(items) == [[0, 1], [0, 1]]
    assert [it["turn_index"] for it in items] == [0, 1, 0, 1]


# ---------- parse_csv：「不评测」列 ----------


def test_parse_csv_not_eval_column_missing_or_empty_evaluates():
    # 列缺失 → 全部评测
    items, errors = parse_csv(_csv([
        "query,is_start,is_end,video_path",
        "1,true,true,/tmp/a.mp4",
    ]), "rich_content")
    assert not errors
    assert len(items) == 1
    # 列存在但留空 → 仍评测
    items, errors = parse_csv(_csv([
        "query,is_start,is_end,video_path,不评测",
        "1,true,false,/tmp/a.mp4,",
        "2,false,true,/tmp/a.mp4,",
    ]), "rich_content")
    assert not errors
    assert len(items) == 2
    assert _group_sizes(items) == [[0, 1]]


def test_parse_csv_not_eval_skips_middle_row():
    # 组中一条为 True → 该行不产生 item，其余分组/turn_index 正常。
    content = _csv([
        "query,is_start,is_end,video_path,不评测",
        "1,true,false,/tmp/a.mp4,",
        "2,false,false,/tmp/a.mp4,true",
        "3,false,true,/tmp/a.mp4,",
    ])
    items, errors = parse_csv(content, "rich_content")
    assert not errors
    assert [it["turn_index"] for it in items] == [0, 1]
    assert len(items) == 2
    assert items[0]["query"] == "1" and items[1]["query"] == "3"


def test_parse_csv_not_eval_last_row_previous_becomes_last():
    # 组尾为 True → 跳过，上一条成为该组最后一条。
    content = _csv([
        "query,is_start,is_end,video_path,不评测",
        "1,true,false,/tmp/a.mp4,",
        "2,false,true,/tmp/a.mp4,true",
    ])
    items, errors = parse_csv(content, "rich_content")
    assert not errors
    assert len(items) == 1
    assert _group_sizes(items) == [[0]]
    assert items[0]["query"] == "1"
    assert items[0]["turn_index"] == 0


def test_parse_csv_not_eval_first_row_group_still_forms():
    # 组首为 True → 跳过，组仍成立，下一条成为新组首条。
    content = _csv([
        "query,is_start,is_end,video_path,不评测",
        "1,true,false,/tmp/a.mp4,true",
        "2,false,true,/tmp/a.mp4,",
    ])
    items, errors = parse_csv(content, "rich_content")
    assert not errors
    assert len(items) == 1
    assert _group_sizes(items) == [[0]]
    assert items[0]["query"] == "2"
    assert items[0]["turn_index"] == 0


# ---------- parse_jsonl：competitor_answer 字段 ----------

def test_parse_jsonl_competitor_answer_variants():
    # 正常字符串（strip 后落键）
    items, errors = parse_jsonl(
        json.dumps({"query": "q1", "video_path": "/tmp/a.mp4", "competitor_answer": "  竞品A  "}) + "\n",
        "rich_content",
    )
    assert not errors
    assert items[0]["competitor_answer"] == "竞品A"

    # 空白串 → 静默丢弃，无 error
    items, errors = parse_jsonl(
        json.dumps({"query": "q1", "video_path": "/tmp/a.mp4", "competitor_answer": "   "}) + "\n",
        "rich_content",
    )
    assert not errors
    assert "competitor_answer" not in items[0]

    # 非字符串 → error 跳行
    items, errors = parse_jsonl(
        json.dumps({"query": "q1", "video_path": "/tmp/a.mp4", "competitor_answer": 123}) + "\n",
        "rich_content",
    )
    assert len(errors) == 1
    assert "competitor_answer 必须是字符串" in errors[0]
    assert items == []

    # 兼容旧键 reference_answer → 读入 competitor_answer
    items, errors = parse_jsonl(
        json.dumps({"query": "q1", "video_path": "/tmp/a.mp4", "reference_answer": "旧键A"}) + "\n",
        "rich_content",
    )
    assert not errors
    assert items[0]["competitor_answer"] == "旧键A"
    # 新旧键并存 → 新键优先
    items, errors = parse_jsonl(
        json.dumps({"query": "q1", "video_path": "/tmp/a.mp4",
                    "reference_answer": "旧键A", "competitor_answer": "新键A"}) + "\n",
        "rich_content",
    )
    assert not errors
    assert items[0]["competitor_answer"] == "新键A"

    # 中文键不读（JSONL 键域全英文），但原始数据保留在 source_data
    items, errors = parse_jsonl(
        json.dumps({"query": "q1", "video_path": "/tmp/a.mp4", "竞品答案": "竞品A"}) + "\n",
        "rich_content",
    )
    assert not errors
    assert "competitor_answer" not in items[0]
    assert items[0]["source_data"]["竞品答案"] == "竞品A"


# ---------- rich_content_result_fields：稀疏 → 密集归一 ----------

def test_result_fields_normalizes_sparse_conflict():
    # 省略（默认空）→ no
    assert rich_content_result_fields(RichContentObservation())["factual_conflict"] == "no"
    # yes 及其变体 → yes
    for raw in ("yes", "Yes", "是", "有冲突"):
        obs = RichContentObservation(factual_conflict=raw)
        assert rich_content_result_fields(obs)["factual_conflict"] == "yes", raw
    # no/unclear/垃圾 → no（保守二元）
    for raw in ("no", "No", "否", "unclear", "乱码"):
        obs = RichContentObservation(factual_conflict=raw)
        assert rich_content_result_fields(obs)["factual_conflict"] == "no", raw


def test_result_fields_detail_bound_to_conflict():
    # yes + 内容 → 保留（strip）
    obs = RichContentObservation(
        factual_conflict="yes", factual_conflict_detail="  产品答25℃，竞品答15℃  "
    )
    fields = rich_content_result_fields(obs)
    assert fields["factual_conflict"] == "yes"
    assert fields["factual_conflict_detail"] == "产品答25℃，竞品答15℃"
    # yes 但裁判漏了内容 → 空
    assert rich_content_result_fields(
        RichContentObservation(factual_conflict="yes")
    )["factual_conflict_detail"] == ""
    # 孤儿内容（判定 no 却输出内容）→ 清空：列只在「是」的行有值
    assert rich_content_result_fields(
        RichContentObservation(factual_conflict="no", factual_conflict_detail="某冲突")
    )["factual_conflict_detail"] == ""
    # 全缺省 → 空
    assert rich_content_result_fields(
        RichContentObservation()
    )["factual_conflict_detail"] == ""


# ---------- evaluate：未提供竞品答案强制省略（防幻觉） ----------

class _StubClient:
    """固定载荷假裁判：complete 直接返回 JSON 文本，不做修复；
    frames=[] 不触发真实抽帧。"""

    def __init__(self, payload: dict):
        self.cfg = JudgeConfig(name="stub", runner="openai_compat")
        self.model = "stub-model"
        self.persona = "test"
        self._payload = payload

    async def complete(self, system, user, stream_callback=None,
                       user_images=None, user_image_refs=None) -> str:
        return json.dumps(self._payload)

    async def repair_json(self, malformed_output: str, **kwargs) -> str:
        return malformed_output


def _judge(payload: dict) -> RichContentJudge:
    profile = VisualModeProfile(
        name="rich", extraction=VisualExtractionConfig(algorithm_version="test")
    )
    return RichContentJudge(_StubClient(payload), profile)


async def test_evaluate_forces_no_without_competitor_answer():
    """未提供竞品答案：裁判即使幻觉输出 yes 也归 no。"""
    result = await _judge({"factual_conflict": "yes"}).evaluate(
        question="q", context="", answer_text="", frames=[],
    )
    assert result["factual_conflict"] == "no"


async def test_evaluate_keeps_yes_with_competitor_answer():
    result = await _judge({"factual_conflict": "yes"}).evaluate(
        question="q", context="", answer_text="", frames=[],
        competitor_answer="竞品A",
    )
    assert result["factual_conflict"] == "yes"


async def test_evaluate_defaults_no_when_field_omitted():
    """提供了竞品答案但裁判省略该字段（稀疏契约/修复缺键）→ no。"""
    result = await _judge({}).evaluate(
        question="q", context="", answer_text="", frames=[],
        competitor_answer="竞品A",
    )
    assert result["factual_conflict"] == "no"


# ---------- evaluate：冲突内容（factual_conflict_detail）绑定 ----------

async def test_evaluate_forces_detail_empty_without_competitor_answer():
    """未提供竞品答案：裁判幻觉输出 yes+内容也一并归 no/空。"""
    result = await _judge({
        "factual_conflict": "yes",
        "factual_conflict_detail": "产品答A，竞品答B",
    }).evaluate(question="q", context="", answer_text="", frames=[])
    assert result["factual_conflict"] == "no"
    assert result["factual_conflict_detail"] == ""


async def test_evaluate_keeps_detail_with_competitor_answer():
    result = await _judge({
        "factual_conflict": "yes",
        "factual_conflict_detail": "产品答A，竞品答B",
    }).evaluate(
        question="q", context="", answer_text="", frames=[],
        competitor_answer="竞品A",
    )
    assert result["factual_conflict_detail"] == "产品答A，竞品答B"
    # 判定 yes 但漏了内容 → 空（不补造）
    result = await _judge({"factual_conflict": "yes"}).evaluate(
        question="q", context="", answer_text="", frames=[],
        competitor_answer="竞品A",
    )
    assert result["factual_conflict_detail"] == ""
    # 孤儿内容（省略判定却输出内容）→ 归一清空
    result = await _judge({"factual_conflict_detail": "某冲突"}).evaluate(
        question="q", context="", answer_text="", frames=[],
        competitor_answer="竞品A",
    )
    assert result["factual_conflict"] == "no"
    assert result["factual_conflict_detail"] == ""


# ---------- prompt 契约 ----------

def test_user_template_competitor_block_is_conditional():
    with_ref = RICH_CONTENT_USER.render(
        question="q", context="", answer_text="", frame_count=1,
        competitor_answer="竞品A",
    )
    assert "竞品答案" in with_ref
    assert "竞品A" in with_ref
    without_ref = RICH_CONTENT_USER.render(
        question="q", context="", answer_text="", frame_count=1,
        competitor_answer="",
    )
    assert "竞品答案" not in without_ref


def test_system_template_declares_sparse_contract():
    rendered = RICH_CONTENT_SYSTEM.render(persona="p", card_types={})
    # JSON 输出键 + 稀疏契约（仅冲突输出 yes，其余省略）+ 纪律措辞 + 竞品答案去权威化
    assert '"factual_conflict"' in rendered
    assert "省略该字段（不输出）" in rendered
    assert "不得让它影响" in rendered
    assert "绝不判断谁对谁错" in rendered
    assert "竞品答案" in rendered
    assert "不代表标准答案" in rendered


def test_system_template_declares_detail_binding():
    rendered = RICH_CONTENT_SYSTEM.render(persona="p", card_types={})
    # JSON 输出键 + 与 yes 绑定（同时输出/同步省略）
    assert '"factual_conflict_detail"' in rendered
    assert "必须**同时**输出 factual_conflict_detail" in rendered
    assert "该字段同样**省略**" in rendered


# ---------- 导出与汇总 ----------

def test_export_rows_translate_correctness_column():
    """correctness 列的 problem_solved 翻译：need_review → no_support。"""
    rows = _rich_content_export_rows([
        {"item_id": "q1", "problem_solved": "ok"},
        {"item_id": "q2", "problem_solved": "nok"},
        {"item_id": "q3", "problem_solved": "need_review"},
        {"item_id": "q4"},  # 无键 → 空单元格
    ])
    assert [row["correctness"] for row in rows] == ["OK", "NOK", "no_support", ""]


def test_export_rows_translate_conflict_column():
    rows = _rich_content_export_rows([
        {"item_id": "q1", "factual_conflict": "yes"},
        {"item_id": "q2", "factual_conflict": "no"},
        {"item_id": "q3"},  # 旧任务结果行无该键 → 空单元格
    ])
    assert [row["事实冲突"] for row in rows] == ["是", "否", ""]


def test_export_rows_include_review_reason_column():
    # review_reason 导出列名为 need_review_cate
    rows = _rich_content_export_rows([
        {"item_id": "q1", "review_reason": "需要多轮确认"},
        {"item_id": "q2"},  # 无键 → 空单元格
    ])
    assert [row["need_review_cate"] for row in rows] == ["需要多轮确认", ""]


def test_export_rows_include_conflict_detail_column():
    # 行值取自 result_fields 归一输出（no 行 detail 已清空）
    rows = _rich_content_export_rows([
        {"item_id": "q1", "factual_conflict": "yes",
         "factual_conflict_detail": "产品答25℃，竞品答15℃"},
        {"item_id": "q2", "factual_conflict": "no"},
        {"item_id": "q3"},  # 旧任务结果行无该键 → 空单元格
    ])
    assert [row["冲突内容"] for row in rows] == ["产品答25℃，竞品答15℃", "", ""]


def test_summarize_counts_conflict_yes():
    task = Task(id="t", mode="rich_content", items=[], options={})
    task.results = [
        {"factual_conflict": "yes"},
        {"factual_conflict": "no"},
        {},  # 无键（无竞品答案的行归一为 no，不计）
        {"error": "boom", "factual_conflict": "yes"},  # 失败行不进 ok 统计
    ]
    assert _summarize_rich_content(task)["factual_conflict_yes"] == 1


# ---------- 前端静态断言 ----------

def test_frontend_competitor_answer_roundtrip_and_column():
    app_js = APP_JS.read_text(encoding="utf-8")
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    # 隐形往返三处：导入映射 → 表单模型 → 提交重组
    assert 'competitorAnswer: ""' in app_js
    assert "competitorAnswer: item.competitor_answer" in app_js
    assert 'item.competitor_answer = (it.competitorAnswer || "").trim();' in app_js
    # 结果列 + 单元格翻译 + 输入提示
    assert '{ key: "factual_conflict", label: "事实冲突" }' in app_js
    assert 'c.key === "factual_conflict"' in app_js
    assert "competitor_answer" in app_js
    # 居中列 + 汇总条
    assert "'factual_conflict'" in index_html
    assert "summary.factual_conflict_yes" in index_html
    # op 卡片区不加输入框（隐形透传，无 v-model）
    assert 'v-model="it.competitorAnswer"' not in index_html


def test_frontend_conflict_detail_column():
    app_js = APP_JS.read_text(encoding="utf-8")
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    # 结果列 + 长文本宽度档（同 problem_solved_reason）
    assert '{ key: "factual_conflict_detail", label: "冲突内容" }' in app_js
    assert '"problem_solved_reason", "factual_conflict_detail"]' in app_js
    # 长文本左对齐：不进居中 key 列表
    assert "'factual_conflict_detail'" not in index_html
