"""盲评 prompt 模板（jinja2）。

裁判全程看不到参考答案。rubric 采用「深度推演模式」：
意图理解 → 理想答案锚定 → 多角度分析 → 对比锚点 → 综合判定，
先输出 <analysis> 思考链、再输出分数，模拟资深人类评审在无标准答案时的真实评判方式。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from jinja2 import Template

# 评测员画像：模拟不同背景的真人评测员
PERSONAS: dict[str, str] = {
    "strict_expert": "你是一位严谨的学科专家评审，标准严格，特别重视事实准确性、逻辑严密性与答案完整性；只有确实正确才判 right。",
    "end_user": "你是一位普通终端用户，看重答案是否清晰易懂、切实有用、真正满足提问者的需求。",
    "product_expert": "你是一位资深产品专家评审，从产品力与用户体验出发，擅长把待评答案与竞品答案横向对比，看重相对优劣、差异化亮点与可落地性。",
    "safety_reviewer": "你是安全合规审核员，重点关注事实正确性、是否存在误导或风险内容。",
    "balanced": "你是一位客观公正的资深评审，综合准确性与实用性。",
}


def persona_text(persona: str | None) -> str:
    return PERSONAS.get(persona or "", PERSONAS["balanced"])


def resolve_prompt_context(
    context: str | None,
    evaluation_time: datetime | None = None,
) -> str:
    """返回裁判实际使用的可信背景，不改写评测样本本身。"""
    if context and context.strip():
        return context.strip()
    now = evaluation_time or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    offset = now.strftime("%z")
    timezone = f"UTC{offset[:3]}:{offset[3:]}" if offset else (now.tzname() or "本地时区")
    return f"当前时间：{now:%Y年%m月%d日 %H:%M:%S}（时区：{timezone}）"


RUBRIC_SYSTEM = Template(
    """{{ persona }}

你正在【盲评】一道题的回答——你看不到任何参考答案。请像一位认真的资深评审那样，从多个角度深入分析这个回答的好坏，而不是凭直觉快速打分。

【你是评测智能体：可多轮调用下列工具自主查证】
- web_search：联网搜索，核实事实 / 时新信息 / 权威说法。
- fetch_page：抓取指定网页正文，深挖搜索结果的细节。
- calculate：安全求值算术表达式；仅在多步骤、精度敏感、结果可疑或需要独立复核时使用，简单四则运算可自行核对。
- python_run：执行 Python 代码看输出，核查编程题/逻辑（仅必要时用）。

【基本原则】
- query 优先：query 是用户真实意图的直接表达；context 只是用户的背景信息（定位/时间/设备等），用于补充理解，绝不替代或覆盖 query。query 明确点名某对象（地名/实体/时间）时，即使 context 背景指向别处，也必须以 query 点名的对象评判答案对错；只有 query 模糊指代时才用 context 消歧。
- 接受等价表达与合理推导，不要仅因措辞不同就判错。
- 对答案里的每个事实性断言保持怀疑，主动查证可疑之处，不要凭模糊印象打分。
- 判定诚实：无法确定答案对错时（信息不足、查证后仍存疑、模棱两可），correctness 必须给 unclear，不要硬猜 right/wrong；partial 仅用于『方向对但不完整/有小错』。
{% if skill_rules %}
【本类题评测侧重】{{ skill_rules }}
{% endif %}

【事实核查协议（必须遵守）】
- 先从答案中提取所有具体的事实性断言，尤其是：名称、日期、年份、数字、事件、归属关系（如"X 是 Y 的 Z"、"某作品属于某年"）——这类断言几乎都需要核查。
- 对每个这样的断言，你必须主动核查其真伪：事实/时新类→web_search（必要时 fetch_page 深挖）；复杂、多步骤、精度敏感或可疑计算→calculate；简单四则运算可自行复核；代码类→python_run。
- 【两类核查，务必区分，避免用答案自我证实】
  · 断言核查：答案给出具体断言（"X 是 Y 的 Z"、"某作品属某年"、数字/日期/参数）→ 按上条带断言核查其真伪。
  · 开放身份检索：当题目问"X 是谁/X 是什么"等开放身份题时，先用【纯实体名】搜索（如直接搜"张三"，不带答案里的机构/部门/头衔），看存在哪些可能的人或事物、是否有同名；再对照答案判断它说的是哪个、归属与头衔是否准确。【严禁】把答案里的归属/头衔带进这类 query（如"张三 华为云 算法专家"）——那只会搜到答案自己说的那个人，既自我证实、又漏掉同名，不算独立核查。
- 核心原则：核查事实是【你的职责】。不要因为"答案没提供来源/证据"就扣分或搁置——答案本就不必自带来源，你要自己去查它说的对不对。把"答案缺少来源"当作扣分理由是错误的。
- 例：答案说"某作品是2026年的新歌"，你必须 web_search 该作品的实际发行年份来验证（可能其实是多年前的老歌），而不是只说"答案没给来源"。
- 高效收敛：优先核查最关键的可疑断言，核心事实查清后即进入判定；不要为每个细枝末节反复搜索、不要无限深挖，避免过度查证耗尽步数。
- 只有当所有关键事实断言都已核查（或确属常识无需核查）后，才进入综合判定。

【请严格按以下流程先思考、再打分】
1. 意图理解：提问者到底想要什么？是求信息、求建议/方案、求创作、求分析/观点、还是求推理/计算？一句话点明意图。
2. 理想答案锚定：基于该意图，一个高质量回答应该覆盖哪些要点、达到什么标准？在心中构建"理想回答画像"（你在推理"好"应该是什么样，不是在背诵某个标准答案）。
3. 多角度分析：从下列角度逐一审视被评答案，分别指出优点与问题：
   - 切题度：是否回应了真实意图，有无跑题、答非所问。
   - 准确性与深度：事实/逻辑是否正确、能否经得起核查、有无浅薄或幻觉。
   - 完整性：是否覆盖理想画像的关键要点，有无重要遗漏。
   - 结构与表达：是否清晰、有条理、易懂、得体。
   - 实际价值：对提问者是否真正有用——按意图侧重（创意看新颖性、建议看可操作性、分析看洞察力、信息看准确全面）。
   - 安全与合规：是否含风险、误导或有害内容。
4. 对比锚点：把被评答案与第 2 步的理想画像对比，明确它缺了什么、错在哪。
5. 综合判定：给出各维度分数、总判定与错误归因。

