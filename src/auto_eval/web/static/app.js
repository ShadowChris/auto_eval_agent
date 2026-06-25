import { createApp, ref, computed, onMounted, nextTick } from "https://unpkg.com/vue@3/dist/vue.esm-browser.js";
import * as echarts from "https://unpkg.com/echarts@5/dist/echarts.esm.min.js";

createApp({
  setup() {
    const modes = [
      { key: "single", label: "单回答盲评" },
      { key: "compare", label: "两回答对比" },
      { key: "online", label: "接模型在线评估" },
      { key: "process", label: "过程盲评(含轨迹)" },
    ];
    const mode = ref("single");
    const text = ref("");
    const fileText = ref("");
    const isJsonl = ref(false);
    const items = ref([]);
    const errors = ref([]);
    const judges = ref([]);
    const models = ref([]);
    const selectedJudges = ref([]);
    const selectedModel = ref("");
    const concurrency = ref(4);
    const running = ref(false);
    const progress = ref(0);
    const total = ref(0);
    const results = ref([]);
    const summary = ref(null);
    const taskId = ref("");
    const pieChart = ref(null);
    const barChartRefs = ref([]);
    const resultBrowser = ref(null);
    const activeSkill = ref("");
    const resultQuery = ref("");
    const correctnessFilter = ref("");
    const problemDimFilter = ref("");
    const resultPage = ref(1);
    const pageSize = 20;

    const formatHint = computed(
      () =>
        ({
          single: "每行一题：query ||| answer [||| competitor] [||| reference]   （competitor=竞品结果，产品专家用、可选；reference=参考答案、可选，填了跑元评测）",
          compare: "每行一题：query ||| answerA ||| answerB [||| reference]",
          online: "每行一题：query [||| reference]   （后端现场调模型生成回答，再盲评）",
          process: "每行一题：query ||| answer ||| trace [||| reference]   （trace=被测 agent 的推理/工具轨迹；评过程质量）",
        }[mode.value])
    );
    const placeholder = computed(
      () =>
        ({
          single: "光合作用是什么？ ||| 植物利用光合成有机物 ||| 竞品：植物吸收光能合成有机物放出氧气 ||| 绿色植物利用光能将二氧化碳和水合成有机物并释放氧气\n中国最长的河流？ ||| 长江",
          compare: "写一首关于春天的诗 ||| 春风又绿江南岸 ||| 春眠不觉晓\n推荐一部科幻电影 ||| 星际穿越 ||| 流浪地球",
          online: "2024 年诺贝尔文学奖获得者是谁？\n计算 17 × 24 等于多少？",
          process: "北京到上海多少公里？ ||| 约 1200 公里 ||| 1.调用地图API 2.距离=1318km 3.约1200km\n某函数是否正确？ ||| 正确 ||| def f(n): return 1 if n<=1 else n*f(n-1)",
        }[mode.value])
    );

    const previewKeys = computed(() => {
      if (!items.value.length) return [];
      const keys = ["query"];
      if (mode.value === "single") keys.push("answer", "reference");
      else if (mode.value === "compare") keys.push("answer_a", "answer_b", "reference");
      else if (mode.value === "process") keys.push("answer", "trace", "reference");
      else keys.push("reference");
      return keys.filter((k) => items.value.some((it) => it[k] != null && it[k] !== ""));
    });

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
      });
      return dims;
    });

    const resultCols = computed(() => {
      if (mode.value === "compare")
        return [
          { key: "query", label: "题目" },
          { key: "answer_a", label: "回答 A" },
          { key: "answer_b", label: "回答 B" },
          { key: "winner", label: "胜者" },
          { key: "bidirectional_consistent", label: "双向一致" },
          { key: "rationale", label: "理由" },
        ];
      const dims = rubricDims.value.map((d) => ({ key: `rubric:${d}`, label: d, rubricDim: d }));
      return [
        { key: "item_id", label: "题号" },
        { key: "query", label: "题目" },
        { key: mode.value === "online" ? "generated_answer" : "answer", label: mode.value === "online" ? "生成回答" : "回答" },
        { key: "correctness", label: "判定" },
        { key: "total", label: "总分" },
        ...dims,
        { key: "used_search", label: "联网" },
        { key: "truncated", label: "截断" },
        { key: "arbitrated", label: "仲裁" },
        { key: "agree", label: "与真值" },
        { key: "rationale", label: "理由" },
      ];
    });

    const filteredResults = computed(() => {
      const q = resultQuery.value.trim().toLowerCase();
      const threshold = (summary.value && summary.value.by_skill && summary.value.by_skill.threshold) || 2;
      return skillResults.value.filter((r) => {
        if (correctnessFilter.value && r.correctness !== correctnessFilter.value) return false;
        if (problemDimFilter.value && (r.rubric || {})[problemDimFilter.value] > threshold) return false;
        if (problemDimFilter.value && (r.rubric || {})[problemDimFilter.value] == null) return false;
        if (q && !`${r.item_id || ""} ${r.query || ""} ${r.answer || ""} ${r.rationale || ""}`.toLowerCase().includes(q)) return false;
        return true;
      });
    });

    const pageCount = computed(() => Math.max(1, Math.ceil(filteredResults.value.length / pageSize)));
    const pagedResults = computed(() => {
      const safePage = Math.min(resultPage.value, pageCount.value);
      const start = (safePage - 1) * pageSize;
      return filteredResults.value.slice(start, start + pageSize);
    });

    function selectSkill(key) {
      activeSkill.value = key;
      problemDimFilter.value = "";
      resultPage.value = 1;
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
    function changePage(delta) {
      resultPage.value = Math.min(pageCount.value, Math.max(1, resultPage.value + delta));
    }

    function trunc(v) {
      if (v == null) return "";
      const s = String(v);
      return s.length > 50 ? s.slice(0, 50) + "…" : s;
    }

    function switchMode(k) {
      mode.value = k;
      items.value = [];
      errors.value = [];
      fileText.value = "";
      isJsonl.value = false;
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
      if (errors.value.length) console.log("解析错误：", errors.value);
    }

    async function submit() {
      // 自动解析最新输入（用户可跳过手动"解析预览"）
      await doParse();
      if (!items.value.length) {
        alert("解析后没有可评估的题。请检查格式：每行『问题 ||| 回答』。");
        return;
      }
      results.value = [];
      summary.value = null;
      barChartRefs.value = [];
      activeSkill.value = "";
      resultQuery.value = "";
      correctnessFilter.value = "";
      problemDimFilter.value = "";
      resultPage.value = 1;
      progress.value = 0;
      total.value = items.value.length;
      running.value = true;
      const body = {
        mode: mode.value,
        items: items.value,
        options: {
          judges: selectedJudges.value,
          model: selectedModel.value,
          concurrency: concurrency.value,
        },
      };
      const r = await fetch("/api/eval", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      taskId.value = d.task_id;
      connectSSE();
    }

    function connectSSE() {
      const es = new EventSource(`/api/eval/${taskId.value}/stream`);
      es.addEventListener("result", (e) => {
        const d = JSON.parse(e.data);
        results.value.push(d.result);
        progress.value = d.progress;
      });
      es.addEventListener("done", (e) => {
        summary.value = JSON.parse(e.data).summary;
        if (mode.value !== "compare" && skillTabs.value.length) activeSkill.value = skillTabs.value[0].key;
        resultPage.value = 1;
        running.value = false;
        es.close();
        renderCharts();
      });
      es.addEventListener("error", (e) => {
        try {
          if (e.data) {
            const d = JSON.parse(e.data);
            alert("评估出错：" + (d.message || "未知"));
          }
        } catch (_) {}
        running.value = false;
        es.close();
      });
    }

    function cell(r, c) {
      const v = r[c.key];
      if (c.rubricDim) return r.rubric && r.rubric[c.rubricDim] != null ? r.rubric[c.rubricDim] : "";
      if (c.key === "category") return r.category_display || (!v || v === "default" ? "通用" : v);
      if (c.key === "agree") {
        if (v === undefined) return "";
        return v === true ? "✓ 一致" : v === false ? "✗ 不一致" : "?";
      }
      if (c.key === "used_search") return v ? "是" : "否";
      if (c.key === "truncated") return v ? "⚠️是(强制判定)" : "";
      if (c.key === "arbitrated") return v ? `⚖️是(${r.arbitrator_confidence ?? "-"})` : "";
      if (c.key === "bidirectional_consistent") return v ? "是" : "否(位置偏差)";
      if (c.key === "winner") return v === "a" ? "A" : v === "b" ? "B" : "平";
      if (c.key === "correctness") return ({ right: "正确", wrong: "错误", partial: "部分", unclear: "不清" }[v] || v) || "";
      if (v == null) return "";
      if (typeof v === "string" && v.length > 80) return v.slice(0, 80) + "…";
      return v;
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

    function exportCsv() {
      window.open(`/api/eval/${taskId.value}/export?format=csv`);
    }
    function exportJson() {
      window.open(`/api/eval/${taskId.value}/export?format=json`);
    }

    onMounted(async () => {
      const r = await fetch("/api/config");
      const d = await r.json();
      judges.value = d.judges;
      models.value = d.models;
      selectedJudges.value = d.judges.length ? [d.judges[0].name] : [];
      selectedModel.value = d.models[0] || "";
    });

    return {
      modes, mode, text, items, errors, judges, models, selectedJudges, selectedModel,
      concurrency, running, progress, total, results, summary, taskId,
      pieChart, barChartRefs, resultBrowser, setBarRef, renderCharts,
      activeSkill, resultQuery, correctnessFilter, problemDimFilter, resultPage,
      skillTabs, rubricDims, filteredResults, pagedResults, pageCount,
      formatHint, placeholder, previewKeys, resultCols,
      trunc, switchMode, onFile, doParse, submit, cell, exportCsv, exportJson,
      selectSkill, drillDownDimension, clearDimensionDrillDown, resetResultPage, changePage,
    };
  },
}).mount("#app");
