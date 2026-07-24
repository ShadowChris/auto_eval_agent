# 垂域挂卡 / Superlink 视觉评估

`mode=rich_content` 用于检查问答产品录屏中 assistant 回答区域的结构化挂卡和蓝色
Superlink 文字。该模式复用操作类的视频路径校验、延迟抽帧、Web 会话目录、进度、
历史和 `judge_calls`，但使用独立抽帧参数、视觉提示词和结果结构。

## 1. 评估口径

- 普通内嵌图片、正文截图和纯文本段落不算挂卡。
- assistant 当前回答区域中的蓝色文字按可点击 Superlink 统计。
- 同一挂卡或链接跨帧重复出现只计一次；同样文字位于回答的两个不同位置时计两次。
- 没有挂卡或 Superlink 只表示观察结果，不自动视为回答错误。
- 仅对实际出现的挂卡评价垂域、实体、时间地点等条件及正文一致性。
- 模式不输出问答类 `correctness`，Web 汇总使用挂卡出现率、挂卡合适率、Superlink
  出现率和待人工复核数。

## 2. JSONL 输入

```json
{"id":"rich_001","query":"北京明天天气怎么样","context":"当前地点北京","video_path":"data/rich_content/weather.mp4","category":"weather","answer_text":"北京明天晴","content_start_time":0,"content_end_time":18.5}
```

| 字段 | 必填 | 说明 |
|---|---:|---|
| `query` | 是 | 用户问题 |
| `video_path` | 是 | 本地视频路径，相对路径以项目根目录为基准 |
| `id` | 否 | 稳定题号 |
| `context` | 否 | 时间、地点等可信背景 |
| `category` | 否 | 实际业务垂域；为空时显示为通用，不额外调用分类模型 |
| `answer_text` | 否 | 只辅助理解语义，不能证明视觉对象存在 |
| `content_start_time` | 否 | 回答内容开始时间，单位秒，默认 0 |
| `content_end_time` | 否 | 回答内容结束时间，单位秒 |
| `expected_visual` | 否 | mini/元评测标注，不会进入视觉裁判 Prompt |

批量导入只解析和展示数据；点击“开始评估”后才校验视频、抽帧和调用视觉裁判。

## 3. 主要输出

```json
{
  "answer_coverage": "complete",
  "card_presence": "present",
  "card_count": 1,
  "card_types": ["weather"],
  "card_contents": ["晴，26～34℃"],
  "card_suitability": "suitable",
  "card_suitability_score": 5,
  "superlink_presence": "present",
  "superlink_count": 2,
  "superlink_count_type": "exact",
  "superlink_texts": ["逐小时预报", "未来15天天气"],
  "needs_review": false
}
```

完整逐对象证据位于 `visual_findings.cards` 和 `visual_findings.superlinks`，包括核心实体、
回答位置、证据帧和置信度。回答未完整覆盖时，已观察到的链接数量使用 `lower_bound`；
没有观察到链接时数量为 `null`、计数类型为 `unknown`。

## 4. 配置与代码

- `config/visual_modes/rich_content.yaml`：挂卡类型、适配性锚点和抽帧参数。
- `src/auto_eval/judges/rich_content_judge.py`：强类型解析与稳定结果字段。
- `src/auto_eval/judges/prompts.py`：视觉识别与跨帧去重规则。
- `src/auto_eval/web/operation_media.py`：复用视频路径、缓存和会话抽帧基础设施。
- `data/test_operation_fast_query_mini.jsonl`：当前复用的本地 mini 回测集。

当前抽帧版本为 `rich-content-v1.0.0`，默认以 1.5 FPS 生成候选，最多保留 16 帧；
视觉输入使用最长边 1280、JPEG 质量 85，以保留小号蓝色文字。