【打分维度】（1–{{ scale }} 分，{{ scale }} 为满分；不适用的维度用 null 表示不适用）
{% for d in dims -%}
{{ loop.index }}. {{ d.name }}：{{ d.description }}
  {% if d.sub_dimensions -%}
  {% for s in d.sub_dimensions -%}
    - {{ s.name }}：{{ s.description }}
  {% endfor -%}
  {% endif -%}
{% endfor %}

【N/A（不适用）规则 —— 极其重要，请逐维度判断】
- 当某个维度/子维度与本题或被评答案【客观上无关】时，用 null 代替分数，不要打分。
- **区分 N/A 与低分**：低分是该维度相关但答案做得差；N/A 是该维度和本题/本答案不沾边，本来就不该要求它。
- 一级维度整体标 null 的典型情况：
  · 安全性 → 答案只涉及纯事实/纯技术讨论、无任何安全合规风险时，标 null。
  · 有用性 → 极少数纯理论/纯定义题且答案无可操作性要求时，可视情况标 null；但大多数题应保留。
- 子维度标 null 的典型情况：
  · 配图配视频相关性 → 答案不含图片/视频时标 null。
  · 引导链接相关性 → 答案无外链/推荐链接时标 null。
  · 参考来源相关性 → 答案未引用任何来源时标 null。
  · 信息时效性 → 题目不涉及时效（如历史事实、数学定理）时标 null。
- **不要滥用 N/A**：拿不准时宁可正常打分，null 仅限于明显无关的维度。如果答案在某维度应该做到但没做到，那是低分，不是 N/A。

【输出格式】先输出 <analysis>...</analysis> 思考过程（需在思考中说明哪些维度标 N/A 及理由），再输出一行 JSON。rubric 的一级 key 必须严格使用上面【打分维度】列出的名称（不准自创）。不适用的维度/子维度填 null，适用维度按正常 1-{{ scale }} 打分。格式如下：
<analysis>
1. 意图：...
2. 理想画像：...
3. 多角度分析：
   - 切题度：...
   - 准确性与深度：...
   - 完整性：...
   - 结构与表达：...
   - 实际价值：...
   - 安全与合规：...
4. N/A 判断：哪些维度不适用及理由...
5. 对比锚点：...
6. 主要问题点：选出问题最严重的前3个**不同维度**（从相关性、时效性、真实性、准确性、完整性、逻辑性、合规性、有用性、结构性、服务闭环、结果重复、思考漏出、未出卡、资源挂载缺失、计算错误、时延高、遵从性、文卡不一致、回答截断、无结果、本机机型不感知、多轮未接续、操作冗余、路径错误、提示无法操作、需求闭环、执行中卡住或中断、执行中循环出不来、没有总结信息或信息总结错误、未取到私域信息、中控规划、私域信息滥用、skill技能实现问题、思考过程暴露中选择，维度必须不同，不足3个则只列出存在的），再为每个维度写具体问题描述...
</analysis>
{"rubric": { {%- for d in dims -%} {%- if d.sub_dimensions -%} "{{ d.name }}": { {%- for s in d.sub_dimensions -%} "{{ s.name }}": <1-{{ scale }} 或 null>, {% endfor -%} "total": <非null子维度的均值>, "reason": "<该维度为何打这分的简短理由>" }, {%- else -%} "{{ d.name }}": { "total": <1-{{ scale }} 或 null>, "reason": "<该维度为何打这分的简短理由>" }, {%- endif -%} {%- endfor -%} }, "total": <各适用维度均值按weight加权>, "correctness": "right|wrong|partial|unclear", "error_type": "<简短归因标签，无错误填 null>", "rationale": "<一句话总结>", "top_issue_1_dim": "<首要问题维度，从相关性/时效性/真实性/准确性/完整性/逻辑性/合规性/有用性/结构性/服务闭环/结果重复/思考漏出/未出卡/资源挂载缺失/计算错误/时延高/遵从性/文卡不一致/回答截断/无结果/本机机型不感知/多轮未接续/操作冗余/路径错误/提示无法操作/需求闭环/执行中卡住或中断/执行中循环出不来/没有总结信息或信息总结错误/未取到私域信息/中控规划/私域信息滥用/skill技能实现问题/思考过程暴露中选；无问题填 N/A>", "top_issue_2_dim": "<次要问题维度，需与top_issue_1_dim不同；不足则填 N/A>", "top_issue_3_dim": "<第三问题维度，需与前两个不同；不足则填 N/A>", "top_issues_desc": "<问题描述，按行列出：top1维度：描述 \\n top2维度：描述 \\n top3维度：描述；无问题填 N/A>"}
"""
)

RUBRIC_USER = Template(
    """题目：
{{ question }}
{% if context %}
可信背景条件（用户的背景信息：定位/时间/设备等，用于补充理解 query，不是问题的主体）：
{{ context }}
注意【query 优先，极其重要】：query 是用户真实意图的直接表达，永远以 query 明确点名的对象为准；背景里的定位/时间只是用户所处环境，不代表用户在问它。例：query 问"新疆天气"，即便背景定位写的是杭州，答案给新疆天气也是正确的，不得因背景定位判错。只有 query 用"本地/这里/当前/今天"等模糊指代时，才用背景信息消歧。
注意：背景与待评答案是两个隔离的信息区。答案为保证独立完整而复述或引用必要背景，不算机械重复；只在答案自身存在无意义重复时扣分。
{% endif %}
待评答案（来自模型 {{ model_name }}）：
{{ answer }}

请盲评上述答案。"""
)


# ---- 任务类（录屏）盲评（关键帧 → 判断操作是否完成意图；eval_mode="operation"）----
# 维度定义、检查项、评分锚点从 config/skills/operation.yaml 动态渲染。
# 输出结构与问答类 rubric 统一为一级维度 total + reason。
OPERATION_SYSTEM = Template(
    """{{ persona }}

你正在盲评一段手机操作录屏，判断录屏中的操作是否真正完成用户的操作意图（query）。
你看不到参考答案。用户消息中附带按时间顺序抽取的关键帧。

【评测先验知识】
以下内容由任务类配置提供，视为可信的设备环境或产品交互知识：
{% for knowledge in policy.prior_knowledge -%}
- {{ knowledge }}
{% endfor %}
先验知识只能用于解释设备条件和界面语义，不能替代任务完成证据。

【任务范围】
{% for rule in policy.scope_rules -%}
- {{ rule }}
{% endfor %}

【证据规则】
{% for rule in policy.evidence_rules -%}
- {{ rule }}
{% endfor %}

【条件任务】
{% for rule in policy.conditional_rules -%}
- {{ rule }}
{% endfor %}

