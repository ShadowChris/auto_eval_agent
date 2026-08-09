import { createApp, ref, computed, onMounted, onUnmounted, nextTick } from "https://unpkg.com/vue@3/dist/vue.esm-browser.js";
import * as echarts from "https://unpkg.com/echarts@5/dist/echarts.esm.min.js";

createApp({
  setup() {
    const workspacePage = ref("evaluation");
    const modes = [
      { key: "single", label: "垂域问答类" },
      { key: "compare", label: "两回答对比" },
      { key: "online", label: "接模型在线评估" },
      { key: "process", label: "过程盲评(含轨迹)" },
      { key: "operation", label: "任务类（录屏）" },
      { key: "rich_content", label: "垂域视觉评测" },
      { key: "rich_content_quality", label: "垂域视觉综合评测" },
    ];
    function modeLabel(key) {
      return modes.find((item) => item.key === key)?.label || key;
    }
    const mode = ref("single");
    const isVideoMode = computed(() => ["operation", "rich_content", "rich_content_quality"].includes(mode.value));
    const text = ref("");
    const fileText = ref("");
    const isJsonl = ref(false);
    const datasetName = ref("");
    const items = ref([]);
    let opItemSequence = 0;
    const opItems = ref([newOpItem()]);
    const opPage = ref(1);
    const opJumpPage = ref("");
    const opPreparing = ref(false);
    const errors = ref([]);
    const judges = ref([]);
    const models = ref([]);
    const selectedJudges = ref([]);
    const visibleJudges = computed(() => {
      if (mode.value !== "operation") return judges.value;
      const judge = terminalUserJudge();
      return judge ? [judge] : [];
    });
    const visualJudge = ref("");  // rich_content_quality 模式：挂卡识别裁判
    const selectedModel = ref("");
    const concurrency = ref(4);
    const evalTimeout = ref(300);
    const running = ref(false);
    const progress = ref(0);
    const total = ref(0);
    const results = ref([]);
    const summary = ref(null);
    const taskId = ref("");
    const runError = ref("");
    const itemProgress = ref({});
    const progressEvents = ref({});
    const pieChart = ref(null);
    const barChartRefs = ref([]);
    const resultBrowser = ref(null);
    const activeSkill = ref("");
    const resultQuery = ref("");
    const correctnessFilter = ref("");
    const problemDimFilter = ref("");
    const resultPage = ref(1);
    const resultPageSize = ref(10);
    const previewPage = ref(1);
    const progressPage = ref(1);
    const resultJumpPage = ref("");
    const previewJumpPage = ref("");
    const progressJumpPage = ref("");
    const cellTooltip = ref({ visible: false, text: "", style: {} });
    const historyItems = ref([]);
    const historyNoteDrafts = ref({});
    const historyNoteEditing = ref({});
    const loadingHistory = ref(false);
    const historyPage = ref(1);
    const historyPageSize = ref(10);
    const historyJumpPage = ref("");
    const knowledgePublished = ref(null);
    const knowledgeDraft = ref({ name: "任务类专家经验", description: "", version: 1, categories: [] });
    const knowledgeCategoryKey = ref("");
    const knowledgeHasDraft = ref(false);
    const knowledgeBusy = ref(false);
    const knowledgeMessage = ref("");
    const knowledgeError = ref(false);
    const clockNow = ref(Date.now());
    let tooltipHideTimer = null;
    let progressClockTimer = null;
    let activeEventSource = null;
    const pageSize = 10;
    const opPageSize = 10;
    const progressStages = ["排队", "分类", "模型/裁判", "聚合", "完成"];

    const formatHint = computed(
      () =>
        ({
          single: "每行一题：query [||| @context: 背景] ||| answer [||| competitor] [||| reference]   （context 可选且视为可信前提）",
          compare: "每行一题：query [||| @context: 背景] ||| answerA ||| answerB [||| reference]",
          online: "每行一题：query [||| @context: 背景] [||| reference]   （后端现场调模型生成回答，再盲评）",
          process: "每行一题：query [||| @context: 背景] ||| answer ||| trace [||| reference]",
          operation: "可逐题上传，也可导入 JSONL：query、context(可选)、video_path、agent_statement(可选)、task_start_time/task_end_time(可选，单位秒)；相对视频路径以项目根目录为基准。",
          rich_content: "可逐题上传，也可导入 JSONL：query、context(可选)、video_path、category/answer_text/content_start_time/content_end_time(均可选)；普通图片不算挂卡，回答区域蓝色文字按 Superlink 统计。",
          rich_content_quality: "综合评测：先视觉识别挂卡/Superlink（需选识别裁判），再将结果注入盲评裁判做回答质量评测（可多选）。格式与垂域视觉评测相同。",
        }[mode.value])
    );
    const placeholder = computed(
      () =>
        ({
          single: "附近有什么餐厅？ ||| @context: 当前时间19:00，地点上海人民广场 ||| 推荐南京大牌档\n中国最长的河流？ ||| 长江",
          compare: "附近有什么餐厅？ ||| @context: 当前时间19:00，地点上海人民广场 ||| 回答A ||| 回答B\n推荐一部科幻电影 ||| 星际穿越 ||| 流浪地球",
          online: "附近有什么餐厅？ ||| @context: 当前时间19:00，地点上海人民广场\n计算 17 × 24 等于多少？",
          process: "规划回家路线 ||| @context: 当前位于上海人民广场，目的地徐家汇 ||| 最终回答 ||| 推理轨迹\n某函数是否正确？ ||| 正确 ||| def f(n): return 1 if n<=1 else n*f(n-1)",
          operation: "",
          rich_content: "",
          rich_content_quality: "",
        }[mode.value])
    );

    const previewKeys = computed(() => {
      if (!items.value.length) return [];
      const keys = ["query", "context"];
      if (mode.value === "single") keys.push("answer", "reference");
      else if (mode.value === "compare") keys.push("answer_a", "answer_b", "reference");
      else if (mode.value === "process") keys.push("answer", "trace", "reference");
      else keys.push("reference");
      return keys.filter((k) => items.value.some((it) => it[k] != null && it[k] !== ""));
    });
    const opPageCount = computed(() => Math.max(1, Math.ceil(opItems.value.length / opPageSize)));
    const pagedOpItems = computed(() => {
      const page = Math.min(opPage.value, opPageCount.value);
      const start = (page - 1) * opPageSize;
      return opItems.value.slice(start, start + opPageSize).map((item, offset) => ({
        item,
        index: start + offset,
      }));
    });
    const previewPageCount = computed(() => Math.max(1, Math.ceil(items.value.length / pageSize)));
    const pagedPreviewItems = computed(() => {
      const page = Math.min(previewPage.value, previewPageCount.value);
      const start = (page - 1) * pageSize;
      return items.value.slice(start, start + pageSize).map((item, offset) => ({
        item,
        index: start + offset,
      }));
    });

    const progressRows = computed(() =>
      items.value.map((item, index) => {
        const current = itemProgress.value[index] || {};
        const result = results.value.find((entry) => entry.index === index);
        const events = progressEvents.value[index] || [];
        const startedAt = Number(current.started_at || 0);
        const finishedAt = Number(current.finished_at || 0);
        const resultElapsed = Number(result?.latency_s);
        const elapsedSeconds = Number.isFinite(resultElapsed)
          ? resultElapsed
          : startedAt > 0
            ? Math.max(0, ((finishedAt || clockNow.value) - startedAt) / 1000)
            : null;
        return {
          index,
          itemId: item.id || `q${index}`,
          query: item.query || item.question || "",
          percent: current.percent ?? 0,
          status: current.status || "pending",
          message: current.message || "排队中",
          requestId: current.request_id || "",
          module: current.module || "",
          judge: current.judge || "",
          round: Number(current.round || 0),
          stageRank: current.stage_rank ?? progressStageRank(current),
          elapsedSeconds,
          events,
          latestEvents: events.slice(-2),
        };
      })
    );
    const progressPageCount = computed(() => Math.max(1, Math.ceil(progressRows.value.length / pageSize)));
    const pagedProgressRows = computed(() => {
      const page = Math.min(progressPage.value, progressPageCount.value);
      const start = (page - 1) * pageSize;
      return progressRows.value.slice(start, start + pageSize);
    });
    const historyPageCount = computed(
      () => Math.max(1, Math.ceil(historyItems.value.length / historyPageSize.value)),
    );
    const pagedHistoryItems = computed(() => {
      const page = Math.min(historyPage.value, historyPageCount.value);
      const start = (page - 1) * historyPageSize.value;
      return historyItems.value.slice(start, start + historyPageSize.value);
    });
    const skillOverviewRows = computed(() => summary.value?.by_skill?.overview || []);
    const selectedKnowledgeCategory = computed(() =>
      knowledgeDraft.value.categories.find((category) => category.key === knowledgeCategoryKey.value)
      || knowledgeDraft.value.categories[0]
      || null
    );
    const knowledgeRuleCount = computed(() =>
      knowledgeDraft.value.categories.reduce((total, category) => total + category.rules.length, 0)
    );
    const knowledgePromptPreview = computed(() => {
      const lines = [
        "【专家经验】",
        "以下是可信的产品能力、前置条件和界面语义知识，仅使用与当前任务直接相关的条目。专家经验可以帮助解释录屏，但不能代替任务完成证据；判断能力范围时，专家经验优先于 Agent 自述；判断本次执行状态时，以录屏中的直接证据为准。",
        "",
      ];
      knowledgeDraft.value.categories.forEach((category) => {
        lines.push(`### ${category.name}`);
        category.rules.filter((rule) => String(rule).trim()).forEach((rule) => lines.push(`- ${String(rule).trim()}`));
        lines.push("");
      });
      return lines.join("\n").trim();
    });

    function cloneKnowledge(value) {
      return JSON.parse(JSON.stringify(value));
    }

    function knowledgeErrorText(data) {
      if (typeof data?.detail === "string") return data.detail;
      if (Array.isArray(data?.detail)) return data.detail.map((item) => item.msg || JSON.stringify(item)).join("；");
      return "未知错误";
    }

    async function loadKnowledge() {
      knowledgeBusy.value = true;
      knowledgeMessage.value = "";
      try {
        const response = await fetch("/api/knowledge/operation");
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(knowledgeErrorText(data));
        knowledgePublished.value = cloneKnowledge(data.published);
        knowledgeDraft.value = cloneKnowledge(data.draft);
        knowledgeHasDraft.value = Boolean(data.has_unpublished_changes);
        knowledgeCategoryKey.value = knowledgeDraft.value.categories[0]?.key || "";
      } catch (error) {
        knowledgeError.value = true;
        knowledgeMessage.value = `加载失败：${error.message}`;
      } finally {
        knowledgeBusy.value = false;
      }
    }

    async function openKnowledgePage() {
      workspacePage.value = "knowledge";
      if (!knowledgePublished.value) await loadKnowledge();
    }

    async function saveKnowledgeDraft(showMessage = true) {
      knowledgeBusy.value = true;
      knowledgeMessage.value = "";
      try {
        const response = await fetch("/api/knowledge/operation/draft", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(knowledgeDraft.value),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(knowledgeErrorText(data));
        knowledgeDraft.value = cloneKnowledge(data.draft);
        knowledgeHasDraft.value = true;
        knowledgeError.value = false;
        if (showMessage) knowledgeMessage.value = "草稿已保存；当前批跑仍使用已发布版本。";
        return true;
      } catch (error) {
        knowledgeError.value = true;
        knowledgeMessage.value = `保存失败：${error.message}`;
        return false;
      } finally {
        knowledgeBusy.value = false;
      }
    }

    async function publishKnowledge() {
      if (!(await saveKnowledgeDraft(false))) return;
      knowledgeBusy.value = true;
      try {
        const response = await fetch("/api/knowledge/operation/publish", { method: "POST" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(knowledgeErrorText(data));
        knowledgePublished.value = cloneKnowledge(data.published);
        knowledgeDraft.value = cloneKnowledge(data.published);
        knowledgeHasDraft.value = false;
        knowledgeError.value = false;
        knowledgeMessage.value = `已发布 v${data.published.version}，新启动的评测任务将使用此版本。`;
      } catch (error) {
        knowledgeError.value = true;
        knowledgeMessage.value = `发布失败：${error.message}`;
      } finally {
        knowledgeBusy.value = false;
      }
    }

    async function discardKnowledgeDraft() {
      if (!confirm("确认放弃当前专家经验草稿？")) return;
      knowledgeBusy.value = true;
      try {
        const response = await fetch("/api/knowledge/operation/draft", { method: "DELETE" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(knowledgeErrorText(data));
        knowledgeDraft.value = cloneKnowledge(data.draft);
        knowledgeHasDraft.value = false;
        knowledgeCategoryKey.value = knowledgeDraft.value.categories[0]?.key || "";
        knowledgeError.value = false;
        knowledgeMessage.value = "草稿已放弃，已恢复到发布版本。";
      } catch (error) {
        knowledgeError.value = true;
        knowledgeMessage.value = `放弃失败：${error.message}`;
      } finally {
        knowledgeBusy.value = false;
      }
    }

    function addKnowledgeCategory() {
      const used = new Set(knowledgeDraft.value.categories.map((category) => category.key));
      let index = knowledgeDraft.value.categories.length + 1;
      while (used.has(`category_${index}`)) index += 1;
      const category = { key: `category_${index}`, name: "新经验类别", description: "", rules: ["请填写一条专家经验。"] };
      knowledgeDraft.value.categories.push(category);
      knowledgeCategoryKey.value = category.key;
    }

    function removeKnowledgeCategory() {
      if (knowledgeDraft.value.categories.length <= 1) {
        alert("专家经验库至少需要保留一个类别。");
        return;
      }
      const category = selectedKnowledgeCategory.value;
      if (!category || !confirm(`确认删除类别“${category.name}”？`)) return;
      const index = knowledgeDraft.value.categories.findIndex((item) => item.key === category.key);
      knowledgeDraft.value.categories.splice(index, 1);
      knowledgeCategoryKey.value = knowledgeDraft.value.categories[Math.max(0, index - 1)]?.key || "";
    }

    function addKnowledgeRule() {
      selectedKnowledgeCategory.value?.rules.push("请填写一条专家经验。");
    }

    function removeKnowledgeRule(index) {
      const category = selectedKnowledgeCategory.value;
      if (!category || category.rules.length <= 1) {
        alert("每个类别至少需要保留一条规则。");
        return;
      }
      category.rules.splice(index, 1);
    }

    function moveKnowledgeRule(index, direction) {
      const rules = selectedKnowledgeCategory.value?.rules;
      if (!rules) return;
      const target = index + direction;
      if (target < 0 || target >= rules.length) return;
      [rules[index], rules[target]] = [rules[target], rules[index]];
    }

    function progressStageRank(progressItem) {
      if (progressItem.status === "done") return 4;
      if (progressItem.module === "结果聚合") return 3;
      if (["模型裁判", "工具调用", "被测模型", "单题评测"].includes(progressItem.module)) return 2;
      if (progressItem.module === "垂域分类") return 1;
      return 0;
    }

    function mergeItemProgress(incoming) {
      appendProgressEvent(incoming);
      const index = incoming.item_index;
      const previous = itemProgress.value[index] || {};
      const previousRank = previous.stage_rank ?? progressStageRank(previous);
      const incomingRank = progressStageRank(incoming);
      const terminal = ["done", "error", "cancelled"].includes(incoming.status);
      const updatedAt = Date.parse(incoming.updated_at || "");
      itemProgress.value = {
        ...itemProgress.value,
        [index]: {
          ...previous,
          ...incoming,
          // Agent Loop 总轮数未知，宏观阶段只前进、不倒退。
          stage_rank: incoming.status === "done"
            ? 4
            : Math.max(previousRank, incomingRank),
          finished_at: terminal
            ? (previous.finished_at || (Number.isFinite(updatedAt) ? updatedAt : Date.now()))
            : previous.finished_at,
        },
      };
    }

    function appendProgressEvent(incoming) {
      const index = incoming.item_index;
      if (index == null) return;
      const previous = progressEvents.value[index] || [];
      const eventKey = incoming.sequence != null
        ? `seq:${incoming.sequence}`
        : [
            incoming.updated_at, incoming.module, incoming.event,
            incoming.judge, incoming.round, incoming.message,
          ].join("|");
      if (previous.some((entry) => entry._key === eventKey)) return;
      progressEvents.value = {
        ...progressEvents.value,
        [index]: [...previous, { ...incoming, _key: eventKey }].slice(-100),
      };
    }

    function progressStageClass(row, stageIndex) {
      if (row.status === "done") return "completed";
      if (stageIndex < row.stageRank) return "completed";
      if (stageIndex === row.stageRank) return ["error", "cancelled"].includes(row.status) ? "error" : "active";
      return "pending";
    }

    function progressDisplay(row) {
      const message = row.message || "排队中";
      const parts = [];
      if (row.judge && !message.includes(row.judge)) parts.push(row.judge);
      const roundLabel = row.round > 0 ? `第${row.round}轮` : "";
      if (roundLabel && !message.includes(roundLabel)) parts.push(roundLabel);
      parts.push(message);
      return parts.join(" · ");
    }

    function progressStageLabel(row) {
      if (row.status === "error") return "失败";
      if (row.status === "cancelled") return "已中断";
      if (row.status === "done") return "完成";
      return progressStages[Math.max(0, Math.min(4, row.stageRank))];
    }

    function progressStatusClass(row) {
      if (row.status === "error") return "status-error";
      if (row.status === "cancelled") return "status-cancelled";
      if (row.status === "done") return "status-done";
      if (row.stageRank === 0) return "status-pending";
      return "status-running";
    }

    function progressMeta(row) {
      const parts = [];
      if (row.judge) parts.push(row.judge);
      if (row.round > 0) parts.push(`第${row.round}轮`);
      return parts.join(" · ");
    }

    function formatProgressEventTime(value) {
      const date = new Date(value || "");
      if (Number.isNaN(date.getTime())) return "--:--:--";
      return date.toLocaleTimeString("zh-CN", {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
    }

    function progressEventMeta(event) {
      const parts = [];
      if (event.module) parts.push(event.module);
      if (event.judge) parts.push(event.judge);
      if (Number(event.round || 0) > 0) parts.push(`第${event.round}轮`);
      return parts.join(" · ");
    }

    function progressEventMessage(event) {
      let message = String(event.message || "");
      const prefixes = [
        event.judge,
        Number(event.round || 0) > 0 ? `第${event.round}轮` : "",
        event.module,
      ].filter(Boolean);
      for (const prefix of prefixes) {
        message = message
          .replace(new RegExp(`^${escapeRegExp(prefix)}\\s*[·|｜]\\s*`), "")
          .replace(new RegExp(`^${escapeRegExp(prefix)}\\s*[：:]\\s*`), "");
      }
      return message.trim();
    }

    function escapeRegExp(value) {
      return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }

    function scrollProgressLog(event) {
      if (!event.currentTarget.open) return;
      nextTick(() => {
        const panel = event.currentTarget.querySelector(".progress-log-scroll");
        if (panel) panel.scrollTop = panel.scrollHeight;
      });
    }

    function formatProgressElapsed(seconds, status) {
      if (seconds == null || !Number.isFinite(seconds)) return "—";
      if (["done", "error", "cancelled"].includes(status)) {
        if (seconds < 60) return `${seconds.toFixed(1)}s`;
      }
      const whole = Math.max(0, Math.floor(seconds));
      if (whole < 60) return `${whole}s`;
      return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, "0")}s`;
    }

    function shortRequestId(requestId) {
      if (!requestId) return "等待生成";
      return requestId.length > 12 ? `…${requestId.slice(-11)}` : requestId;
    }

    async function copyRequestId(requestId) {
      if (!requestId) return;
      try {
        await navigator.clipboard.writeText(requestId);
      } catch (_) {}
    }

    const skillTabs = computed(() => {
      const map = new Map();
      results.value.forEach((r) => {
        if (r.error) {
          const failed = map.get("__error__") || { key: "__error__", label: "评估失败", count: 0 };
          failed.count += 1;
          map.set("__error__", failed);
          return;
        }
        if (!r.category) return;
        const key = r.category;
        const displayLabel = key === "operation" ? "任务类" : (r.category_display || key);
        const current = map.get(key) || { key, label: displayLabel, count: 0 };
        current.count += 1;
        map.set(key, current);
      });
      return Array.from(map.values()).sort((a, b) => {
        if (a.key === "__error__") return 1;
        if (b.key === "__error__") return -1;
        return b.count - a.count;
      });
    });

    const skillResults = computed(() => {
      if (mode.value === "compare" || !activeSkill.value) return results.value;
      if (activeSkill.value === "__error__") return results.value.filter((r) => r.error);
      return results.value.filter((r) => !r.error && r.category === activeSkill.value);
    });

    const rubricDims = computed(() => {
      const dims = [];
      skillResults.value.forEach((r) => {
        Object.keys(r.rubric || {}).forEach((d) => {
          if (!dims.includes(d)) dims.push(d);
        });
        // 也收集 N/A 维度，确保列始终存在（不同 case 可能 N/A 不同维度）
        (r.na_dimensions || []).forEach((d) => {
          if (!dims.includes(d)) dims.push(d);
        });
      });
      return dims;
    });

    const resultCols = computed(() => {
      const contextCols = results.value.some((r) => r.context != null && r.context !== "")
        ? [{ key: "context", label: "背景" }]
        : [];
      if (mode.value === "compare")
        return [
          { key: "query", label: "题目" },
          ...contextCols,
          { key: "answer_a", label: "回答 A" },
          { key: "answer_b", label: "回答 B" },
          { key: "winner", label: "胜者" },
          { key: "bidirectional_consistent", label: "双向一致" },
          { key: "rationale", label: "理由" },
        { key: "latency_s", label: "耗时" },
        ];
      if (mode.value === "operation")
        return [
          { key: "item_id", label: "题号" },
          { key: "query", label: "操作意图" },
          ...contextCols,
          { key: "correctness", label: "完成判定" },
          { key: "issue_types", label: "问题类型" },
          { key: "is_low_level", label: "是否低级" },
          { key: "total", label: "总分" },
          ...rubricDims.value.map((d) => ({ key: `rubric:${d}`, label: d, rubricDim: d })),
          { key: "arbitrated", label: "仲裁" },
          { key: "rationale", label: "步骤与证据" },
          { key: "latency_s", label: "耗时" },
        ];
      if (mode.value === "rich_content")
        return [
          { key: "item_id", label: "题号" },
          { key: "query", label: "Query" },
          ...contextCols,
          { key: "category_display", label: "垂域" },
          { key: "answer_text", label: "answer_text" },
          { key: "card_presence", label: "是否有卡片" },
          { key: "card_count", label: "卡片数量" },
          { key: "card_types", label: "卡片种类" },
          { key: "card_contents", label: "卡片内容" },
          { key: "superlink_presence", label: "Superlink是否存在" },
          { key: "superlink_count", label: "Superlink数量" },
          { key: "superlink_texts", label: "Superlink文字" },
          { key: "card_suitability", label: "卡片是否合适" },
          { key: "card_suitability_reason", label: "卡片不合适原因" },
          { key: "superlink_suitability", label: "Superlink是否合适" },
          { key: "superlink_suitability_reason", label: "Superlink不合适原因" },
          { key: "answer_coverage", label: "回答覆盖" },
          { key: "needs_review", label: "需人工复核" },
          { key: "review_reason", label: "复核原因" },
          { key: "problem_solved", label: "是否解决用户问题" },
          { key: "problem_solved_reason", label: "评价原因" },
          { key: "answer_issues", label: "回答内容问题" },
          { key: "rationale", label: "识别结论" },
          { key: "latency_s", label: "耗时" },
        ];
      if (mode.value === "rich_content_quality")
        return [
          { key: "item_id", label: "题号" },
          { key: "query", label: "Query" },
          ...contextCols,
          { key: "category_display", label: "垂域" },
          { key: "answer", label: "回答" },
          { key: "card_presence", label: "挂卡" },
          { key: "card_count", label: "挂卡数" },
          { key: "card_types", label: "挂卡类型" },
          { key: "card_contents", label: "挂卡内容" },
          { key: "card_suitability", label: "挂卡适配性" },
          { key: "card_suitability_score", label: "适配分" },
          { key: "superlink_presence", label: "Superlink" },
          { key: "superlink_count", label: "链接数" },
          { key: "superlink_texts", label: "链接文字" },
          { key: "answer_coverage", label: "回答覆盖" },
          { key: "correctness", label: "判定" },
          { key: "total", label: "总分" },
          ...rubricDims.value.map((d) => ({ key: `rubric:${d}`, label: d, rubricDim: d })),
          { key: "used_search", label: "联网" },
          { key: "truncated", label: "截断" },
          { key: "arbitrated", label: "仲裁" },
          { key: "top_issue_1_dim", label: "首要问题维度" },
          { key: "top_issue_2_dim", label: "次要问题维度" },
          { key: "top_issue_3_dim", label: "第三问题维度" },
          { key: "top_issues_desc", label: "问题描述" },
          { key: "needs_review", label: "需人工复核" },
          { key: "rationale", label: "理由" },
          { key: "latency_s", label: "耗时" },
        ];
      const dims = rubricDims.value.map((d) => ({ key: `rubric:${d}`, label: d, rubricDim: d }));
      return [
        { key: "item_id", label: "题号" },
        { key: "query", label: "题目" },
        ...contextCols,
        { key: mode.value === "online" ? "generated_answer" : "answer", label: mode.value === "online" ? "生成回答" : "回答" },
        { key: "correctness", label: "判定" },
        { key: "total", label: "总分" },
        ...dims,
        { key: "used_search", label: "联网" },
        { key: "truncated", label: "截断" },
        { key: "arbitrated", label: "仲裁" },
        { key: "agree", label: "与真值" },
        { key: "top_issue_1_dim", label: "首要问题维度" },
        { key: "top_issue_2_dim", label: "次要问题维度" },
        { key: "top_issue_3_dim", label: "第三问题维度" },
        { key: "top_issues_desc", label: "问题描述" },
        { key: "rationale", label: "理由" },
        { key: "latency_s", label: "耗时" },
      ];
    });

    function columnWidth(c) {
      const compact = c.rubricDim
        || [
          "correctness", "winner", "total", "used_search", "truncated", "arbitrated",
          "agree", "latency_s", "bidirectional_consistent", "is_low_level",
          "card_presence", "card_count", "superlink_presence",
          "superlink_count", "answer_coverage", "needs_review", "problem_solved",
        ].includes(c.key);
      const textColumn = ["query", "context", "answer", "answer_text", "generated_answer", "answer_a", "answer_b", "rationale", "top_issues_desc", "answer_issues", "problem_solved_reason"].includes(c.key);
      let minWidth = compact ? 80 : textColumn ? 150 : 96;
      let maxWidth = compact ? 120 : c.key === "rationale" ? 380 : textColumn ? 320 : 200;
      if (c.key === "item_id") {
        minWidth = 110;
        maxWidth = 160;
      } else if (c.key === "query" && mode.value === "operation") {
        minWidth = 140;
        maxWidth = 240;
      }
      const visualLength = (value) => Array.from(String(value ?? "")).reduce(
        (sum, char) => sum + (char.charCodeAt(0) > 255 ? 2 : 1),
        0,
      );
      const sampleLengths = skillResults.value
        .slice(0, 200)
        .map((result) => visualLength(cell(result, c)))
        .sort((a, b) => a - b);
      const representativeIndex = Math.max(0, Math.ceil(sampleLengths.length * 0.8) - 1);
      const representativeLength = sampleLengths[representativeIndex] || 1;
      const desired = (Math.max(visualLength(c.label), representativeLength) * 7) + 28;
      return Math.max(minWidth, Math.min(maxWidth, desired));
    }

    const resultTableWidth = computed(
      () => 48 + resultCols.value.reduce((sum, c) => sum + columnWidth(c), 0) + (isVideoMode.value ? 300 : 0)
    );

    function isFrozenResultColumn(column) {
      return mode.value === "operation" && ["item_id", "query"].includes(column.key);
    }

    function frozenResultColumnStyle(column, columnIndex) {
      if (!isFrozenResultColumn(column)) return {};
      const left = 48 + resultCols.value
        .slice(0, columnIndex)
        .reduce((sum, previous) => sum + columnWidth(previous), 0);
      return { left: `${left}px` };
    }

    const filteredResults = computed(() => {
      const q = resultQuery.value.trim().toLowerCase();
      const threshold = (summary.value && summary.value.by_skill && summary.value.by_skill.threshold) || 2;
      return skillResults.value.filter((r) => {
        if (correctnessFilter.value && r.correctness !== correctnessFilter.value) return false;
        if (problemDimFilter.value && (r.rubric || {})[problemDimFilter.value] > threshold) return false;
        if (problemDimFilter.value && (r.rubric || {})[problemDimFilter.value] == null) return false;
        if (q && !`${r.item_id || ""} ${r.query || ""} ${r.context || ""} ${r.answer || ""} ${r.answer_text || ""} ${(r.issue_types || []).join(" ")} ${(r.card_contents || []).join(" ")} ${(r.superlink_texts || []).join(" ")} ${r.rationale || ""}`.toLowerCase().includes(q)) return false;
        return true;
      });
    });

    const pageCount = computed(() => Math.max(1, Math.ceil(filteredResults.value.length / resultPageSize.value)));
    const pagedResults = computed(() => {
      const safePage = Math.min(resultPage.value, pageCount.value);
      const start = (safePage - 1) * resultPageSize.value;
      return filteredResults.value.slice(start, start + resultPageSize.value);
    });

    const fallbackStat = computed(() => {
      const bs = summary.value && summary.value.by_skill;
      if (!bs || !bs.overview) return null;
      const total = bs.overview.reduce((s, r) => s + (r.n_items || 0), 0);
      const fbCount = bs.overview.reduce((s, r) => s + (r.fallback_count || 0), 0);
      return { total, fbCount, rate: total ? fbCount / total : 0 };
    });

    function selectSkill(key) {
      activeSkill.value = key;
      problemDimFilter.value = "";
      resultPage.value = 1;
      progressPage.value = 1;
    }
    function drillDownDimension(skill, dimension) {
      activeSkill.value = skill;
      problemDimFilter.value = dimension;
      correctnessFilter.value = "";
      resultPage.value = 1;
      nextTick(() => resultBrowser.value && resultBrowser.value.scrollIntoView({ behavior: "smooth", block: "start" }));
    }
    function clearDimensionDrillDown() {
      problemDimFilter.value = "";
      resultPage.value = 1;
    }
    function resetResultPage() {
      resultPage.value = 1;
    }

    function paginationPages(current, total) {
      if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);
      const pages = new Set([1, total]);
      for (let page = Math.max(2, current - 1); page <= Math.min(total - 1, current + 1); page += 1) {
        pages.add(page);
      }
      const sorted = [...pages].sort((a, b) => a - b);
      const result = [];
      sorted.forEach((page, index) => {
        if (index > 0 && page - sorted[index - 1] > 1) result.push(`ellipsis-${page}`);
        result.push(page);
      });
      return result;
    }

    function setTablePage(kind, requestedPage) {
      const configs = {
        result: [resultPage, pageCount, resultJumpPage],
        operation: [opPage, opPageCount, opJumpPage],
        preview: [previewPage, previewPageCount, previewJumpPage],
        progress: [progressPage, progressPageCount, progressJumpPage],
        history: [historyPage, historyPageCount, historyJumpPage],
      };
      const config = configs[kind];
      if (!config || requestedPage === "" || requestedPage == null) return;
      const [pageRef, countRef, jumpRef] = config;
      const page = Math.trunc(Number(requestedPage));
      if (!Number.isFinite(page)) return;
      pageRef.value = Math.min(countRef.value, Math.max(1, page));
      jumpRef.value = "";
    }

    function changePage(delta) {
      setTablePage("result", resultPage.value + delta);
    }
    function changeOpPage(delta) {
      setTablePage("operation", opPage.value + delta);
    }
    function changePreviewPage(delta) {
      setTablePage("preview", previewPage.value + delta);
    }
    function changeProgressPage(delta) {
      setTablePage("progress", progressPage.value + delta);
    }
    function changeHistoryPage(delta) {
      setTablePage("history", historyPage.value + delta);
    }
    function jumpTablePage(kind) {
      const jumpValues = {
        result: resultJumpPage.value,
        operation: opJumpPage.value,
        preview: previewJumpPage.value,
        progress: progressJumpPage.value,
        history: historyJumpPage.value,
      };
      setTablePage(kind, jumpValues[kind]);
    }

    function changeResultPageSize() {
      if (![10, 20, 50].includes(resultPageSize.value)) resultPageSize.value = 10;
      resultPage.value = 1;
      resultJumpPage.value = "";
    }

    function changeHistoryPageSize() {
      if (![10, 20, 50].includes(historyPageSize.value)) historyPageSize.value = 10;
      historyPage.value = 1;
      historyJumpPage.value = "";
    }

    function trunc(v) {
      if (v == null) return "";
      const s = String(v);
      return s.length > 50 ? s.slice(0, 50) + "…" : s;
    }

    function defaultJudgeSelection(targetMode) {
      if (["single", "operation", "rich_content"].includes(targetMode)) {
        const endUserJudge = terminalUserJudge();
        if (endUserJudge) return [endUserJudge.name];
      }
      if (targetMode === "rich_content_quality") {
        // 综合评测：挂卡识别默认用第一位裁判，回答评测默认选所有非产品专家
        visualJudge.value = judges.value.length ? judges.value[0].name : "";
        const rubricJudges = judges.value
          .filter((j) => j.persona !== "product_expert")
          .map((j) => j.name);
        return rubricJudges.length ? rubricJudges : (judges.value.length ? [judges.value[0].name] : []);
      }
      return judges.value.length ? [judges.value[0].name] : [];
    }

    function terminalUserJudge() {
      return judges.value.find(
        (judge) => String(judge.display || "").trim() === "终端用户",
      ) || judges.value.find((judge) => judge.persona === "end_user");
    }

    function disconnectSSE() {
      if (activeEventSource) {
        activeEventSource.close();
        activeEventSource = null;
      }
    }

    function resetEvaluationView() {
      disconnectSSE();
      taskId.value = "";
      running.value = false;
      progress.value = 0;
      total.value = 0;
      results.value = [];
      summary.value = null;
      itemProgress.value = {};
      progressEvents.value = {};
      barChartRefs.value = [];
      activeSkill.value = "";
      resultQuery.value = "";
      correctnessFilter.value = "";
      problemDimFilter.value = "";
      resultPage.value = 1;
      progressPage.value = 1;
      runError.value = "";
    }

    function switchMode(k) {
      if (k === mode.value) return;
      resetEvaluationView();
      mode.value = k;
      selectedJudges.value = defaultJudgeSelection(k);
      items.value = [];
      previewPage.value = 1;
      progressPage.value = 1;
      errors.value = [];
      fileText.value = "";
      isJsonl.value = false;
      datasetName.value = "";
      if (["operation", "rich_content", "rich_content_quality"].includes(k)) {
        opItems.value = [newOpItem()];
        opPage.value = 1;
        opJumpPage.value = "";
      }
    }

    function onFile(e) {
      const f = e.target.files[0];
      if (!f) return;
      resetEvaluationView();
      datasetName.value = f.name || "";
      const r = new FileReader();
      r.onload = () => {
        fileText.value = r.result;
        text.value = r.result;
        isJsonl.value = true;
      };
      r.readAsText(f, "utf-8");
    }

    // —— 任务类（录屏）评测：逐题卡片（query + 可选 context + 视频上传 + 可选 agent 自述）——
    function newOpItem() {
      return { _uiKey: ++opItemSequence, id: "", query: "", context: "", category: "", videoName: "", videoPath: "", frames: [], frameCount: 0, duration: 0, answer: "", taskStartTime: null, taskEndTime: null, contentStartTime: null, contentEndTime: null, sourceLine: null, sourceData: null, uploading: false, uploadError: "" };
    }
    function addOpItem() {
      opItems.value.push(newOpItem());
      opPage.value = Math.ceil(opItems.value.length / opPageSize);
      opJumpPage.value = "";
    }
    function removeOpItem(i) {
      if (opItems.value.length <= 1) return;
      opItems.value.splice(i, 1);
      opPage.value = Math.min(opPage.value, Math.max(1, Math.ceil(opItems.value.length / opPageSize)));
      opJumpPage.value = "";
    }
    async function uploadVideo(i, file) {
      const it = opItems.value[i];
      if (!file) return;
      if (file.size > 20 * 1024 * 1024) { it.uploadError = "视频超过 20MB 限制"; return; }
      it.uploading = true; it.uploadError = "";
      const fd = new FormData(); fd.append("file", file);
      try {
        const r = await fetch(`/api/upload/video?mode=${encodeURIComponent(mode.value)}`, { method: "POST", body: fd });
        if (!r.ok) { it.uploadError = "上传失败 " + r.status; return; }
        const d = await r.json();
        it.videoName = file.name;
        it.videoPath = d.video_path;
        it.frames = d.frames || [];
        it.frameCount = d.frame_count || 0;
        it.duration = d.duration || 0;
      } catch (e) {
        it.uploadError = "上传出错：" + e;
      } finally {
        it.uploading = false;
      }
    }
    function onOpVideo(e, i) { uploadVideo(i, e.target.files[0]); e.target.value = ""; }
    function onOpDrop(e, i) {
      e.preventDefault();
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) uploadVideo(i, f);
    }

    async function onOpManifestFile(e) {
      const file = e.target.files && e.target.files[0];
      e.target.value = "";
      if (!file) return;
      resetEvaluationView();
      datasetName.value = file.name || "";
      opPreparing.value = true;
      errors.value = [];
      items.value = [];
      opItems.value = [newOpItem()];
      opPage.value = 1;
      opJumpPage.value = "";
      try {
        const content = await file.text();
        const parseResponse = await fetch("/api/parse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: mode.value, jsonl: content }),
        });
        const parsed = await parseResponse.json().catch(() => ({}));
        if (!parseResponse.ok) throw new Error(parsed.detail || "JSONL 解析请求失败");
        const importErrors = [...(parsed.errors || [])];
        if (!(parsed.items || []).length) {
          errors.value = importErrors.length ? importErrors : ["JSONL 中没有可导入的数据"];
          return;
        }

        errors.value = importErrors;
        const imported = parsed.items || [];
        if (imported.length) {
          items.value = imported;
          opItems.value = imported.map((item) => ({
            ...newOpItem(),
            id: item.id || "",
            query: item.query || "",
            context: item.context || "",
            category: item.category === "default" ? "" : (item.category || ""),
            videoName: String(item.video_path || "").split(/[\\/]/).pop(),
            videoPath: item.video_path || "",
            answer: (mode.value === "rich_content" || mode.value === "rich_content_quality") ? (item.answer_text || "") : (item.answer || ""),
            taskStartTime: item.task_start_time ?? null,
            taskEndTime: item.task_end_time ?? null,
            contentStartTime: item.content_start_time ?? null,
            contentEndTime: item.content_end_time ?? null,
            sourceLine: item.source_line ?? null,
            sourceData: item.source_data || null,
          }));
          opPage.value = 1;
        }
      } catch (error) {
        errors.value = ["批量导入失败：" + (error?.message || String(error))];
      } finally {
        opPreparing.value = false;
      }
    }

    const canSubmit = computed(() => {
      if (isVideoMode.value)
        return !opPreparing.value && opItems.value.some(
          (it) => it.query.trim() && ((it.frames || []).length || it.videoPath)
        );
      return !!text.value;
    });

    async function doParse() {
      const body = { mode: mode.value };
      if (isJsonl.value && fileText.value) body.jsonl = fileText.value;
      else body.text = text.value;
      const r = await fetch("/api/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      items.value = d.items;
      errors.value = d.errors;
      previewPage.value = 1;
      if (errors.value.length) console.log("解析错误：", errors.value);
    }

    async function submit() {
      runError.value = "";
      if (isVideoMode.value) {
        const valid = opItems.value.filter(
          (it) => it.query.trim() && ((it.frames || []).length || it.videoPath)
        );
        if (!valid.length) {
          alert("请为每题填写 query，并提供视频路径或上传视频后再评估。");
          return;
        }
        items.value = valid.map((it, idx) => {
          const item = {
            id: it.id || `${mode.value === "operation" ? "op" : "rich"}${idx + 1}`,
            query: it.query.trim(),
            context: (it.context || "").trim(),
            video_path: it.videoPath,
          };
          if (mode.value === "operation") {
            item.category = "operation";
            item.answer = (it.answer || "").trim();
          } else {
            // rich_content / rich_content_quality
            item.category = (it.category || "").trim() || "default";
            item.answer_text = (it.answer || "").trim();
          }
          if ((it.frames || []).length) {
            item.media = [it.videoPath];
            item.frames = it.frames;
          }
          if (Number.isFinite(it.taskStartTime)) item.task_start_time = it.taskStartTime;
          if (Number.isFinite(it.taskEndTime)) item.task_end_time = it.taskEndTime;
          if (Number.isFinite(it.contentStartTime)) item.content_start_time = it.contentStartTime;
          if (Number.isFinite(it.contentEndTime)) item.content_end_time = it.contentEndTime;
          if (Number.isFinite(it.sourceLine)) item.source_line = it.sourceLine;
          if (it.sourceData) item.source_data = it.sourceData;
          return item;
        });
        errors.value = [];
      } else {
        // 自动解析最新输入（用户可跳过手动"解析预览"）
        await doParse();
        if (!items.value.length) {
          alert("解析后没有可评估的题。请检查格式：每行『问题 ||| 回答』。");
          return;
        }
      }
      results.value = [];
      summary.value = null;
      progressEvents.value = {};
      barChartRefs.value = [];
      activeSkill.value = "";
      resultQuery.value = "";
      correctnessFilter.value = "";
      problemDimFilter.value = "";
      resultPage.value = 1;
      progress.value = 0;
      total.value = items.value.length;
      itemProgress.value = Object.fromEntries(
        items.value.map((item, index) => [
          index,
          {
            item_index: index,
            item_id: item.id || `q${index}`,
            status: "pending",
            percent: 0,
            message: "排队中",
            stage_rank: 0,
          },
        ])
      );
      running.value = true;
      const body = {
        mode: mode.value,
        items: items.value,
        dataset_name: datasetName.value || (isVideoMode.value ? "手动录入" : (isJsonl.value ? "未命名数据集.jsonl" : "文本输入")),
        options: {
          judges: mode.value === "operation"
            ? defaultJudgeSelection("operation")
            : selectedJudges.value,
          visual_judge: visualJudge.value,
          model: selectedModel.value,
          concurrency: concurrency.value,
          eval_timeout_s: evalTimeout.value,
        },
      };
      let r;
      try {
        r = await fetch("/api/eval", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch (error) {
        running.value = false;
        itemProgress.value = {};
        runError.value = "无法启动评估：" + (error?.message || "网络错误");
        return;
      }
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.task_id) {
        running.value = false;
        itemProgress.value = {};
        const detail = typeof d.detail === "string" ? d.detail : "服务端拒绝了评估请求";
        runError.value = "无法启动评估：" + detail;
        return;
      }
      taskId.value = d.task_id;
      loadHistory();
      connectSSE();
    }

    async function reconcileTaskAfterError(message) {
      let snapshot = null;
      try {
        const response = await fetch(`/api/history/${taskId.value}`);
        if (response.ok) snapshot = await response.json();
      } catch (_) {}
      const snapshotResults = snapshot?.results || results.value;
      const resultByIndex = new Map(snapshotResults.map((entry) => [entry.index, entry]));
      const snapshotProgress = snapshot?.item_progress || {};
      progressEvents.value = snapshot?.progress_events || progressEvents.value;
      const reconciled = {};
      items.value.forEach((item, index) => {
        const previous = itemProgress.value[index] || {};
        const remote = snapshotProgress[index] || snapshotProgress[String(index)] || {};
        const result = resultByIndex.get(index);
        let status = remote.status || previous.status || "error";
        let rowMessage = remote.message || previous.message || "";
        if (result) {
          status = result.error ? "error" : "done";
          rowMessage = result.error ? "评测失败" : "评测完成";
        } else if (status !== "done" && status !== "error") {
          status = "error";
          rowMessage = `任务中断：${message}`;
        }
        const updatedAt = Date.parse(remote.updated_at || "");
        reconciled[index] = {
          ...previous,
          ...remote,
          status,
          message: rowMessage,
          percent: status === "done" || status === "error" ? 100 : (remote.percent ?? previous.percent ?? 0),
          stage_rank: status === "done" ? 4 : (remote.stage_rank ?? previous.stage_rank ?? 0),
          finished_at: previous.finished_at
            || (Number.isFinite(updatedAt) ? updatedAt : Date.now()),
        };
      });
      results.value = snapshotResults;
      progress.value = snapshotResults.length;
      itemProgress.value = reconciled;
      if (snapshot?.summary) summary.value = snapshot.summary;
    }

    function connectSSE() {
      disconnectSSE();
      const connectedTaskId = taskId.value;
      if (!connectedTaskId) return;
      const es = new EventSource(`/api/eval/${connectedTaskId}/stream`);
      activeEventSource = es;
      es.addEventListener("item_progress", (e) => {
        if (taskId.value !== connectedTaskId) return;
        const d = JSON.parse(e.data);
        mergeItemProgress(d);
      });
      es.addEventListener("progress_event", (e) => {
        if (taskId.value !== connectedTaskId) return;
        appendProgressEvent(JSON.parse(e.data));
      });
      es.addEventListener("result", (e) => {
        if (taskId.value !== connectedTaskId) return;
        const d = JSON.parse(e.data);
        const existing = results.value.findIndex((entry) => entry.index === d.result.index);
        if (existing >= 0) {
          results.value = results.value.map((entry, index) => index === existing ? d.result : entry);
        } else {
          results.value = [...results.value, d.result];
        }
        progress.value = d.progress;
        const index = d.result.index;
        if (index != null) {
          const previous = itemProgress.value[index] || {};
          itemProgress.value = {
            ...itemProgress.value,
            [index]: {
              ...previous,
              status: d.result.error ? "error" : "done",
              percent: 100,
              message: d.result.error ? "评测失败" : "评测完成",
              stage_rank: d.result.error ? (previous.stage_rank ?? 0) : 4,
              finished_at: Date.now(),
            },
          };
        }
      });
      es.addEventListener("done", async () => {
        if (taskId.value !== connectedTaskId) return;
        running.value = false;
        es.close();
        if (activeEventSource === es) activeEventSource = null;
        await loadHistoryTask(connectedTaskId, false);
        await loadHistory();
      });
      es.addEventListener("error", async (e) => {
        if (taskId.value !== connectedTaskId) return;
        // 原生 EventSource 网络错误没有 data，让浏览器按协议自动重连并回放状态。
        if (!e.data) return;
        let message = "未知错误";
        try {
          const d = JSON.parse(e.data);
          message = d.message || message;
        } catch (_) {}
        running.value = false;
        es.close();
        if (activeEventSource === es) activeEventSource = null;
        await reconcileTaskAfterError(message);
        runError.value = "评估出错：" + message;
        await loadHistory();
      });
      es.addEventListener("cancelled", async (e) => {
        if (taskId.value !== connectedTaskId) return;
        let message = "任务已中断";
        try {
          message = JSON.parse(e.data).message || message;
        } catch (_) {}
        running.value = false;
        es.close();
        if (activeEventSource === es) activeEventSource = null;
        await loadHistoryTask(connectedTaskId, false);
        runError.value = message;
        await loadHistory();
      });
    }

    function isNA(r, dim) {
      return r.na_dimensions && r.na_dimensions.includes(dim);
    }
    function cellTitle(r, c) {
      // 维度列 hover 显示该维度的打分理由（rubric_reasons）；N/A 维度显示"不适用"
      if (c.rubricDim && isNA(r, c.rubricDim)) {
        return "[不适用] " + (r.rubric_reasons && r.rubric_reasons[c.rubricDim]
          ? r.rubric_reasons[c.rubricDim] : "该维度与本题/本答案无关");
      }
      if (c.rubricDim && r.rubric_reasons && r.rubric_reasons[c.rubricDim]) {
        return r.rubric_reasons[c.rubricDim];
      }
      return "";
    }
    function cell(r, c) {
      const v = r[c.key];
      if (c.rubricDim) {
        if (isNA(r, c.rubricDim)) return "N/A";
        return r.rubric && r.rubric[c.rubricDim] != null ? r.rubric[c.rubricDim] : "";
      }
      if (c.key === "category") return r.category_display || (!v || v === "default" ? "通用" : v);
      if (c.key === "agree") {
        if (v === undefined) return "";
        return v === true ? "✓ 一致" : v === false ? "✗ 不一致" : "?";
      }
      if (c.key === "used_search") return v ? "是" : "否";
      if (c.key === "is_low_level") return v === "yes" ? "是" : "否";
      if (c.key === "latency_s") return v != null ? v + "秒" : "";
      if (c.key === "truncated") return v ? "⚠️是(强制判定)" : "";
      if (c.key === "arbitrated") return v ? `⚖️是(${r.arbitrator_confidence ?? "-"})` : "";
      if (c.key === "bidirectional_consistent") return v ? "是" : "否(位置偏差)";
      if (c.key === "winner") return v === "a" ? "A" : v === "b" ? "B" : "平";
      if (c.key === "correctness") {
        if (mode.value === "operation")
          return ({ ok: "✓ 完成", nok: "✗ 未完成或执行错误", no_support: "⊘ 客观条件不支持", others: "? 其他" }[v] || v) || "";
        return ({ right: "正确", wrong: "错误", partial: "部分", unclear: "不清" }[v] || v) || "";
      }
      if (["issue_types", "card_types", "card_contents", "superlink_texts"].includes(c.key)) {
        return Array.isArray(v) ? v.join("；") : (v || "");
      }
      if (c.key === "card_presence" || c.key === "superlink_presence") {
        return ({ present: "是", absent: "否", unclear: "不清楚" }[v] || v) || "";
      }
      if (c.key === "card_suitability" || c.key === "superlink_suitability") {
        return ({
          ok: "OK",
          nok: "NOK",
          suitable: "合适",
          partially_suitable: "部分合适",
          unsuitable: "不合适",
          unclear: "不确定",
          not_applicable: "N/A",
        }[v] || v) || "";
      }
      if (c.key === "problem_solved") {
        return ({ ok: "OK", nok: "NOK", need_review: "需复查" }[v] || v) || "";
      }
      if (c.key === "answer_coverage") {
        return ({ complete: "完整", partial: "部分", unclear: "不确定" }[v] || v) || "";
      }
      if (c.key === "superlink_count_type") {
        return ({ exact: "精确", lower_bound: "至少", unknown: "未知" }[v] || v) || "";
      }
      if (c.key === "needs_review") return v ? "T" : "F";
      if (v == null) return "";
      return v;
    }

    function showCellTooltip(event, value) {
      const text = value == null ? "" : String(value);
      if (!text || text.length < 12) return;
      if (tooltipHideTimer) clearTimeout(tooltipHideTimer);
      const rect = event.currentTarget.getBoundingClientRect();
      const width = Math.min(560, Math.max(260, window.innerWidth - 24));
      const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
      const estimatedHeight = Math.min(360, Math.max(80, Math.ceil(text.length / 30) * 22));
      const below = rect.bottom + 8;
      const top = below + estimatedHeight < window.innerHeight
        ? below
        : Math.max(12, rect.top - estimatedHeight - 8);
      cellTooltip.value = {
        visible: true,
        text,
        style: { left: `${left}px`, top: `${top}px`, width: `${width}px` },
      };
    }

    function scheduleHideCellTooltip() {
      tooltipHideTimer = setTimeout(() => {
        cellTooltip.value.visible = false;
      }, 120);
    }

    function keepCellTooltip() {
      if (tooltipHideTimer) clearTimeout(tooltipHideTimer);
    }

    function hideCellTooltip() {
      cellTooltip.value.visible = false;
    }

    function setBarRef(el, i) {
      if (el) barChartRefs.value[i] = el;
    }

    function renderCharts() {
      nextTick(() => {
        const bs = summary.value && summary.value.by_skill;
        if (!bs || !bs.overview) return;
        // 饼图：垂域样本量分布
        const pieData = bs.overview.filter((s) => s.n_items > 0).map((s) => ({ name: s.display, value: s.n_items }));
        if (pieChart.value && pieData.length) {
          echarts.init(pieChart.value).setOption({
            tooltip: { trigger: "item", formatter: "{b}: {c} 题 ({d}%)" },
            legend: { bottom: 0, type: "scroll" },
            title: { text: "垂域样本分布", left: "center", textStyle: { fontSize: 13 } },
            series: [{ type: "pie", radius: ["30%", "60%"], center: ["50%", "48%"], data: pieData }],
          });
        }
        // 各垂域维度问题分布：两列卡片中的竖向柱状图
        (bs.sections || []).forEach((s, i) => {
          const el = barChartRefs.value[i];
          if (!el || !s.n_items) return;
          const dpd = s.dim_problem_dist || {};
          const dims = Object.keys(dpd).filter((d) => dpd[d].rate > 0);
          if (!dims.length) return;
          const chart = echarts.getInstanceByDom(el) || echarts.init(el);
          chart.setOption({
            tooltip: {
              trigger: "axis",
              formatter: (ctx) => {
                const d = dims[ctx[0].dataIndex];
                const allIds = dpd[d].item_ids || [];
                const shownIds = allIds.slice(0, 5);
                const count = dpd[d].count ?? allIds.length;
                const preview = shownIds.length ? `<br/>示例题号：${shownIds.join(", ")}` : "";
                return `${d}：${(ctx[0].value * 100).toFixed(0)}%<br/>问题题目：${count} 题${preview}<br/><span style="color:#9ca3af">点击柱子查看完整明细</span>`;
              },
            },
            grid: { left: 48, right: 18, top: 42, bottom: 62 },
            title: { text: `${s.display} 维度问题占比（N=${s.n_items}）`, left: "center", textStyle: { fontSize: 12 } },
            xAxis: {
              type: "category",
              data: dims,
              axisLabel: { interval: 0, rotate: dims.length > 3 ? 24 : 0, fontSize: 11 },
            },
            yAxis: { type: "value", max: 1, axisLabel: { formatter: (v) => v * 100 + "%" } },
            series: [
              {
                type: "bar",
                data: dims.map((d) => dpd[d].rate),
                itemStyle: { color: "#e6a23c" },
                emphasis: { itemStyle: { color: "#d97706" } },
                cursor: "pointer",
                label: { show: true, position: "top", formatter: (ctx) => (ctx.value * 100).toFixed(0) + "%" },
              },
            ],
          });
          chart.off("click");
          chart.on("click", (params) => {
            const dimension = dims[params.dataIndex];
            if (dimension) drillDownDimension(s.skill, dimension);
          });
        });
      });
    }

    function formatTime(ts) {
      if (!ts) return "";
      const d = new Date(ts * 1000);
      if (Number.isNaN(d.getTime())) return String(ts);
      return d.toLocaleString();
    }

    async function loadHistory() {
      loadingHistory.value = true;
      try {
        const r = await fetch("/api/history?limit=0");
        const d = await r.json();
        historyItems.value = d.items || [];
        historyPage.value = Math.min(historyPage.value, historyPageCount.value);
        historyJumpPage.value = "";
        historyNoteDrafts.value = Object.fromEntries(
          historyItems.value.map((item) => [item.task_id, item.note || ""]),
        );
        historyNoteEditing.value = {};
      } finally {
        loadingHistory.value = false;
      }
    }

    function editHistoryNote(item) {
      historyNoteDrafts.value[item.task_id] = item.note || "";
      historyNoteEditing.value[item.task_id] = true;
    }

    function cancelHistoryNote(item) {
      historyNoteDrafts.value[item.task_id] = item.note || "";
      historyNoteEditing.value[item.task_id] = false;
    }

    async function saveHistoryNote(item) {
      const note = String(historyNoteDrafts.value[item.task_id] || "").trim();
      const response = await fetch(`/api/history/${item.task_id}/note`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        alert("备注保存失败：" + (data.detail || "未知错误"));
        return;
      }
      item.note = data.note || "";
      historyNoteDrafts.value[item.task_id] = item.note;
      historyNoteEditing.value[item.task_id] = false;
    }

    async function delHistory(id) {
      if (!confirm("确认删除这条历史记录？删除后不可恢复。")) return;
      const r = await fetch(`/api/history/${id}`, { method: "DELETE" });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        alert("删除失败：" + (data.detail || "未知错误"));
        return;
      }
      if (taskId.value === id) {
        taskId.value = "";
        results.value = [];
        summary.value = null;
      }
      await loadHistory();
    }

    function isActiveHistoryStatus(status) {
      return status === "pending" || status === "running";
    }

    function historyStatusLabel(status) {
      return ({
        pending: "等待中",
        running: "评估中",
        done: "已完成",
        error: "失败",
        cancelled: "已中断",
      }[status] || status || "未知");
    }

    async function cancelHistoryTask(item) {
      if (!confirm(`确认中断「${item.dataset_name || item.task_id}」的批跑？已完成结果会保留。`)) return;
      const response = await fetch(`/api/eval/${item.task_id}/cancel`, { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        alert("中断失败：" + (data.detail || "未知错误"));
        await loadHistory();
        return;
      }
      if (taskId.value === item.task_id) {
        await loadHistoryTask(item.task_id, false);
        runError.value = "用户手动中断批跑";
      }
      await loadHistory();
    }

    async function loadHistoryTask(id, scrollToResults = true) {
      const r = await fetch(`/api/history/${id}`);
      if (!r.ok) {
        alert("历史记录加载失败");
        return;
      }
      const d = await r.json();
      disconnectSSE();
      taskId.value = d.task_id || id;
      mode.value = d.mode || mode.value;
      datasetName.value = d.dataset_name || "";
      items.value = d.items || [];
      results.value = d.results || [];
      itemProgress.value = d.item_progress || {};
      progressEvents.value = d.progress_events || {};
      summary.value = d.summary || null;
      total.value = items.value.length || results.value.length;
      progress.value = d.done_total ?? results.value.length;
      running.value = isActiveHistoryStatus(d.status);
      runError.value = d.status === "cancelled"
        ? (d.error || "任务已中断")
        : d.status === "error"
          ? (d.error ? `评估出错：${d.error}` : "评估出错")
          : "";
      activeSkill.value = "";
      resultQuery.value = "";
      correctnessFilter.value = "";
      problemDimFilter.value = "";
      resultPage.value = 1;
      progressPage.value = 1;
      barChartRefs.value = [];
      const options = d.options || {};
      if (Array.isArray(options.judges) && options.judges.length) selectedJudges.value = options.judges;
      if (options.visual_judge) visualJudge.value = options.visual_judge;
      if (options.model) selectedModel.value = options.model;
      if (mode.value !== "compare" && skillTabs.value.length) activeSkill.value = skillTabs.value[0].key;
      renderCharts();
      if (running.value) connectSSE();
      if (scrollToResults) {
        nextTick(() => resultBrowser.value && resultBrowser.value.scrollIntoView({ behavior: "smooth", block: "start" }));
      }
    }

    function exportCsv() {
      window.open(`/api/eval/${taskId.value}/export?format=csv`);
    }
    function exportJson() {
      window.open(`/api/eval/${taskId.value}/export?format=json`);
    }
    function exportXlsx() {
      window.open(`/api/eval/${taskId.value}/export?format=xlsx`);
    }
    function exportFrames() {
      window.open(`/api/eval/${taskId.value}/export?format=frames_zip`);
    }
    function resultWarnings(result) {
      const direct = result?.video_prepare_warnings;
      if (Array.isArray(direct) && direct.length) return direct.filter(Boolean).map(String);
      const index = Number(result?.index);
      if (!Number.isInteger(index) || index < 0) return [];
      const itemWarnings = items.value[index]?.video_prepare_warnings;
      return Array.isArray(itemWarnings) ? itemWarnings.filter(Boolean).map(String) : [];
    }
    function itemArtifactUrl(result, format) {
      const index = Number(result && result.index);
      if (!taskId.value || !Number.isInteger(index) || index < 0) return "";
      return `/api/eval/${taskId.value}/items/${index}/export?format=${encodeURIComponent(format)}`;
    }

    onMounted(async () => {
      progressClockTimer = window.setInterval(() => {
        clockNow.value = Date.now();
      }, 1000);
      const r = await fetch("/api/config");
      const d = await r.json();
      judges.value = d.judges;
      models.value = d.models;
      selectedJudges.value = defaultJudgeSelection(mode.value);
      selectedModel.value = d.models[0] || "";
      loadHistory();
    });

    onUnmounted(() => {
      disconnectSSE();
      if (progressClockTimer != null) window.clearInterval(progressClockTimer);
    });

    return {
      workspacePage,
      modes, mode, modeLabel, isVideoMode, text, items, errors, judges, visibleJudges, models, selectedJudges, visualJudge, selectedModel, datasetName,
      concurrency, evalTimeout, running, progress, total, results, summary, taskId, runError,
      itemProgress, progressEvents, progressRows, pagedProgressRows, progressStages,
      historyItems, pagedHistoryItems, historyNoteDrafts, historyNoteEditing, loadingHistory, pageSize,
      historyPage, historyPageSize, historyPageCount, historyJumpPage,
      opPage, opPageSize, opPageCount, opJumpPage,
      previewPage, previewPageCount, previewJumpPage,
      progressPage, progressPageCount, progressJumpPage,
      resultJumpPage,
      pieChart, barChartRefs, resultBrowser, setBarRef, renderCharts,
      activeSkill, resultQuery, correctnessFilter, problemDimFilter, resultPage, resultPageSize,
      skillTabs, rubricDims, filteredResults, pagedResults, pageCount, resultTableWidth, fallbackStat,
      formatHint, placeholder, previewKeys, pagedPreviewItems, skillOverviewRows, resultCols, opItems, pagedOpItems, opPreparing, canSubmit,
      trunc, switchMode, onFile, onOpManifestFile, doParse, submit, cell, cellTitle, isNA, columnWidth, isFrozenResultColumn, frozenResultColumnStyle, exportCsv, exportJson, exportXlsx, exportFrames, resultWarnings, itemArtifactUrl, addOpItem, removeOpItem, onOpVideo, onOpDrop,
      loadHistory, loadHistoryTask, delHistory, cancelHistoryTask,
      editHistoryNote, cancelHistoryNote, saveHistoryNote, formatTime,
      isActiveHistoryStatus, historyStatusLabel,
      selectSkill, drillDownDimension, clearDimensionDrillDown, resetResultPage, changePage,
      changePreviewPage, changeProgressPage, changeOpPage, changeHistoryPage,
      changeResultPageSize, changeHistoryPageSize, paginationPages, setTablePage, jumpTablePage,
      progressStageClass, progressDisplay, progressStageLabel, progressStatusClass,
      progressMeta, formatProgressEventTime, progressEventMeta, progressEventMessage, scrollProgressLog,
      formatProgressElapsed, shortRequestId, copyRequestId,
      cellTooltip, showCellTooltip, scheduleHideCellTooltip, keepCellTooltip, hideCellTooltip,
      knowledgePublished, knowledgeDraft, knowledgeCategoryKey, knowledgeHasDraft, knowledgeBusy,
      knowledgeMessage, knowledgeError, selectedKnowledgeCategory, knowledgeRuleCount,
      knowledgePromptPreview, openKnowledgePage, loadKnowledge, saveKnowledgeDraft, publishKnowledge,
      discardKnowledgeDraft, addKnowledgeCategory, removeKnowledgeCategory, addKnowledgeRule,
      removeKnowledgeRule, moveKnowledgeRule,
    };
  },
}).mount("#app");
