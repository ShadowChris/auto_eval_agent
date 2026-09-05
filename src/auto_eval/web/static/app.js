import { createApp, ref, computed, onMounted, onUnmounted, nextTick } from "https://unpkg.com/vue@3/dist/vue.esm-browser.js";

createApp({
  setup() {
    const modes = [
      { key: "rich_content", label: "垂域视觉评测" },
      { key: "compare", label: "垂域视觉对比" },
    ];
    function modeLabel(key) {
      return modes.find((item) => item.key === key)?.label || key;
    }
    const mode = ref("rich_content");
    const isVideoMode = computed(() => true);
    const datasetName = ref("");
    const items = ref([]);
    let opItemSequence = 0;
    const opItems = ref([newOpItem()]);
    const opPage = ref(1);
    const opJumpPage = ref("");
    const opPreparing = ref(false);
    const errors = ref([]);
    const judges = ref([]);
    const selectedJudges = ref([]);
    const visibleJudges = computed(() => judges.value);
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
    const resultBrowser = ref(null);
    const activeSkill = ref("");
    const resultQuery = ref("");
    const resultPage = ref(1);
    const resultPageSize = ref(10);
    const progressPage = ref(1);
    const resultJumpPage = ref("");
    const progressJumpPage = ref("");
    const cellTooltip = ref({ visible: false, text: "", style: {} });
    const historyItems = ref([]);
    const historyNoteDrafts = ref({});
    const historyNoteEditing = ref({});
    const loadingHistory = ref(false);
    const clockNow = ref(Date.now());
    let tooltipHideTimer = null;
    let progressClockTimer = null;
    const pageSize = 10;
    const opPageSize = 10;
    const progressStages = ["排队", "分类", "模型/裁判", "聚合", "完成"];

    const formatHint = computed(
      () =>
        ({
          compare: "逐题导入 JSONL：id(可选)、query、context(可选)、video1、video2、context1/context2(可选)、answer1/answer2(可选)、task_start_time/task_end_time(可选，单位秒)",
          rich_content: "可逐题上传，也可导入 JSONL：query、context(可选)、video_path、category/answer_text/task_start_time/task_end_time(均可选)；普通图片不算挂卡，回答区域蓝色文字按 Superlink 统计。",
        }[mode.value])
    );

    const opPageCount = computed(() => Math.max(1, Math.ceil(opItems.value.length / opPageSize)));
    const pagedOpItems = computed(() => {
      const page = Math.min(opPage.value, opPageCount.value);
      const start = (page - 1) * opPageSize;
      return opItems.value.slice(start, start + opPageSize).map((item, offset) => ({
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

    function progressStageRank(progressItem) {
      if (progressItem.status === "done") return 4;
      if (progressItem.module === "结果聚合") return 3;
      if (["模型裁判", "被测模型", "单题评测"].includes(progressItem.module)) return 2;
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
        const displayLabel = r.category_display || key;
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

    const resultCols = computed(() => {
      const contextCols = results.value.some((r) => r.context != null && r.context !== "")
        ? [{ key: "context", label: "背景" }]
        : [];
      if (mode.value === "compare")
        return [
          { key: "item_id", label: "题号" },
          { key: "query", label: "题目" },
          ...contextCols,
          { key: "relevance", label: "相关性" },
          { key: "safety", label: "安全合规" },
          { key: "content_quality", label: "内容质量" },
          { key: "need_closure", label: "需求闭环" },
          { key: "personalization", label: "个性化一致性" },
          { key: "has_conflict", label: "内容冲突" },
          { key: "rationale", label: "理由" },
          { key: "latency_s", label: "耗时" },
        ];
      // rich_content（默认）
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
        { key: "answer_coverage", label: "回答覆盖" },
        { key: "needs_review", label: "需人工复核" },
        { key: "review_reason", label: "复核原因" },
        { key: "problem_solved", label: "Correctness" },
        { key: "problem_solved_reason", label: "评价原因" },
        { key: "answer_issues", label: "error_type" },
        { key: "rationale", label: "识别结论" },
        { key: "latency_s", label: "耗时" },
      ];
    });

    function columnWidth(c) {
      const compact = [
        "latency_s", "card_presence", "card_count", "superlink_presence",
        "superlink_count", "answer_coverage", "needs_review", "problem_solved",
      ].includes(c.key);
      const textColumn = ["query", "context", "answer_text", "rationale", "answer_issues", "problem_solved_reason"].includes(c.key);
      let minWidth = compact ? 80 : textColumn ? 150 : 96;
      let maxWidth = compact ? 120 : c.key === "rationale" ? 380 : textColumn ? 320 : 200;
      if (c.key === "item_id") {
        minWidth = 110;
        maxWidth = 160;
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

    const filteredResults = computed(() => {
      const q = resultQuery.value.trim().toLowerCase();
      return skillResults.value.filter((r) => {
        if (q && !`${r.item_id || ""} ${r.query || ""} ${r.context || ""} ${r.answer_text || ""} ${(r.card_contents || []).join(" ")} ${(r.superlink_texts || []).join(" ")} ${r.rationale || ""}`.toLowerCase().includes(q)) return false;
        return true;
      });
    });

    const pageCount = computed(() => Math.max(1, Math.ceil(filteredResults.value.length / resultPageSize.value)));
    const pagedResults = computed(() => {
      const safePage = Math.min(resultPage.value, pageCount.value);
      const start = (safePage - 1) * resultPageSize.value;
      return filteredResults.value.slice(start, start + resultPageSize.value);
    });

    // —— 结果统计：Correctness(problem_solved) 与 error_type(answer_issues 标签) ——
    const correctnessStats = computed(() => {
      const counts = new Map();
      let evaluated = 0;
      filteredResults.value.forEach((r) => {
        if (r.error) return;
        evaluated += 1;
        const key = String(r.problem_solved || "");
        counts.set(key, (counts.get(key) || 0) + 1);
      });
      const labels = { ok: "OK", nok: "NOK", need_review: "需复查" };
      const fixedKeys = ["ok", "nok", "need_review"];
      const extraKeys = Array.from(counts.keys()).filter((k) => k && !fixedKeys.includes(k)).sort();
      const orderedKeys = [...fixedKeys, ...extraKeys, ...(counts.has("") ? [""] : [])];
      const rows = evaluated
        ? orderedKeys.map((key) => {
            const count = counts.get(key) || 0;
            return {
              key: key || "__unset__",
              label: labels[key] || key || "未判定",
              count,
              percent: (count / evaluated) * 100,
            };
          })
        : [];
      return { evaluated, rows };
    });

    const errorTypeStats = computed(() => {
      const counts = new Map();
      const sampleCounts = new Map();
      let issueTotal = 0;
      let issueSampleTotal = 0;
      let evaluated = 0;
      filteredResults.value.forEach((r) => {
        if (r.error) return;
        evaluated += 1;
        const rowLabels = new Set();
        String(r.answer_issues || "").split(/\r?\n/).forEach((line) => {
          const trimmed = line.trim();
          if (!trimmed) return;
          // 每条问题格式为“标签：具体描述”，分类只取“：”（或“:”）前的标签
          const colon = trimmed.search(/[：:]/);
          const label = (colon > 0 ? trimmed.slice(0, colon) : trimmed).trim();
          if (!label) return;
          counts.set(label, (counts.get(label) || 0) + 1);
          rowLabels.add(label);
          issueTotal += 1;
        });
        if (rowLabels.size) issueSampleTotal += 1;
        rowLabels.forEach((label) => sampleCounts.set(label, (sampleCounts.get(label) || 0) + 1));
      });
      const rows = Array.from(counts.entries())
        .map(([label, count]) => ({
          label,
          count,
          percent: issueTotal ? (count / issueTotal) * 100 : 0,
          // 样本占比 = 出现该错误的题数 / 已评测题数（同题重复标签只算一次）
          sampleRate: evaluated ? ((sampleCounts.get(label) || 0) / evaluated) * 100 : 0,
        }))
        .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, "zh"));
      return {
        issueTotal,
        issueSampleTotal,
        evaluated,
        anyIssueRate: evaluated ? (issueSampleTotal / evaluated) * 100 : 0,
        rows,
      };
    });

    function selectSkill(key) {
      activeSkill.value = key;
      resultPage.value = 1;
      progressPage.value = 1;
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
    function changeOpPage(delta) {
      setTablePage("operation", opPage.value + delta);
    }
    function changeProgressPage(delta) {
      setTablePage("progress", progressPage.value + delta);
    }
    function jumpTablePage(kind) {
      const jumpValues = {
        result: resultJumpPage.value,
        operation: opJumpPage.value,
        progress: progressJumpPage.value,
      };
      setTablePage(kind, jumpValues[kind]);
    }

    function changeResultPageSize() {
      if (![10, 20, 50].includes(resultPageSize.value)) resultPageSize.value = 10;
      resultPage.value = 1;
      resultJumpPage.value = "";
    }

    function defaultJudgeSelection() {
      return judges.value.length ? [judges.value[0].name] : [];
    }

    function switchMode(k) {
      mode.value = k;
      selectedJudges.value = defaultJudgeSelection();
      items.value = [];
      progressPage.value = 1;
      errors.value = [];
      datasetName.value = "";
      opItems.value = [newOpItem()];
      opPage.value = 1;
      opJumpPage.value = "";
    }

    // —— 视频评测：逐题卡片（query + 可选 context + 视频上传 + 可选 answer_text）——
    function newOpItem() {
      return { _uiKey: ++opItemSequence, id: "", query: "", context: "", category: "", videoName: "", videoPath: "", frames: [], frameCount: 0, duration: 0, answer: "", taskStartTime: null, taskEndTime: null, sourceLine: null, sourceData: null, sessionGroup: null, turnIndex: null, uploading: false, uploadError: "" };
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
      datasetName.value = file.name || "";
      opPreparing.value = true;
      errors.value = [];
      items.value = [];
      opItems.value = [newOpItem()];
      opPage.value = 1;
      opJumpPage.value = "";
      try {
        const content = await file.text();
        const isCsv = /\.csv$/i.test(file.name || "");
        console.log("[onOpManifestFile] mode:", mode.value, "csv:", isCsv, "file size:", content.length);
        const parseBody = isCsv
          ? { mode: mode.value, csv: content }
          : { mode: mode.value, jsonl: content };
        const parseResponse = await fetch("/api/parse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(parseBody),
        });
        const parsed = await parseResponse.json().catch(() => ({}));
        console.log("[onOpManifestFile] response ok:", parseResponse.ok, "items:", (parsed.items || []).length, "errors:", (parsed.errors || []).length);
        if (!parseResponse.ok) throw new Error(parsed.detail || (isCsv ? "CSV 解析请求失败" : "JSONL 解析请求失败"));
        const importErrors = [...(parsed.errors || [])];
        if (!(parsed.items || []).length) {
          errors.value = importErrors.length ? importErrors : ["JSONL 中没有可导入的数据"];
          console.warn("[onOpManifestFile] no items parsed");
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
            videoPath: item.video_path || item.video1 || "",
            answer: mode.value === "compare" ? (item.answer1 || "") : (item.answer_text || ""),
            answer1: item.answer1 || "",
            answer2: item.answer2 || "",
            context1: item.context1 || "",
            context2: item.context2 || "",
            video1Path: item.video1 || "",
            video2Path: item.video2 || "",
            taskStartTime: item.task_start_time ?? null,
            taskEndTime: item.task_end_time ?? null,
            sourceLine: item.source_line ?? null,
            sourceData: item.source_data || null,
            sessionGroup: item.session_group ?? null,
            turnIndex: item.turn_index ?? null,
          }));
          opPage.value = 1;
          console.log("[onOpManifestFile] opItems mapped:", opItems.value.length, "first videoPath:", opItems.value[0]?.videoPath, "first query:", opItems.value[0]?.query);
        }
      } catch (error) {
        console.error("[onOpManifestFile] error:", error);
        errors.value = ["批量导入失败：" + (error?.message || String(error))];
      } finally {
        opPreparing.value = false;
      }
    }

    const canSubmit = computed(() =>
      !opPreparing.value && opItems.value.some(
        (it) => it.query.trim() && ((it.frames || []).length || it.videoPath)
      )
    );

    async function submit() {
      runError.value = "";
      const valid = opItems.value.filter(
        (it) => it.query.trim() && ((it.frames || []).length || it.videoPath)
      );
      if (!valid.length) {
        alert("请为每题填写 query，并提供视频路径或上传视频后再评估。");
        return;
      }
      items.value = valid.map((it, idx) => {
        const prefix = mode.value === "compare" ? "cmp" : "rich";
        const item = {
          id: it.id || `${prefix}${idx + 1}`,
          query: it.query.trim(),
          context: (it.context || "").trim(),
        };
        if (mode.value === "compare") {
          item.video1 = it.video1Path || it.videoPath || "";
          item.video2 = it.video2Path || "";
          item.context1 = (it.context1 || "").trim();
          item.context2 = (it.context2 || "").trim();
          item.answer1 = (it.answer1 || it.answer || "").trim();
          item.answer2 = (it.answer2 || "").trim();
          item.category = (it.category || "").trim() || "default";
        } else {
          item.video_path = it.videoPath;
          item.category = (it.category || "").trim() || "default";
          item.answer_text = (it.answer || "").trim();
        }
        if ((it.frames || []).length) {
          item.media = [it.videoPath];
          item.frames = it.frames;
        }
        if (Number.isFinite(it.taskStartTime)) item.task_start_time = it.taskStartTime;
        if (Number.isFinite(it.taskEndTime)) item.task_end_time = it.taskEndTime;
        if (Number.isFinite(it.sourceLine)) item.source_line = it.sourceLine;
        if (it.sourceData) item.source_data = it.sourceData;
        if (it.sessionGroup != null) item.session_group = it.sessionGroup;
        if (it.turnIndex != null) item.turn_index = it.turnIndex;
        return item;
      });
      errors.value = [];
      results.value = [];
      summary.value = null;
      progressEvents.value = {};
      activeSkill.value = "";
      resultQuery.value = "";
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
        dataset_name: datasetName.value || "手动录入",
        options: {
          judges: selectedJudges.value,
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
        const result = d.result;
        const index = result && result.index;
        if (index == null) {
          results.value.push(result);
        } else {
          // 断线重连时服务端会整段回放 results：按 index 替换去重，
          // 防止重连一次就全量翻倍（重复行 + 内存线性增长）
          const pos = results.value.findIndex((x) => x && x.index === index);
          if (pos >= 0) results.value.splice(pos, 1, result);
          else results.value.push(result);
        }
        progress.value = d.progress;
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

    function cell(r, c) {
      const v = r[c.key];
      if (c.key === "category") return r.category_display || (!v || v === "default" ? "通用" : v);
      if (c.key === "latency_s") return v != null ? v + "秒" : "";
      // 垂域视觉对比维度渲染
      if (["relevance", "safety", "content_quality", "need_closure", "personalization"].includes(c.key)) {
        if (v === "answer1") return "产品1更优";
        if (v === "answer2") return "产品2更优";
        if (v === "tie") return "平手";
        if (v == null) return "N/A";
        return v || "";
      }
      if (c.key === "has_conflict") {
        if (v === "yes") return "有冲突";
        if (v === "no") return "无冲突";
        if (v === "unclear") return "不清楚";
        return v || "";
      }
      if (["card_types", "card_contents", "superlink_texts"].includes(c.key)) {
        return Array.isArray(v) ? v.join("；") : (v || "");
      }
      if (c.key === "card_presence" || c.key === "superlink_presence") {
        return ({ present: "是", absent: "否", unclear: "不清楚" }[v] || v) || "";
      }
      if (c.key === "card_suitability") {
        if (v === "ok") return "OK";
        if (v === "nok") return "NOK";
        return v || "";
      }
      if (c.key === "problem_solved") {
        return ({ ok: "OK", nok: "NOK", need_review: "需复查" }[v] || v) || "";
      }
      if (c.key === "answer_coverage") {
        return ({ complete: "完整", partial: "部分", unclear: "不确定" }[v] || v) || "";
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
        let detail = "";
        try {
          detail = (await r.json()).detail || "";
        } catch (e) {}
        alert(detail ? `删除失败：${detail}` : "删除失败");
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
      if (!modes.some((m) => m.key === d.mode)) {
        alert("该历史记录使用已下线的评测模式，无法加载。");
        return;
      }
      taskId.value = d.task_id || id;
      mode.value = d.mode;
      datasetName.value = d.dataset_name || "";
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
      resultPage.value = 1;
      progressPage.value = 1;
      if (mode.value !== "compare" && skillTabs.value.length) activeSkill.value = skillTabs.value[0].key;
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
    function exportFrames() {
      window.open(`/api/eval/${taskId.value}/export?format=frames_zip`);
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
      judges.value = d.judges || [];
      selectedJudges.value = defaultJudgeSelection();
      loadHistory();
    });

    onUnmounted(() => {
      if (progressClockTimer != null) window.clearInterval(progressClockTimer);
    });

    return {
      modes, mode, modeLabel, isVideoMode, items, errors, judges, visibleJudges, selectedJudges, datasetName,
      concurrency, evalTimeout, running, progress, total, results, summary, taskId, runError,
      itemProgress, progressEvents, progressRows, pagedProgressRows, progressStages,
      historyItems, historyNoteDrafts, historyNoteEditing, loadingHistory, pageSize,
      opPage, opPageSize, opPageCount, opJumpPage,
      progressPage, progressPageCount, progressJumpPage,
      resultJumpPage,
      resultBrowser,
      activeSkill, resultQuery, resultPage, resultPageSize,
      skillTabs, filteredResults, pagedResults, pageCount, resultTableWidth,
      correctnessStats, errorTypeStats,
      formatHint, resultCols, opItems, pagedOpItems, opPreparing, canSubmit,
      switchMode, onOpManifestFile, submit, cell, columnWidth, exportCsv, exportJson, exportXlsx, exportFrames, itemArtifactUrl, addOpItem, removeOpItem, onOpVideo, onOpDrop,
      loadHistory, loadHistoryTask, delHistory, editHistoryNote, cancelHistoryNote, saveHistoryNote, formatTime,
      selectSkill, resetResultPage, changePage,
      changeProgressPage, changeOpPage, changeResultPageSize, paginationPages, setTablePage, jumpTablePage,
      progressStageClass, progressDisplay, progressStageLabel, progressStatusClass,
      progressMeta, formatProgressEventTime, progressEventMeta, progressEventMessage, scrollProgressLog,
      formatProgressElapsed, shortRequestId, copyRequestId,
      cellTooltip, showCellTooltip, scheduleHideCellTooltip, keepCellTooltip, hideCellTooltip,
    };
  },
}).mount("#app");