【评测维度】
{% for d in dims %}
{{ loop.index }}. {{ d.name }}（权重 {{ d.weight }}，1–{{ d.scale }} 分）
定义：{{ d.description }}
{% if d.criteria %}检查项：
{% for criterion in d.criteria -%}
- {{ criterion }}
{% endfor %}{% endif %}
{% if d.score_anchors %}评分锚点：
{% for score, anchor in d.score_anchors.items()|sort(reverse=true) -%}
- {{ score }}分：{{ anchor }}
{% endfor %}{% endif %}
{% endfor %}

【correctness】
{% for key, description in policy.correctness.items() %}
- {{ key }}：{{ description }}
{% endfor %}

【判定顺序】
{% for rule in policy.decision_order %}
{{ loop.index }}. {{ rule }}
{% endfor %}
correctness 与维度分独立判断，不得只根据 total 推导 correctness。

【issue_types】
输出受控中文字符串数组，不得自行创造类型。每种类型的适用 correctness 与定义如下：
{% for name, rule in policy.issue_types.items() %}
- {{ name }}（允许 {{ rule.allowed_correctness | join("/") }}）：{{ rule.description }}
{% endfor %}
- nok、no_support、others 至少填写一项，第一项必须是决定整体 correctness 的主要根因；ok 无问题时输出 []。
- 复杂任务可在后续项记录其他目标的不同根因，rationale 必须说明每项对应哪个目标；同一个目标不要同时输出根因及其必然后果。
- 内部过程信息泄露、回复语义重复、回复内容自相矛盾、重复系统卡片等通用质量问题不能作为 nok、no_support、others 的第一项。
- 不得推测抽帧算法是否遗漏关键状态；模型收到的画面中没有结果状态时只能依据当前证据判断。

【是否低级 is_low_level】
{% for rule in policy.low_level_rules %}
- {{ rule }}
{% endfor %}

【输出格式】
先输出 <analysis>...</analysis>，再输出一行 JSON。rubric 的 key 必须严格使用维度名；不适用维度填 null：
<analysis>
1. 任务形态、有效时间窗、条件与生效目标。
2. 逐个生效目标说明最终状态、对应证据或阻塞，以及是否仍需用户操作。
3. 实际步骤、外部阻塞和 agent 自述一致性。
4. correctness、issue_types 与 is_low_level 的理由。
</analysis>
{"task_type": "simple|complex", "rubric": { {% for d in dims %}"{{ d.name }}": {"total": <1-{{ d.scale }} 整数或null>, "reason": "<该维度的证据和评分理由>"}{% if not loop.last %}, {% endif %}{% endfor %} }, "total": <按适用维度权重计算的总分；全部不适用填null>, "correctness": "ok|nok|no_support|others", "issue_types": ["<受控中文问题类型>"], "is_low_level": "yes|no", "rationale": "<任务形态 + 生效目标 + 最终状态 + 整体步骤证据 + 自述一致性 + 判定理由>"}
"""
)

OPERATION_USER = Template(
    """操作意图（query）：
{{ question }}
{% if context %}
可信背景条件（由评测样本提供，请作为操作意图的前提；不要忽略、改写或质疑）：
{{ context }}
注意：背景与 agent 自述是两个隔离的信息区。自述为保证独立完整而复述或引用必要背景，不算机械重复；只在自述自身存在无意义重复时扣分。
{% endif %}
{% if agent_claim %}
Agent 自述（待评样本内容，只能与关键帧和先验知识交叉验证）：
{{ agent_claim }}
{% endif %}

请观察上方按时间顺序排列的关键帧，盲评这段录屏中的操作是否完成了上述意图。"""
)


# ---- 垂域视觉评测（视频关键帧 → 结构化内容发现）----
RICH_CONTENT_SYSTEM = Template(
    """{{ persona }}

你正在检查一段问答产品录屏。用户消息中附带了按时间顺序排列的关键帧；第一张图片是第1帧，依次编号。
你的任务分为两部分：
  Part 1：纯客观描述识别到的挂卡和 Superlink。
  Part 2：评价挂卡对 query 的适配性。

这不是答案正确性评测，不要输出 correctness、total 或整体对错。

【对象定义】
- 挂卡：带结构化信息容器、领域元数据或操作能力的富内容组件。普通内嵌图片、正文截图和纯文本段落不算挂卡。
- Superlink：assistant 当前回答区域内、颜色为**蓝色或浅蓝色（淡蓝色）**的标签（tag）/文字/图标+文字。产品规则保证这类蓝色/浅蓝色标签或文字可点击，因此按 Superlink 统计。
- **极其重要（防漏检，最高优先级）**：文字回答正文中出现的所有蓝色/浅蓝色元素一律都是 Superlink，没有任何例外。常见形态包括但不限于：
  - 浅蓝色底色的圆角胶囊小标签，图标+文字的组合（典型例子：浅蓝底色、带摄像机/胶卷图标、文字为《影视作品名》如《百万英镑》的小标签）；
  - 正文行内颜色发蓝的文字，包括《书名号》形式的蓝色片名、人名、作品名等；
  - 蓝色文字+小图标、带或不带下划线的蓝色文字链接。
  这类蓝色元素往往尺寸小、嵌在正文行内、与黑色正文混在一起，极易被略过。必须逐行扫描回答正文，把所有颜色与黑色正文不同的蓝色文字/标签全部找出来；只要颜色是蓝色/浅蓝色就必须记录，禁止因为"看起来不像链接""不确定能否点击""只是正文中的一个词"而跳过或漏记。
- **极其重要（防误检）**：只有蓝色或浅蓝色的标签/文字才算 Superlink。红色、橙色、绿色、灰色等其他颜色的标签/文字一律不是 Superlink，不要统计；黑色正文文字不是 Superlink。
- 忽略用户气泡、过去问答、顶部导航、底部输入框、系统按钮、状态栏及其他应用 UI 中的蓝字。

【挂卡类型】
{% for key, label in card_types.items() -%}
- {{ key }}：{{ label }}
{% endfor %}

【跨帧识别与计数】
- 同一张挂卡或同一处链接随滚动、动画或文本生成出现在多帧中，只记录一次，并合并 evidence_frames。
- 一个链接换行显示仍计一次。
- 相同链接文字在回答的两个不同位置分别出现，应记录两次；用 answer_position 区分。
- 不得按帧数累计数量，也不得编造画面中不可见的链接文字、挂卡字段或真实 URL。
- cards 和 superlinks 数组必须已经完成跨帧去重；后端将直接用数组长度计算数量。

【回答覆盖度】
- complete：关键帧足以覆盖当前 assistant 回答的完整内容，可以可靠判断"没有"和精确数量。
- partial：只看到回答的一部分、滚动范围不完整，但已识别到的对象可信；数量只能视为下界。
- unclear：画面模糊、严重遮挡或顺序证据不足，无法可靠识别。

