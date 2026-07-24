import { createApp, ref, computed, onMounted, onUnmounted, nextTick } from "https://unpkg.com/vue@3/dist/vue.esm-browser.js";
import * as echarts from "https://unpkg.com/echarts@5/dist/echarts.esm.min.js";

createApp({
  setup() {
    const modes = [
      { key: "single", label: "单回答盲评" },
      { key: "compare", label: "两回答对比" },
      { key: "online", label: "接模型在线评估" },
      { key: "process", label: "过程盲评(含轨迹)" },
      { key: "operation", label: "操作类(录屏)" },
      { key: "rich_content", label: "垂域挂卡 / Superlink" },
    ];
    const mode = ref("single");
    const isVideoMode = computed(() => ["operation", "rich_content"].includes(mode.value));
    const text = ref("");
    const fileText = ref("");
    const isJsonl = ref(false);
    const items = ref([]);
    const opItems = ref([newOpItem()]);
    const opPreparing = ref(false);
    const errors = ref([]);
    const judges = ref([]);
    const models = ref([]);
    const selectedJudges = ref([]);
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
    const previewPage = ref(1);
    const progressPage = ref(1);
    const resultJumpPage = ref("");
    const previewJumpPage = ref("");
    const progressJumpPage = ref("");
    const cellTooltip = ref({ visible: false, text: "", style: {} });
    const historyItems = ref([]);
    const loadingHistory = ref(false);
    const clockNow = ref(Date.now());
    let tooltipHideTimer = null;
    let progressClockTimer = null;
    const pageSize = 10;
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
    const skillOverviewRows = computed(() => summary.value?.by_skill?.overview || []);

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
      const terminal = incoming.status === "done" || incoming.status === "error";
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
      if (stageIndex === row.stageRank) return row.status === "error" ? "error" : "active";
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
      if (row.status === "done") return "完成";
      return progressStages[Math.max(0, Math.min(4, row.stageRank))];
    }

    function progressStatusClass(row) {
      if (row.status === "error") return "status-error";
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
      if (status === "done" || status === "error") {
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
        const current = map.get(key) || { key, label: r.category_display || key, count: 0 };
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
          { key: "card_presence", label: "挂卡" },
          { key: "card_count", label: "挂卡数" },
          { key: "card_types", label: "挂卡类型" },
          { key: "card_contents", label: "挂卡内容" },
          { key: "card_suitability", label: "挂卡适配性" },
          { key: "card_suitability_score", label: "适配分" },
          { key: "superlink_presence", label: "Superlink" },
          { key: "superlink_count", label: "链接数" },
          { key: "superlink_count_type", label: "计数类型" },
          { key: "superlink_texts", label: "链接文字" },
          { key: "answer_coverage", label: "回答覆盖" },
          { key: "needs_review", label: "需人工复核" },
          { key: "rationale", label: "识别结论" },
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
        { key: "rationale", label: "理由" },
        { key: "latency_s", label: "耗时" },
      ];
    });

    function columnWidth(c) {
      const compact = c.rubricDim
        || ["correctness", "winner", "total", "used_search", "truncated", "arbitrated", "agree", "latency_s", "bidirectional_consistent"].includes(c.key);
      const textColumn = ["query", "context", "answer", "generated_answer", "answer_a", "answer_b", "rationale"].includes(c.key);
      const minWidth = compact ? 88 : c.key === "item_id" ? 90 : textColumn ? 180 : 110;
      const maxWidth = compact ? 130 : c.key === "rationale" ? 420 : textColumn ? 360 : 220;
      const visualLength = (value) => Array.from(String(value ?? "")).reduce(
        (sum, char) => sum + (char.charCodeAt(0) > 255 ? 2 : 1),
        0,
      );
      const sampleLengths = results.value.slice(0, 100).map((result) => visualLength(cell(result, c)));
      const desired = (Math.max(visualLength(c.label), ...sampleLengths, 1) * 7) + 28;
      return Math.max(minWidth, Math.min(maxWidth, desired));
    }

    const resultTableWidth = computed(
      () => 48 + resultCols.value.reduce((sum, c) => sum + columnWidth(c), 0)
    );

    const filteredResults = computed(() => {
      const q = resultQuery.value.trim().toLowerCase();
      const threshold = (summary.value && summary.value.by_skill && summary.value.by_skill.threshold) || 2;
      return skillResults.value.filter((r) => {
        if (correctnessFilter.value && r.correctness !== correctnessFilter.value) return false;
        if (problemDimFilter.value && (r.rubric || {})[problemDimFilter.value] > threshold) return false;
        if (problemDimFilter.value && (r.rubric || {})[problemDimFilter.value] == null) return false;
        if (q && !`${r.item_id || ""} ${r.query || ""} ${r.context || ""} ${r.answer || ""} ${(r.card_contents || []).join(" ")} ${(r.superlink_texts || []).join(" ")} ${r.rationale || ""}`.toLowerCase().includes(q)) return false;
        return true;
      });
    });

    const pageCount = computed(() => Math.max(1, Math.ceil(filteredResults.value.length / pageSize)));
    const pagedResults = computed(() => {
      const safePage = Math.min(resultPage.value, pageCount.value);
      const start = (safePage - 1) * pageSize;
      return filteredResults.value.slice(start, start + pageSize);
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
        preview: [previewPage, previewPageCount, previewJumpPage],
        progress: [progressPage, progressPageCount, progressJumpPage],
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
    function changePreviewPage(delta) {
      setTablePage("preview", previewPage.value + delta);
    }
    function changeProgressPage(delta) {
      setTablePage("progress", progressPage.value + delta);
    }
    function jumpTablePage(kind) {
      const jumpValues = {
        result: resultJumpPage.value,
        preview: previewJumpPage.value,
        progress: progressJumpPage.value,
      };
      setTablePage(kind, jumpValues[kind]);
    }

    function trunc(v) {
      if (v == null) return "";
      const s = String(v);
      return s.length > 50 ? s.slice(0, 50) + "…" : s;
    }

    function defaultJudgeSelection(targetMode) {
      if (["operation", "rich_content"].includes(targetMode)) {
        const endUserJudge = judges.value.find((judge) => judge.persona === "end_user");
        if (endUserJudge) return [endUserJudge.name];
      }
      return judges.value.length ? [judges.value[0].name] : [];
    }

    function switchMode(k) {
      mode.value = k;
      selectedJudges.value = defaultJudgeSelection(k);
      items.value = [];
      previewPage.value = 1;
      progressPage.value = 1;
      errors.value = [];
      fileText.value = "";
      isJsonl.value = false;
      if (["operation", "rich_content"].includes(k)) opItems.value = [newOpItem()];
    }

    function onFile(e) {
      const f = e.target.files[0];
      if (!f) return;
      const r = new FileReader();
      r.onload = () => {
        fileText.value = r.result;
        text.value = r.result;
        isJsonl.value = true;
      };
      r.readAsText(f, "utf-8");
    }

    // —— 操作类评测：逐题卡片（query + 可选 context + 视频上传 + 可选 agent 自述）——
    function newOpItem() {
      return { id: "", query: "", context: "", category: "", videoName: "", videoPath: "", frames: [], frameCount: 0, duration: 0, answer: "", taskStartTime: null, taskEndTime: null, contentStartTime: null, contentEndTime: null, uploading: false, uploadError: "" };
    }
    function addOpItem() { opItems.value.push(newOpItem()); }
    function removeOpItem(i) { if (opItems.value.length > 1) opItems.value.splice(i, 1); }
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
      opPreparing.value = true;
      errors.value = [];
      items.value = [];
      opItems.value = [newOpItem()];
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
            answer: mode.value === "rich_content" ? (item.answer_text || "") : (item.answer || ""),
            taskStartTime: item.task_start_time ?? null,
            taskEndTime: item.task_end_time ?? null,
            contentStartTime: item.content_start_time ?? null,
            contentEndTime: item.content_end_time ?? null,
          }));
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
        options: {
          judges: selectedJudges.value,
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
      const es = new EventSource(`/api/eval/${taskId.value}/stream`);
      es.addEventListener("item_progress", (e) => {
        const d = JSON.parse(e.data);
        mergeItemProgress(d);
      });
      es.addEventListener("progress_event", (e) => {
        appendProgressEvent(JSON.parse(e.data));
      });
      es.addEventListener("result", (e) => {
        const d = JSON.parse(e.data);
        results.value.push(d.result);
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
      es.addEventListener("done", (e) => {
        summary.value = JSON.parse(e.data).summary;
        if (mode.value !== "compare" && skillTabs.value.length) activeSkill.value = skillTabs.value[0].key;
        resultPage.value = 1;
        running.value = false;
        es.close();
        renderCharts();
        loadHistory();
      });
      es.addEventListener("error", async (e) => {
        // 原生 EventSource 网络错误没有 data，让浏览器按协议自动重连并回放状态。
        if (!e.data) return;
        let message = "未知错误";
        try {
          const d = JSON.parse(e.data);
          message = d.message || message;
        } catch (_) {}
        running.value = false;
        es.close();
        await reconcileTaskAfterError(message);
        runError.value = "评估出错：" + message;
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
      if (c.key === "latency_s") return v != null ? v + "秒" : "";
      if (c.key === "truncated") return v ? "⚠️是(强制判定)" : "";
      if (c.key === "arbitrated") return v ? `⚖️是(${r.arbitrator_confidence ?? "-"})` : "";
      if (c.key === "bidirectional_consistent") return v ? "是" : "否(位置偏差)";
      if (c.key === "winner") return v === "a" ? "A" : v === "b" ? "B" : "平";
      if (c.key === "correctness") {
        if (mode.value === "operation")
          return ({ right: "✓ 完成", wrong: "✗ 未完成", partial: "◐ 部分/非完美", unclear: "? 无法判断" }[v] || v) || "";
        return ({ right: "正确", wrong: "错误", partial: "部分", unclear: "不清" }[v] || v) || "";
      }
      if (["card_types", "card_contents", "superlink_texts"].includes(c.key)) {
        return Array.isArray(v) ? v.join("；") : (v || "");
      }
      if (c.key === "card_presence" || c.key === "superlink_presence") {
        return ({ present: "有", absent: "无", unclear: "不确定" }[v] || v) || "";
      }
      if (c.key === "card_suitability") {
        return ({
          suitable: "合适",
          partially_suitable: "部分合适",
          unsuitable: "不合适",
          unclear: "不确定",
          not_applicable: "N/A",
        }[v] || v) || "";
      }
      if (c.key === "answer_coverage") {
        return ({ complete: "完整", partial: "部分", unclear: "不确定" }[v] || v) || "";
      }
      if (c.key === "superlink_count_type") {
        return ({ exact: "精确", lower_bound: "至少", unknown: "未知" }[v] || v) || "";
      }
      if (c.key === "needs_review") return v ? "是" : "否";
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
        const r = await fetch("/api/history?limit=50");
        const d = await r.json();
        historyItems.value = d.items || [];
      } finally {
        loadingHistory.value = false;
      }
    }

    async function delHistory(id) {
      if (!confirm("确认删除这条历史记录？删除后不可恢复。")) return;
      const r = await fetch(`/api/history/${id}`, { method: "DELETE" });
      if (!r.ok) {
        alert("删除失败");
        return;
      }
      if (taskId.value === id) {
        taskId.value = "";
        results.value = [];
        summary.value = null;
      }
      await loadHistory();
    }

    async function loadHistoryTask(id) {
      const r = await fetch(`/api/history/${id}`);
      if (!r.ok) {
        alert("历史记录加载失败");
        return;
      }
      const d = await r.json();
      taskId.value = d.task_id || id;
      mode.value = d.mode || mode.value;
      items.value = d.items || [];
      results.value = d.results || [];
      itemProgress.value = d.item_progress || {};
      progressEvents.value = d.progress_events || {};
      summary.value = d.summary || null;
      total.value = items.value.length || results.value.length;
      progress.value = results.value.length;
      running.value = false;
      activeSkill.value = "";
      resultQuery.value = "";
      correctnessFilter.value = "";
      problemDimFilter.value = "";
      resultPage.value = 1;
      progressPage.value = 1;
      barChartRefs.value = [];
      if (mode.value !== "compare" && skillTabs.value.length) activeSkill.value = skillTabs.value[0].key;
      renderCharts();
      nextTick(() => resultBrowser.value && resultBrowser.value.scrollIntoView({ behavior: "smooth", block: "start" }));
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
      if (progressClockTimer != null) window.clearInterval(progressClockTimer);
    });

    return {
      modes, mode, isVideoMode, text, items, errors, judges, models, selectedJudges, selectedModel,
      concurrency, evalTimeout, running, progress, total, results, summary, taskId, runError,
      itemProgress, progressEvents, progressRows, pagedProgressRows, progressStages,
      historyItems, loadingHistory, pageSize,
      previewPage, previewPageCount, previewJumpPage,
      progressPage, progressPageCount, progressJumpPage,
      resultJumpPage,
      pieChart, barChartRefs, resultBrowser, setBarRef, renderCharts,
      activeSkill, resultQuery, correctnessFilter, problemDimFilter, resultPage,
      skillTabs, rubricDims, filteredResults, pagedResults, pageCount, resultTableWidth, fallbackStat,
      formatHint, placeholder, previewKeys, pagedPreviewItems, skillOverviewRows, resultCols, opItems, opPreparing, canSubmit,
      trunc, switchMode, onFile, onOpManifestFile, doParse, submit, cell, cellTitle, isNA, columnWidth, exportCsv, exportJson, exportXlsx, addOpItem, removeOpItem, onOpVideo, onOpDrop,
      loadHistory, loadHistoryTask, delHistory, formatTime,
      selectSkill, drillDownDimension, clearDimensionDrillDown, resetResultPage, changePage,
      changePreviewPage, changeProgressPage, paginationPages, setTablePage, jumpTablePage,
      progressStageClass, progressDisplay, progressStageLabel, progressStatusClass,
      progressMeta, formatProgressEventTime, progressEventMeta, progressEventMessage, scrollProgressLog,
      formatProgressElapsed, shortRequestId, copyRequestId,
      cellTooltip, showCellTooltip, scheduleHideCellTooltip, keepCellTooltip, hideCellTooltip,
    };
  },
}).mount("#app");
