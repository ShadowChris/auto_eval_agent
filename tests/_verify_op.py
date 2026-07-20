"""操作类评测改动综合自检：import 链 / schema / 模板 / runner 装配 / parse 防御 / multipart / 抽帧算法。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 1) import 链：所有改动模块能加载
from auto_eval.media import select_keyframe_times, probe_duration, extract_scene_keyframes, encode_frame
from auto_eval.schema import EvalItem
from auto_eval.judges.prompts import OPERATION_SYSTEM, OPERATION_USER
from auto_eval.judges.rubric_judge import RubricJudge
from auto_eval.web.parse_input import parse_text, parse_jsonl
from auto_eval.web.runner import _to_evalitem
print("[1] import 链 OK")

# 2) EvalItem 新增 media 字段
it = EvalItem(id="t", question="q", media=["x.mp4"], metadata={"frames": ["a.jpg"]})
assert it.media == ["x.mp4"], it.media
print("[2] schema.media OK")

# 3) operation.yaml 已加载为 skill + OPERATION_SYSTEM 动态渲染维度（从 yaml 读，不再硬编码）
from pathlib import Path
from auto_eval.config import load_config
cfg = load_config(Path(r"d:\workspace\quick_test\auto_eval_agent\config"))
op_skill = cfg.domain_skills.get("operation")
assert op_skill and op_skill.rubrics, "operation skill 未加载（检查 config/skills/operation.yaml）"
op_dims = op_skill.rubrics
print("[3a] operation.yaml 加载 OK, dims=", [d.name for d in op_dims], "weights=", [d.weight for d in op_dims])
s = OPERATION_SYSTEM.render(persona="评测员", agent_claim="我已设好闹钟", dims=op_dims, scale=op_dims[0].scale)
assert "操作完成度" in s and "我已设好闹钟" in s, "模板渲染异常"
assert all(d.name in s for d in op_dims), "维度未全部渲染进 prompt"
u = OPERATION_USER.render(question="set alarm", current_date="2026年7月")
assert "set alarm" in u
print("[3b] OPERATION_SYSTEM 动态渲染 OK")

# 4) _to_evalitem 透传 media / frames→metadata
ei = _to_evalitem({"query": "设闹钟", "media": ["v.mp4"], "frames": ["f1.jpg", "f2.jpg"]}, 3)
assert ei.media == ["v.mp4"] and ei.metadata.get("frames") == ["f1.jpg", "f2.jpg"], (ei.media, ei.metadata)
print("[4] _to_evalitem OK")

# 5) operation 文本仍拦截，JSONL 支持批量清单
items, errs = parse_text("任意", "operation")
assert items == [] and errs
items2, errs2 = parse_jsonl('{"question":"q","video_path":"data/q.mp4"}', "operation")
assert len(items2) == 1 and not errs2 and items2[0]["video_path"] == "data/q.mp4"
print("[5] operation JSONL 解析 OK")

# 6) python-multipart（UploadFile 需要）
try:
    import multipart  # noqa
    print("[6] python-multipart: 已安装")
except ImportError:
    print("[6] python-multipart: ⚠️ 缺失（/api/upload/video 的 UploadFile 需要，pip install python-multipart）")

# 7) 抽帧算法（场景检测+去重+终态保底）对真实闹钟录屏
video = r"d:\workspace\quick_test\auto_eval_agent\data\闹钟操作录频.mp4"
dur = probe_duration(video)
times = select_keyframe_times(video)
print(f"[7] duration={dur:.2f}s  抽帧数={len(times)}  时刻={[round(t,2) for t in times]}")
assert 4 <= len(times) <= 12, f"帧数异常: {len(times)}"
assert any(t < 1.0 for t in times), "未覆盖发送瞬间(~0.45s)"
assert any(abs(t - dur) < 0.6 for t in times), "未含终态帧"
static = [t for t in times if 8 < t < 14]  # 8-14s 是纯静止段
print(f"    静止段(8-14s)内帧数={len(static)}（期望接近 0，等间隔会在这里浪费 2 帧）")
print("[7] 抽帧单测 OK")

print("\n全部自检通过 ✅")