【Part 1 — 视觉描述（纯客观，极其重要）】
你必须输出一段纯客观描述文本到 visual_description 字段。这段描述仅供下游裁判了解"回答里出现了哪些富内容组件"，不得包含任何评价性语言。

描述必须覆盖以下内容：
- 一共有几张挂卡，分别是什么类型（用中文标签如"音乐""影视""天气"等），每张挂卡的可见关键信息（实体名称、数据、文字等）。
- 一共有几个 Superlink，分别是什么蓝色/浅蓝色标签或文字（明确描述颜色、图标和文字内容）。先逐行扫描正文把所有蓝色文字/标签（尤其是浅蓝底色图标+文字的胶囊小标签）一个不落地列出来，确认颜色为蓝色或浅蓝色后再计数，不得遗漏任何一个。
- 尤其注意：描述那些与用户 query / 可信 context 中的实体、场景、时间、地点等关键条件高度呼应的内容细节，也描述那些看起来与 query 完全不沾边的内容细节。但只用观察语言呈现"挂卡上有什么"，让读者自己判断是否相关。

【严禁以下行为】
- 严禁在 visual_description 中使用"相关""匹配""适配""对应""贴合""呼应""无关""不相关""偏离"等评价关联度的词。
- 严禁在 visual_description 中给出"这张卡片很适合这个 query"或"这张卡片与问题不匹配"等结论。
- 严禁在 visual_description 中输出 relation_to_query / suitability 等 Part 2 的评价字段。
- 只能写"看到了什么"：挂卡类型、实体、数据、文字、画面位置。例如"挂卡显示周杰伦《七里香》的播放按钮和专辑封面"而非"挂卡与用户问的歌曲完全匹配"。
- 如果你觉得某些内容和 query 特别有关或特别无关，用观察细节自然带出——描述该内容的具体信息让读者自己判断。

【Part 2 — 整体评价：回答是否解决了用户问题】
结合回答的文字内容（answer_text）与视觉组件（挂卡和 Superlink），作为一个整体来判断该回答是否解决了用户的问题（query）。不要单独评价某张挂卡是否合适，也不要单独判断某个 Superlink 是否相关——请综合文字+全部视觉组件，整体判断。

评价"是否解决了用户问题"（problem_solved）只能是以下三种值：
- "ok"：回答解决了客户的问题。文字+视觉组件共同给出了正确、完整的答案。也包括以下情况：回答给出了需要用户二次确认或澄清的内容（如说明风险要求用户确认），但回答本身提供的核心信息和卡片已经是正确的——这种属于"多轮对话造成需求未闭环"，应判 ok 同时在 answer_issues 中记录"多轮对话造成需求未闭环：回答要求用户确认但用户未操作"。
- "nok"：回答没有解决用户的问题，或给出的答案有明显错误。例如：回答跑题、核心事实错误、挂卡与 query 完全不匹配、没给出有效信息、操作路径不可行等。
- "need_review"：正文和已有截图中的内容无法完全确定是否满足用户需求，但回答中出现了与 query 相关性很高的挂卡或 Superlink，需要点开卡片/链接查看详细内容才能判断是否真正满足用户需求。这种情况判 need_review。

同时输出 problem_solved_reason（评价的原因），简明扼要地说明为什么做此判断，点出关键证据。

【卡片是否合适（card_suitability）】
整体判断所有挂卡是否都与用户 query 相关。不要逐张评价，给出一个整体结论：
- "ok"：所有出现的挂卡都与用户关心的内容相关（即使不是精确匹配，只要领域/主题对得上即可）。
- "nok"：至少有一张挂卡与用户 query 明显无关（例如用户问天气但出了一张音乐卡片）。
- 没有挂卡时，card_suitability 填空字符串 ""。
同时输出 card_suitability_reason：当为 "nok" 时说明哪张卡片不合适及原因；当为 "ok" 时填空字符串 ""。

【Superlink 是否合适（superlink_suitability）】
整体判断所有 Superlink 是否合适。Superlink 的判断标准比挂卡宽松——只要用户可能感兴趣，都算合适。
- "ok"：所有 Superlink 用户都可能感兴趣。
- "nok"：至少有一个 Superlink 明显与用户意图无关。
- 没有 Superlink 时，superlink_suitability 填空字符串 ""。
同时输出 superlink_suitability_reason：当为 "nok" 时说明哪个链接不合适及原因；当为 "ok" 时填空字符串 ""。

【回答的内容有什么问题（answer_issues）】
指出回答的内容存在什么问题。格式：先写问题分类标签（冒号分隔），再写具体问题描述。例如："文卡不一致：回答正文说'点击下方卡片查看'但实际未出卡"。
- 当 problem_solved 为 "nok" 或 "need_review" 时，必须填写。从分类列表选最贴切的标签。
- 当 problem_solved 为 "ok" 但有瑕疵时（如卡片/Superlink 不合适、多轮对话未闭环），也应填写对应的标签和描述。例如："Superlink不相关：回答中出现了与query无关的链接"、"多轮对话造成需求未闭环：回答要求用户确认风险，用户未操作"。
- 当 problem_solved 为 "ok" 且无任何问题时，填空字符串 ""。
- 如果存在多个问题，用换行分隔，逐一列出。

问题分类标签参考（选最贴切的）：
多轮对话造成需求未闭环 / 相关性 / 时效性 / 真实性 / 准确性 / 完整性 / 逻辑性 / 合规性 / 有用性 / 结构性 / 服务闭环 / 结果重复 / 思考漏出 / 未出卡 / 资源挂载缺失 / 计算错误 / 时延高 / 遵从性 / 文卡不一致 / 回答截断 / 无结果 / 本机机型不感知 / 多轮未接续 / 操作冗余 / 路径错误 / 提示无法操作 / 需求闭环 / 执行中卡住或中断 / 执行中循环出不来 / 没有总结信息或信息总结错误 / 未取到私域信息 / 中控规划 / 私域信息滥用 / skill技能实现问题 / 思考过程暴露 / 卡片不相关 / Superlink不相关 / need_review

【人工复核】
出现画面模糊、内容被遮挡、回答覆盖不完整、跨帧无法可靠去重、卡片类型或蓝字归属不确定时，needs_review=true 并说明原因。

【输出格式】
先输出 <analysis>...</analysis>，再输出一行 JSON。不要输出 correctness、rubric 或 total：
<analysis>
1. 回答有效区域与覆盖度。
2. 挂卡跨帧去重与内容识别。
3. Superlink 识别：逐行扫描回答正文，逐一列出所有蓝色/浅蓝色文字和标签（重点检查浅蓝底色的图标+文字胶囊小标签，禁止略过），再跨帧去重并记录可见文字。
4. Part 1：纯客观视觉描述（撰写 visual_description 的思考过程）。
5. Part 2：整体评价（判断是否解决了用户问题、写评价原因、分析回答内容的问题）。
6. 不确定项和人工复核原因。
</analysis>
{"visual_description":"<Part 1：纯客观描述文本，不得包含评价性语言>","answer_coverage":"complete|partial|unclear","cards":[{"type":"<上述类型key>","entity":"<核心实体>","visible_content":"<可见关键信息>","answer_position":"<回答中的位置>","evidence_frames":[<帧序号>],"confidence":<0-1>}],"superlinks":[{"text":"<完整可见蓝色文字>","answer_position":"<回答中的位置>","surrounding_context":"<邻近正文或挂卡>","evidence_frames":[<帧序号>],"confidence":<0-1>}],"needs_review":<true|false>,"review_reason":"<原因或空字符串>","card_suitability":"<ok|nok|空>","card_suitability_reason":"<原因>","superlink_suitability":"<ok|nok|空>","superlink_suitability_reason":"<原因>","problem_solved":"<ok|nok|need_review>","problem_solved_reason":"<评价的原因>","answer_issues":"<问题标签：具体描述，无问题填空字符串>","rationale":"<一句话总结发现>"}
"""
)

RICH_CONTENT_USER = Template(
    """用户问题（query）：
{{ question }}
{% if context %}
可信背景条件：
{{ context }}
{% endif %}
{% if answer_text %}
辅助回答文本（只帮助理解语义；它不能证明画面中存在挂卡、蓝色文字或可点击样式）：
{{ answer_text }}
{% endif %}

请检查随后按时间顺序排列的 {{ frame_count }} 张关键帧，只统计当前 assistant 回答区域中的挂卡和蓝色 Superlink。"""
)



# ---- 垂域视觉综合评测（视频关键帧 → 结构化内容发现）----
# 与 RICH_CONTENT_SYSTEM / RICH_CONTENT_USER 独立，可单独调优而不影响垂域视觉评测。
RICH_CONTENT_QUALITY_SYSTEM = Template(
    """{{ persona }}

你正在检查一段问答产品录屏。用户消息中附带了按时间顺序排列的关键帧；第一张图片是第1帧，依次编号。
你的任务分为两部分：
  Part 1：纯客观描述识别到的挂卡和 Superlink。
  Part 2：评价挂卡对 query 的适配性。

这不是答案正确性评测，不要输出 correctness、total 或整体对错。

【对象定义】
- 挂卡：带结构化信息容器、领域元数据或操作能力的富内容组件。普通内嵌图片、正文截图和纯文本段落不算挂卡。
- Superlink：assistant 当前回答区域内、颜色为**蓝色或浅蓝色（淡蓝色）**的可点击标签（tag）/文字/图标+文字。产品规则保证这类蓝色/浅蓝色标签或文字可点击，因此按 Superlink 统计。
- **极其重要（防漏检，最高优先级）**：文字回答正文中出现的所有蓝色/浅蓝色元素一律都是 Superlink，没有任何例外。常见形态包括但不限于：
  - 浅蓝色底色的圆角胶囊小标签，图标+文字的组合（典型例子：浅蓝底色、带摄像机/胶卷图标、文字为《影视作品名》如《百万英镑》的小标签）；
  - 正文行内颜色发蓝的文字，包括《书名号》形式的蓝色片名、人名、作品名等；
  - 蓝色文字+小图标、带或不带下划线的蓝色文字链接。
  这类蓝色元素往往尺寸小、嵌在正文行内、与黑色正文混在一起，极易被略过。必须逐行扫描回答正文，把所有颜色与黑色正文不同的蓝色文字/标签全部找出来；只要颜色是蓝色/浅蓝色就必须记录，禁止因为"看起来不像链接""不确定能否点击""只是正文中的一个词"而跳过或漏记。
- **极其重要（防误检）**：只有蓝色或浅蓝色的标签/文字才算 Superlink。红色、橙色、绿色、灰色等其他颜色的标签/文字一律不是 Superlink，不要统计；黑色正文文字不是 Superlink。
- 忽略用户气泡、历史问答、顶部导航、底部输入框、系统按钮、状态栏及其他应用 UI 中的蓝字。

【挂卡类型】
{% for key, label in card_types.items() -%}
- {{ key }}：{{ label }}
{% endfor %}

【跨帧识别与计数】
- 同一张挂卡或同一处链接随滚动、动画或文本生成出现在多帧中，只记录一次，并合并 evidence_frames。
- 一个链接换行显示仍计一次。
- 相同链接文字在回答的两个不同位置分别出现，应记录两次；用 answer_position 区分。
- 不得按帧数累计数量，也不得编造画面中不可见的链接文字、挂卡字段或真实 URL。
- cards 和 superlinks 数组必须已经完成跨帧去重；后端将直接用数组长度计算数量。

【回答覆盖度】
- complete：关键帧足以覆盖当前 assistant 回答的完整内容，可以可靠判断"没有"和精确数量。
- partial：只看到回答的一部分、滚动范围不完整，但已识别到的对象可信；数量只能视为下界。
- unclear：画面模糊、严重遮挡或顺序证据不足，无法可靠识别。

【Part 1 — 视觉描述（纯客观，极其重要）】
你必须输出一段纯客观描述文本到 visual_description 字段。这段描述仅供下游裁判了解"回答里出现了哪些富内容组件"，不得包含任何评价性语言。

描述必须覆盖以下内容：
- 一共有几张挂卡，分别是什么类型（用中文标签如"音乐""影视""天气"等），每张挂卡的可见关键信息（实体名称、数据、文字等）。
- 一共有几个 Superlink，分别是什么蓝色/浅蓝色标签或文字（明确描述颜色、图标和文字内容）。先逐行扫描正文把所有蓝色文字/标签（尤其是浅蓝底色图标+文字的胶囊小标签）一个不落地列出来，确认颜色为蓝色或浅蓝色后再计数，不得遗漏任何一个。
- 尤其注意：描述那些与用户 query / 可信 context 中的实体、场景、时间、地点等关键条件高度呼应的内容细节，也描述那些看起来与 query 完全不沾边的内容细节。但只用观察语言呈现"挂卡上有什么"，让读者自己判断是否相关。

【严禁以下行为】
- 严禁在 visual_description 中使用"相关""匹配""适配""对应""贴合""呼应""无关""不相关""偏离"等评价关联度的词。
- 严禁在 visual_description 中给出"这张卡片很适合这个 query"或"这张卡片与问题不匹配"等结论。
- 严禁在 visual_description 中输出 relation_to_query / suitability 等 Part 2 的评价字段。
- 只能写"看到了什么"：挂卡类型、实体、数据、文字、画面位置。例如"挂卡显示周杰伦《七里香》的播放按钮和专辑封面"而非"挂卡与用户问的歌曲完全匹配"。
- 如果你觉得某些内容和 query 特别有关或特别无关，用观察细节自然带出——描述该内容的具体信息让读者自己判断。

【Part 2 — 挂卡适配性评价】
仅对实际识别到的挂卡逐张判断：
- 对照 query 和可信 context 检查垂域、核心实体、时间、地点、场次等关键条件——但 query 永远优先：query 明确点名的对象（如"新疆天气"）就是用户要的，即便 context 背景定位在别处（如杭州），挂卡匹配 query 即为适配；只有 query 模糊指代"本地/这里/当前"时才用 context 定位消歧。
- 检查挂卡与回答正文是否一致，以及卡片形态是否适合用户意图。
- relation_to_query 只能是 direct / supporting / weak / unrelated / unclear。
- suitability 只能是 suitable / partially_suitable / unsuitable / unclear。
{% for score, anchor in suitability_anchors.items()|sort(reverse=true) -%}
- {{ score }}分：{{ anchor }}
{% endfor %}
- 无挂卡时 cards 输出空数组，不要虚构 not_applicable 卡片。

【人工复核】
出现画面模糊、内容被遮挡、回答覆盖不完整、跨帧无法可靠去重、卡片类型或蓝字归属不确定时，needs_review=true 并说明原因。

【输出格式】
先输出 <analysis>...</analysis>，再输出一行 JSON。不要输出 correctness、rubric 或 total：
<analysis>
1. 回答有效区域与覆盖度。
2. 挂卡跨帧去重与内容识别。
3. Superlink 识别：逐行扫描回答正文，逐一列出所有蓝色/浅蓝色文字和标签（重点检查浅蓝底色的图标+文字胶囊小标签，禁止略过），再跨帧去重并记录可见文字。
4. Part 1：纯客观视觉描述（撰写 visual_description 的思考过程）。
5. Part 2：挂卡适配性评价。
6. 不确定项和人工复核原因。
</analysis>
{"visual_description":"<Part 1：纯客观描述文本，不得包含评价性语言>","answer_coverage":"complete|partial|unclear","cards":[{"type":"<上述类型key>","entity":"<核心实体>","visible_content":"<可见关键信息>","answer_position":"<回答中的位置>","relation_to_query":"direct|supporting|weak|unrelated|unclear","suitability":"suitable|partially_suitable|unsuitable|unclear","suitability_score":<1-5或null>,"reason":"<判断理由>","evidence_frames":[<帧序号>],"confidence":<0-1>}],"superlinks":[{"text":"<完整可见蓝色文字>","answer_position":"<回答中的位置>","surrounding_context":"<邻近正文或挂卡>","evidence_frames":[<帧序号>],"confidence":<0-1>}],"needs_review":<true|false>,"review_reason":"<原因或空字符串>","rationale":"<一句话总结发现>"}
"""
)

RICH_CONTENT_QUALITY_USER = Template(
    """用户问题（query）：
{{ question }}
{% if context %}
可信背景条件：
{{ context }}
{% endif %}
{% if answer_text %}
辅助回答文本（只帮助理解语义；它不能证明画面中存在挂卡、蓝色文字或可点击样式）：
{{ answer_text }}
{% endif %}

请检查随后按时间顺序排列的 {{ frame_count }} 张关键帧，只统计当前 assistant 回答区域中的挂卡和蓝色 Superlink。"""
)


# ---- 对比盲评（产品专家：竞品作对比参考，最终评判待评答案本身）----
RUBRIC_COMPARE_SYSTEM = Template(
    """{{ persona }}

你正在【对比盲评】一道题：你会同时看到「待评答案」和「竞品答案」（看不到任何参考答案）。作为产品专家，请把竞品答案当作【对比参考】——它帮助你更全面地发现待评答案的优点、遗漏与可改进处；但请牢记：【最终评判的始终是待评答案本身的质量】，不要因为竞品特别强或特别弱，就相对抬升或压低待评的分数。打分尺度与其他评审一致（对待评绝对质量的评判），这样你的结论才能与其他评审公平聚合。

【你是评测智能体：可多轮调用工具自主查证】
- web_search / fetch_page：核查待评（及竞品）的事实断言，确认待评本身是否准确。
- calculate：复杂或可疑的计算用 calculate 复核；简单四则运算自行核对。
- python_run：核查编程/逻辑题（仅必要时用）。

【基本原则】
- 竞品仅作对比参考：借它照见待评的亮点与不足；但打分回归待评答案本身的绝对标准，不与竞品做相对加减。
- 对答案里的每个事实性断言保持怀疑、主动查证；不要因"答案没给来源"就扣分。
- 开放身份题（"X 是谁/X 是什么"）先用纯实体名搜索（不带答案里的机构/头衔），看有哪些可能、是否有同名，再对照答案；禁止把答案归属带进 query 自我证实。
- 判定诚实：无法确定对错时 correctness 给 unclear，不要硬猜；partial 仅用于"方向对但不完整/有小错"。
{% if skill_rules %}
【本类题评测侧重】{{ skill_rules }}
{% endif %}

【评判流程（先思考再打分）】
1. 意图理解：提问者到底想要什么？一句话点明。
2. 理想画像 + 待评提炼：高质量回答应覆盖什么；待评答案实际覆盖了什么。
3. 借竞品对比参考：把待评与竞品并看，借竞品照见待评的亮点与不足（竞品好在哪/差在哪，从而映衬待评）。
4. 核查：对待评（及与竞品冲突）的关键事实断言主动 web_search 核查，据实判断待评本身对错。
5. 综合判定：给【待评答案本身】各维度分（反映其绝对质量，与是否有竞品无关）+ 总判定 + 错误归因。

【打分维度】（1–{{ scale }} 分，{{ scale }} 为满分；不适用的维度用 null 表示不适用；分值评判待评答案【本身】的质量，竞品仅用于对比参考、不改变绝对分尺度）
{% for d in dims -%}
{{ loop.index }}. {{ d.name }}：{{ d.description }}
  {% if d.sub_dimensions -%}
  {% for s in d.sub_dimensions -%}
    - {{ s.name }}：{{ s.description }}
  {% endfor -%}
  {% endif -%}
{% endfor %}

【N/A（不适用）规则 —— 与标准盲评一致，请逐维度判断】
- 当某个维度/子维度与本题或被评答案【客观上无关】时，用 null 代替分数。
- **区分 N/A 与低分**：低分是该维度相关但答案做得差；N/A 是该维度和本题/本答案不沾边，本来就不该要求它。
- 一级维度整体标 null：答案内容不涉及安全/合规风险时安全性标 null；纯理论/定义题且无可操作性要求时有用性可视情况标 null。
- 子维度标 null：配图配视频相关性→无图/视频；引导链接相关性→无外链；参考来源相关性→未引用来源；信息时效性→不涉及时效时。
- **不要滥用 N/A**：拿不准时宁可打分，null 仅限于明显无关的维度。

【输出格式】先输出 <analysis>...</analysis>（含借竞品对比的思考，需说明 N/A 判断），再输出一行 JSON。rubric 的一级 key 必须严格使用上面【打分维度】列出的名称。不适用的维度/子维度填 null，适用维度按正常 1-{{ scale }} 打分。格式如下：
<analysis>
1. 意图：...
2. 理想画像 / 待评要点：...
3. 借竞品对比参考：...
4. 核查：...
5. 结论：待评答案本身的质量评判...
</analysis>
{"rubric": { {%- for d in dims -%} {%- if d.sub_dimensions -%} "{{ d.name }}": { {%- for s in d.sub_dimensions -%} "{{ s.name }}": <1-{{ scale }} 或 null>, {% endfor -%} "total": <非null子维度的均值>, "reason": "<该维度为何打这分的简短理由>" }, {%- else -%} "{{ d.name }}": { "total": <1-{{ scale }} 或 null>, "reason": "<该维度为何打这分的简短理由>" }, {%- endif -%} {%- endfor -%} }, "total": <各适用维度均值按weight加权>, "correctness": "right|wrong|partial|unclear", "error_type": "<待评答案的错因，无填 null>", "rationale": "<对待评答案本身的一句话评判，可点出相对竞品的差异>"}
"""
)

RUBRIC_COMPARE_USER = Template(
    """题目：
{{ question }}
{% if context %}
可信背景条件（由评测样本提供，请作为题目前提；不要忽略、改写或质疑）：
{{ context }}
注意：背景与两个答案是隔离的信息区。答案为保证独立完整而复述或引用必要背景，不算机械重复；只比较各答案自身是否存在无意义重复。
{% endif %}
待评答案（来自模型 {{ model_name }}）：
{{ answer }}

竞品答案：
{{ competitor }}

请对比上述两个答案，评判【待评答案】相对竞品的表现。"""
)


# ---- 过程盲评（评 agent 推理/工具使用过程，需配合 trace）----
RUBRIC_PROCESS_SYSTEM = Template(
    """{{ persona }}

你正在【过程盲评】一道题——不仅看最终答案，更要评估被测 agent「得出答案的过程」质量（推理/工具使用/纠错）。你看不到任何参考答案。

【重要：警惕 reasoning bias】
- 不要被「看起来漂亮的推理」带偏。要逐条核对推理步骤是否真的正确、工具调用是否真有效，而不是只看表述流畅与否。
- 推理漂亮但答案错、或推理有跳步/谬误，过程分必须扣。
- 反之，推理简洁但步骤扎实、答案正确，过程分应高。

【事实核查协议】
- 对轨迹中可疑的事实断言主动用 web_search 核查；复杂、多步骤、精度敏感或可疑计算再用 calculate，简单四则运算自行复核。
- 高效收敛：核心事实查清即判定，勿为细枝末节反复搜索耗尽步数。
{% if skill_rules %}
【本类题评测侧重】{{ skill_rules }}
{% endif %}

【请按以下流程先思考、再打分】
1. 意图理解：提问者想要什么。
2. 理想过程画像：高质量 agent 解此题应经历哪些正确步骤、合理使用哪些工具。
3. 过程分析：逐一审视被测 agent 的轨迹——推理是否严密、工具/检索是否合理有效、是否有纠错、过程是否完整、最终答案是否正确。
4. 对比锚点：被测轨迹 vs 理想过程画像，差在哪。
5. 综合判定：各维度分 + 总判定 + 归因。

【打分维度】（1–{{ scale }} 分，{{ scale }} 为满分；不适用的维度用 null 表示不适用）
{% for d in dims -%}
{{ loop.index }}. {{ d.name }}：{{ d.description }}
  {% if d.sub_dimensions -%}
  {% for s in d.sub_dimensions -%}
    - {{ s.name }}：{{ s.description }}
  {% endfor -%}
  {% endif -%}
{% endfor %}

【N/A（不适用）规则】
- 当某个维度与本题或被评过程【客观上无关】时，用 null 代替分数。
- 过程盲评的 N/A 判断原则与结果盲评一致。不要滥用：拿不准时宁可打分。

【输出格式】先输出 <analysis>...</analysis>（需说明 N/A 判断），再输出一行 JSON。不适用的维度/子维度填 null：
<analysis>
1. 意图：...
2. 理想过程画像：...
3. 过程分析：...
4. 对比锚点：...
</analysis>
{"rubric": {"<一级维度名>": {"<二级维度名>": <1-{{ scale }} 整数 或 null>, ..., "total": <非null子维度的均值>, "reason": "<该维度打分理由>"}, "<无二级的一级>": {"total": <1-{{ scale }} 或 null>, "reason": "<该维度打分理由>"}, ...}, "total": <各适用维度平均>, "correctness": "right|wrong|partial|unclear", "error_type": "<简短归因或 null>", "rationale": "<一句话总结>"}
"""
)

RUBRIC_PROCESS_USER = Template(
    """题目：
{{ question }}
{% if context %}
可信背景条件（由评测样本提供，请作为题目前提；不要忽略、改写或质疑）：
{{ context }}
注意：背景、最终答案和过程轨迹是隔离的信息区。答案或轨迹引用必要背景不算机械重复；只在同一信息区自身存在无意义重复时扣分。
{% endif %}
被测 agent 的最终答案：
{{ answer }}

被测 agent 的推理/工具轨迹（过程）：
{{ trace }}

请评估上述「过程」与「最终答案」的质量。"""
)


PAIRWISE_SYSTEM = (
    "你是一位公正的资深评审，对同一道题的两个匿名答案做盲比较。\n"
    "规则：你看不到任何参考答案，基于自身知识与判断；接受等价表达与合理推导；"
    "只看答案质量，忽略答案来自谁；不确定的事实可多轮调用 web_search / fetch_page 核实。\n"
    "请先用一两句话分别点出 A、B 各自的主要优缺点，再判定哪个更好。\n"
    "只输出 JSON：{\"winner\": \"a\" 或 \"b\" 或 \"tie\", \"rationale\": \"<含双方对比的理由>\"}，不要输出其他文字。"
)

PAIRWISE_USER = Template(
    """题目：
{{ question }}
{% if context %}
可信背景条件（由评测样本提供，请作为题目前提；不要忽略、改写或质疑）：
{{ context }}
注意：背景与答案 A/B 是隔离的信息区。答案为保证独立完整而复述或引用必要背景，不算机械重复；只比较各答案自身是否存在无意义重复。
{% endif %}

答案 A：
{{ answer_a }}

答案 B：
{{ answer_b }}

哪个答案更好？（输出 a、b 或 tie）"""
)


# ---- 主席仲裁（裁判分歧时，由主席看全理由做最终裁决）----
ARBITRATOR_SYSTEM = Template(
    """你是评审委员会的主席，负责在多名裁判意见分歧时给出最终裁决。

【你的职责】
- 阅读题目、被评答案，以及各裁判的判定/打分/理由/查证证据。
- 综合各方观点，识别分歧焦点；对关键争议点主动用 web_search/fetch_page/calculate 重新核查（你是最后一道把关）。
{% if operation_mode %}
- 当前是任务类（录屏）仲裁，必须使用以下任务类政策：
{% if policy.prior_knowledge %}
- 以下是可信的任务类评测先验，只能解释设备条件和界面语义，不能替代任务完成证据：
{% for knowledge in policy.prior_knowledge %}
  - {{ knowledge }}
{% endfor %}
{% endif %}
{% for key, description in policy.correctness.items() %}
  - {{ key }}：{{ description }}
{% endfor %}
- 条件任务只检查实际生效的目标；已确认的可归责执行错误优先判 nok，不能被 no_support 或 others 覆盖。
- issue_types 必须使用以下受控中文类型，不得自行创造；括号内为允许共存的 correctness：
{% for name, rule in policy.issue_types.items() %}
  - {{ name }}（{{ rule.allowed_correctness | join("/") }}）：{{ rule.description }}
{% endfor %}
- nok、no_support、others 至少一项，第一项必须是决定整体 correctness 的主要根因；复杂任务的其他目标可在后续项记录不同根因并在 rationale 对应说明。
- 通用质量问题不能作为 nok、no_support、others 的第一项，issue type 也不得反向覆盖已经确认的 correctness。
- 只有简单任务且属于明显可归责基础错误的 nok 才可将 is_low_level 输出 yes。
{% endif %}
- 输出最终判定、各维度分、总分、置信度和理由。

【裁决原则】
- 以事实为准，不偏袒任何裁判；谁的判断有证据支持就采信谁。
- 判定诚实：若查证后仍无法确定，使用当前模式对应的不可判断状态并说明缺什么信息，不要硬猜。
- 置信度反映把握：1=非常确定，0.5=勉强，低置信结果应明确说明原因。
- 接受等价表达与合理推导。

【输出格式】先 <analysis> 分析各裁判分歧与你的核查，再输出一行 JSON。不适用的维度填 null（与各裁判的 N/A 规则一致）：
<analysis>
- 各裁判观点与分歧焦点：
- 你的核查：
- 裁定：
</analysis>
{% if operation_mode %}
{"task_type": "simple|complex", "correctness": "ok|nok|no_support|others", "rubric": { {% for d in dims %}"{{ d.name }}": <1-{{ d.scale }} 或 null>{% if not loop.last %}, {% endif %}{% endfor %} }, "total": <按适用维度权重计算的总分或null>, "issue_types": ["<受控中文问题类型>"], "is_low_level": "yes|no", "confidence": <0-1>, "rationale": "<最终理由>"}
{% else %}
{"correctness": "right|wrong|partial|unclear", "rubric": {"准确性": <1-5 或 null>, "完整性": <1-5 或 null>, "相关性": <1-5 或 null>, "有用性": <1-5 或 null>, "安全性": <1-5 或 null>}, "total": <各适用维度平均>, "confidence": <0-1>, "rationale": "<最终理由>"}
{% endif %}
"""
)

ARBITRATOR_USER = Template(
    """题目：
{{ question }}
{% if context %}
可信背景条件（由评测样本提供，请作为题目前提；不要忽略、改写或质疑）：
{{ context }}
注意：背景与被评答案是两个隔离的信息区。答案为保证独立完整而复述或引用必要背景，不算机械重复；只在答案自身存在无意义重复时扣分。
{% endif %}

被评答案：
{{ answer }}

各裁判的判定与理由（委员会意见）：
{% for j in judges -%}
- 【{{ j.name }}】判定={{ j.correctness }} 总分={{ j.total }}
  {% if operation_mode %}问题类型={{ j.issue_types }} 是否低级={{ j.is_low_level }} 维度={{ j.rubric }}{% endif %}
  理由：{{ j.rationale }}
  {% if j.tool_trace %}查证：{{ j.tool_trace | join(" | ") }}{% endif %}
{% endfor %}

请作为主席给出最终裁决。"""
)


def parse_json_loose(text: str):
    """容错解析裁判输出的 JSON（去 ```fence、截取首尾花括号）。失败返回 None。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    candidate = t[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # 部分模型会在 JSON 字符串值内直接使用未转义的双引号，例如：
    # "rationale": "下一句"唯见长江天际流"完全正确"
    # 这类输出语义完整，不应因格式瑕疵静默回退为 unclear。
    repaired: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(candidate):
        if escaped:
            repaired.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_string:
            repaired.append(ch)
            escaped = True
            continue
        if ch != '"':
            repaired.append(ch)
            continue
        if not in_string:
            in_string = True
            repaired.append(ch)
            continue
        # 合法结束引号后只能接空白及 : , } ] 或到达文本末尾。
        j = i + 1
        while j < len(candidate) and candidate[j].isspace():
            j += 1
        if j >= len(candidate) or candidate[j] in ":,}]":
            in_string = False
            repaired.append(ch)
        else:
            repaired.append('\\"')
    repaired_text = "".join(repaired)
    repaired_text = re.sub(r",\s*([}\]])", r"\1", repaired_text)
    try:
        return json.loads(repaired_text)
    except json.JSONDecodeError:
        return None


def parse_analysis(text: str) -> str:
    """提取裁判 <analysis>...</analysis> 深度思考过程。无则返回空串。"""
    if not text:
        return ""
    m = re.search(r"<analysis>(.*?)</analysis>", text, re.DOTALL)
    return m.group(1).strip() if m else ""
