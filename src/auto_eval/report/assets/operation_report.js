/* Web 与离线 HTML 共用；仅渲染投影后的报告数据，不访问网络或执行源数据。 */
(function (global) {
  "use strict";
  const TYPES = ["ok", "nok", "no_support", "others"];
  const COLORS = ["var(--or-green)", "var(--or-red)", "var(--or-amber)", "var(--or-slate)"];
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
  const text = value => value != null && typeof value === "object" ? JSON.stringify(value, null, 2) : String(value ?? "");
  const pct = value => value == null ? "—" : `${(value * 100).toFixed(2)}%`;
  const signed = (value, rate = false) => value == null ? "—"
    : `${value > 0 ? "+" : ""}${rate ? (value * 100).toFixed(2) : value}${rate ? "pp" : ""}`;
  const deltaClass = value => value == null || value === 0 ? "" : value < 0 ? "or-good" : "or-bad";
  const rateClass = pair => pair.ok_rate_change === "improved" ? "or-good" : pair.ok_rate_change === "worsened" ? "or-bad" : "";
  const table = (headers, rows, cls = "") => `<table class="${cls}"><thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  function safeUrl(value) {
    if (typeof value !== "string" || /[\u0000-\u001f\\]/.test(value)) return "";
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
    } catch (_) { return ""; }
  }
  function link(value, label) {
    const url = safeUrl(value);
    return url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">${esc(label)}</a>` : "";
  }
  function videoLinks(row) {
    if (!row) return "—";
    return `<div class="or-links">${[
      link(row.video_url_domain, "域名站"),
      link(row.video_url_ip, "IP站"),
      link(row.video_url, "其他录屏"),
    ].filter(Boolean).join("") || "—"}</div>`;
  }
  function verdict(row) {
    const color = COLORS[TYPES.indexOf(row.correctness)] || "var(--or-muted)";
    return `<span class="or-pill" style="color:${color}">${esc(row.correctness || "—")}</span>`;
  }

  function mount(root, initialPayload) {
    let payload, baseline, target, pair, issueRows = [], pairs = [], generation = "", resizeFrame = 0;
    let disposed = false, observedWidth = 0, observer;
    const state = { group: "", view: "bar", issue: "", change: "all", correctness: "", page: 1, size: 10, open: null, query: "", sort: "default" };
    const $ = name => root.querySelector(`[data-or="${name}"]`);
    const comparison = () => payload.kind === "comparison";
    root.classList.add("operation-report");

    function prepare() {
      if (comparison()) {
        baseline = payload.groups.find(g => g.task_id === payload.baseline_task_id);
        pair = payload.pairs.find(p => p.target_task_id === state.group) || payload.pairs[0];
        state.group = pair?.target_task_id || "";
        target = payload.groups.find(g => g.task_id === state.group);
        issueRows = (pair?.issue_type_rows || []).map(r => ({
          name: r.issue_type, count: r.target_count, rate: r.target_rate,
          baselineCount: r.baseline_count, baselineRate: r.baseline_rate,
          countDelta: r.count_delta, rateDelta: r.rate_delta,
        }));
        pairs = (pair?.matches || []).map(([left, right]) => ({ left: baseline.cases[left], right: target.cases[right], key: right }));
      } else {
        issueRows = (payload.statistics.issue_type_rows || []).map(r => ({ name: r.issue_type, count: r.case_count, rate: r.rate }));
        pairs = payload.cases.map((right, key) => ({ right, key }));
      }
      if (state.issue && !issueRows.some(r => r.name === state.issue)) state.issue = "";
    }
    function topIssues(limit) {
      return [...issueRows].sort((a, b) =>
        (comparison() ? (b.count + b.baselineCount) - (a.count + a.baselineCount) : b.count - a.count)
        || a.name.localeCompare(b.name, "zh-CN")).slice(0, limit);
    }
    function metric(label, value, note) {
      return `<div class="or-metric"><div class="or-muted">${label}</div><div class="or-metric-value">${value}</div><div class="or-muted">${note}</div></div>`;
    }
    function render() {
      if (!payload || disposed) return;
      const isCompare = comparison(), stats = payload.statistics;
      const subtitle = isCompare
        ? payload.groups.map(g => `${esc(g.group_label)}：${esc(g.group_name)}`).join(" · ")
        : esc(payload.dataset_name || payload.task_id);
      root.innerHTML = `
        <div class="or-heading"><div><div class="or-name">${isCompare ? "任务类对比报告" : "任务类统计报告"}</div><div class="or-muted">${subtitle}</div></div>
          ${isCompare ? `<label>实验组 <select data-or="group">${payload.pairs.map(p => `<option value="${esc(p.target_task_id)}" ${p.target_task_id === state.group ? "selected" : ""}>${esc(p.target_label)}</option>`).join("")}</select></label>` : ""}
        </div>
        <div class="or-summary">${isCompare
          ? metric("实验组 OK 率", pct(pair.target_ok_rate), `对照组 ${pct(pair.baseline_ok_rate)} · 两组交集`)
            + metric("OK 率差值", `<span class="${rateClass(pair)}">${signed(pair.ok_rate_delta, true)}</span>`, esc(pair.ok_rate_change_label))
            + metric("两组共同有效 Case", esc(pair.valid_pair_count), `全组交集 ${esc(payload.all_groups_common_valid_count)} 条`)
            + metric("OK 净变化", `<span class="${rateClass(pair)}">${signed(pair.net_ok_change)}</span>`, `其他→OK ${pair.to_ok_count} / OK→其他 ${pair.from_ok_count}`)
          : metric("OK 率", pct(stats.ok_rate), `${stats.ok_count} / ${stats.valid_count} 条有效评估`)
            + metric("NOK 率", pct(stats.valid_count ? stats.nok_count / stats.valid_count : null), `${stats.nok_count} 条`)
            + metric("有效评估", `${stats.valid_count} / ${stats.total_cases}`, `覆盖率 ${pct(stats.coverage_rate)}`)
            + metric("评估失败", esc(stats.failed_count), `待评估 ${stats.pending_count} · 均不计入分母`)
        }</div>
        <div class="or-panels">
          <section class="or-panel"><div class="or-panel-heading"><h3>Correctness 分布</h3><span class="or-muted">${isCompare ? "全组共同有效集合" : "全部有效评估"}</span></div><div data-or="correctness" class="or-chart"></div></section>
          <section class="or-panel"><div class="or-panel-heading"><h3>${isCompare ? "问题类型对比" : "Top 问题类型"}</h3><div class="or-segments">${["bar", "radar", "table"].map((v, i) => `<button data-view="${v}" aria-pressed="${state.view === v}">${["柱状图", "雷达图", "表格"][i]}</button>`).join("")}</div></div>
            ${isCompare ? `<div class="or-controls"><label class="or-muted">排序 <select data-or="sort">
              ${[["default", "频次差值升序"], ["count", "实验组频次降序"], ["baseline", "对照组频次降序"], ["absolute", "差值绝对值降序"]].map(([v, n]) => `<option value="${v}" ${state.sort === v ? "selected" : ""}>${n}</option>`).join("")}
            </select></label><span class="or-muted">雷达图固定使用两组合计 Top 6</span></div>` : ""}
            <div data-or="issues" class="or-chart"></div><p data-or="issue-note" class="or-muted or-hint"></p>
          </section>
        </div>
        <p data-or="conclusion" class="or-conclusion"></p>
        ${isCompare ? `<details class="or-panel"><summary>相对对照组的共同 Case 对比</summary><div class="or-table-wrap">${table(
          ["实验组", "共同有效", "对照组OK率", "实验组OK率", "其他→OK", "OK→其他", "OK净变化", "OK率差值", "结论"],
          payload.pairs.map(p => [esc(p.target_label), p.valid_pair_count, pct(p.baseline_ok_rate), pct(p.target_ok_rate), p.to_ok_count, p.from_ok_count, signed(p.net_ok_change), signed(p.ok_rate_delta, true), esc(p.ok_rate_change_label)]),
        )}</div></details>` : ""}
        <section class="or-panel">
          <div class="or-panel-heading"><h3>问题 Case</h3><span data-or="case-count" class="or-muted" aria-live="polite"></span></div>
          <div data-or="tags" class="or-tags"></div>
          <div class="or-controls">
            <select data-or="issue" aria-label="问题类型"><option value="">全部问题类型 / 全部有效 Case</option>${topIssues(issueRows.length).map(r => `<option value="${esc(r.name)}" ${state.issue === r.name ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select>
            <select data-or="correctness-filter" aria-label="${isCompare ? "实验组判定筛选" : "判定筛选"}"><option value="">${isCompare ? "全部实验组判定" : "全部判定"}</option>${TYPES.map(t => `<option value="${t}" ${state.correctness === t ? "selected" : ""}>${t}</option>`).join("")}</select>
            <input data-or="search" type="search" placeholder="搜索 index / query / 理由" aria-label="搜索 Case" value="${esc(state.query)}">
          </div>
          ${isCompare ? `<div class="or-segments or-controls">${[["all", "全部命中"], ["new", "新增问题"], ["resolved", "问题消失"], ["persistent", "持续存在"]].map(([v, n]) => `<button data-change="${v}" aria-pressed="${state.change === v}">${n}</button>`).join("")}</div>` : ""}
          <div data-or="cases" class="or-table-wrap"></div>
          <div class="or-footer"><span class="or-muted">原录屏：域名站 / IP站；空链接不显示，访问需相应网络权限。</span><div class="or-row"><label class="or-muted">每页 <select data-or="size">${[10, 20, 50].map(n => `<option value="${n}" ${state.size === n ? "selected" : ""}>${n} 条</option>`).join("")}</select></label><button data-page="-1">上一页</button><span data-or="page-label" class="or-muted"></span><button data-page="1">下一页</button></div></div>
        </section>
        <p class="or-muted">报告快照 ${esc(payload.generated_at)} · 问题类型按 Case 去重，占比之和可能超过 100%。${isCompare ? "NOK 表示判定 nok；其他→OK 中的“其他”包括 nok、no_support、others。" : ""} 未打包媒体或模型原始调用。</p>`;
      drawCorrectness(); drawIssues(); renderCases(); renderConclusion();
    }
    function drawCorrectness() {
      let body = "";
      if (!comparison()) {
        const stats = payload.statistics;
        if (!stats.valid_count) {
          $("correctness").innerHTML = '<div class="or-empty">暂无有效判定</div>'; return;
        }
        body = `<div class="or-columns">${stats.correctness_rows.map((r, i) => `<div class="or-column"><div class="or-col-label">${r.count}<br><span class="or-muted">${pct(r.rate)}</span></div><button data-correctness="${r.correctness}" aria-label="查看 ${r.correctness} 的 Case" class="or-col-fill" style="height:${r.rate * 100}%;background:${COLORS[i]};min-height:${r.count ? 3 : 0}px"></button></div>`).join("")}</div><div class="or-col-names">${TYPES.map(t => `<span>${t}</span>`).join("")}</div>`;
        body += `<details><summary>查看统计表</summary>${table(["判定", "频次", "占比"], stats.correctness_rows.map(r => [esc(r.correctness), r.count, pct(r.rate)]))}</details>`;
      } else {
        const max = Math.max(...payload.groups.map(g => g.statistics.ok_rate ?? -1));
        body = `<p class="or-muted">全组共同有效 ${payload.all_groups_common_valid_count} 条；最高 OK 率加粗。</p>`;
        for (const group of payload.groups) {
          const s = group.statistics;
          body += `<div class="or-stack-group"><div class="or-stack-head"><span>${esc(group.group_label)}</span><span style="font-weight:${s.ok_rate != null && s.ok_rate === max ? 700 : 400}">OK ${pct(s.ok_rate)}</span></div><div class="or-stack">${s.correctness_rows.map((r, i) => `<span style="width:${(r.rate || 0) * 100}%;background:${COLORS[i]}" title="${r.correctness}：${r.count} 条 · ${pct(r.rate)}">${r.rate >= .1 ? pct(r.rate) : ""}</span>`).join("")}</div>${!s.valid_count ? '<span class="or-muted">无全组共同有效数据</span>' : ""}</div>`;
        }
        body += `<div class="or-legend">${TYPES.map((t, i) => `<span><i class="or-dot" style="background:${COLORS[i]}"></i>${t}</span>`).join("")}</div>`;
        body += `<details><summary>查看各组统计表</summary><div class="or-table-wrap">${table(["组别", "原始量", "共同有效量", ...TYPES, "OK率"], payload.groups.map(g => [esc(g.group_label), g.original_count, g.common_valid_count, ...g.statistics.correctness_rows.map(r => r.count), pct(g.statistics.ok_rate)]))}</div></details>`;
      }
      $("correctness").innerHTML = body;
    }
    function orderedIssues() {
      return [...issueRows].sort((a, b) => {
        const d = !comparison() || state.sort === "count" ? b.count - a.count
          : state.sort === "baseline" ? b.baselineCount - a.baselineCount
          : state.sort === "absolute" ? Math.abs(b.countDelta) - Math.abs(a.countDelta)
          : a.countDelta - b.countDelta;
        return d || a.name.localeCompare(b.name, "zh-CN");
      });
    }
    function drawIssues() {
      const rows = orderedIssues(), isCompare = comparison();
      if (!rows.length) {
        $("issues").innerHTML = '<div class="or-empty">暂无问题类型</div>';
        $("issue-note").textContent = "仅统计有效评估 Case 的问题类型。"; return;
      }
      if (state.view === "table") {
        $("issues").innerHTML = `<div class="or-table-wrap">${table(
          isCompare ? ["问题类型", "对照频次", "对照占比", "实验频次", "实验占比", "频次差值", "占比差值"] : ["问题类型", "Case 数", "占比"],
          rows.map(r => [`<button class="or-link" data-issue="${esc(r.name)}">${esc(r.name)}</button>`,
            ...(isCompare ? [r.baselineCount, pct(r.baselineRate), r.count, pct(r.rate), `<span class="${deltaClass(r.countDelta)}">${signed(r.countDelta)}</span>`, `<span class="${deltaClass(r.rateDelta)}">${signed(r.rateDelta, true)}</span>`] : [r.count, pct(r.rate)])]),
        )}</div>`;
      } else if (state.view === "radar") {
        $("issues").innerHTML = '<div data-or="radar" class="or-radar"></div>';
        drawRadar();
      } else {
        const shown = rows.slice(0, 10), max = Math.max(...rows.map(r => isCompare ? Math.abs(r.rateDelta || 0) : r.count), 1e-6);
        $("issues").innerHTML = `<div class="or-muted">${isCompare ? "← 问题减少 · 占比差值（pp） · 问题增加 →" : "频次降序 · 数量 / 占比"}</div><div class="or-issue-rows">${shown.map(r => {
          const d = r.rateDelta || 0;
          const style = isCompare ? `left:${d < 0 ? 50 - Math.abs(d) / max * 50 : 50}%;width:${Math.abs(d) / max * 50}%;background:var(${d < 0 ? "--or-green" : "--or-red"})` : `width:${r.count / max * 100}%`;
          return `<button class="or-issue-bar" data-issue="${esc(r.name)}" aria-pressed="${state.issue === r.name}"><span>${esc(r.name)}</span><span class="or-track ${isCompare ? "or-delta" : ""}"><span class="or-fill" style="${style}"></span></span><span class="or-value ${isCompare ? deltaClass(d) : ""}">${isCompare ? signed(r.rateDelta, true) : `${r.count} · ${pct(r.rate)}`}</span></button>`;
        }).join("")}</div>${rows.length > 10 ? `<p class="or-muted">显示 10 / ${rows.length} 类，完整类别见表格。</p>` : ""}`;
      }
      $("issue-note").textContent = state.view === "radar"
        ? "相同轴、相同刻度；越向外问题越常见，面积不代表整体能力。下方可选择问题查看 Case。"
        : isCompare ? `两组共同有效 ${pair.valid_pair_count} 条。差值=实验组−对照组；问题减少不等于整体结果改善。`
          : `占比分母为 ${payload.statistics.valid_count} 条有效评估。点击问题类型查看 Case。`;
    }
    function drawRadar() {
      const el = $("radar"); if (!el) return;
      const axes = topIssues(6);
      if (axes.length < 3) { el.innerHTML = '<div class="or-empty">不足 3 类问题，不绘制雷达图；请查看柱状图或表格。</div>'; return; }
      const width = Math.max(230, el.clientWidth), height = 280, cx = width / 2, cy = 124, radius = Math.min(78, width * .22);
      const maximum = Math.max(.05, Math.ceil(Math.max(...axes.flatMap(r => [r.rate || 0, r.baselineRate || 0])) * 20) / 20);
      const point = (i, r) => [cx + Math.sin(i * Math.PI * 2 / axes.length) * r, cy - Math.cos(i * Math.PI * 2 / axes.length) * r];
      const points = ratio => axes.map((_, i) => point(i, radius * ratio).join(",")).join(" ");
      let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Top 问题类型占比雷达图"><title>各轴问题占比；越向外越多</title>`;
      for (const ratio of [1 / 3, 2 / 3, 1]) svg += `<polygon points="${points(ratio)}" fill="none" stroke="var(--or-line)"/>`;
      axes.forEach((r, i) => {
        const p = point(i, radius), l = point(i, radius + 16), label = r.name.length > 10 ? r.name.slice(0, 9) + "…" : r.name;
        const parts = width < 420 || label.length > 7 ? [label.slice(0, 4), label.slice(4)] : [label];
        const sin = Math.sin(i * Math.PI * 2 / axes.length);
        svg += `<line x1="${cx}" y1="${cy}" x2="${p[0]}" y2="${p[1]}" stroke="var(--or-line)"/><text class="or-radar-label" data-issue="${esc(r.name)}" x="${l[0]}" y="${l[1]}" text-anchor="${Math.abs(sin) < .1 ? "middle" : sin > 0 ? "start" : "end"}" dominant-baseline="middle"><title>${esc(r.name)}</title>${parts.filter(Boolean).map((part, j) => `<tspan x="${l[0]}" dy="${j ? 15 : i === 0 && parts.length > 1 ? -10 : 0}">${esc(part)}</tspan>`).join("")}</text>`;
      });
      for (const field of comparison() ? ["baselineRate", "rate"] : ["rate"]) {
        const color = field === "rate" ? "var(--or-blue)" : "var(--or-slate)";
        const polygon = axes.map((r, i) => point(i, radius * (r[field] || 0) / maximum).join(",")).join(" ");
        svg += `<polygon points="${polygon}" fill="${color}" fill-opacity=".12" stroke="${color}" stroke-width="2"/>`;
        axes.forEach((r, i) => {
          const p = point(i, radius * (r[field] || 0) / maximum);
          svg += `<circle cx="${p[0]}" cy="${p[1]}" r="3" fill="${color}"><title>${esc(r.name)}：${pct(r[field])}</title></circle>`;
        });
      }
      svg += `<text x="${cx + 6}" y="${cy - radius + 5}" font-size="11" fill="var(--or-muted)">${(maximum * 100).toFixed(1)}%</text></svg>`;
      el.innerHTML = svg + `<div class="or-legend">${comparison() ? '<span><i class="or-dot" style="background:var(--or-slate)"></i>对照组</span>' : ""}<span><i class="or-dot" style="background:var(--or-blue)"></i>${comparison() ? esc(target.group_label) : "当前批次"}</span><span>各轴范围 0–${(maximum * 100).toFixed(1)}%</span></div>`;
    }
    function changes(row) {
      const left = new Set(row.left?.issue_types || []), right = new Set(row.right.issue_types || []);
      return { new: [...right].filter(n => !left.has(n)), resolved: [...left].filter(n => !right.has(n)), persistent: [...right].filter(n => left.has(n)) };
    }
    function filteredCases() {
      const keyword = state.query.trim().toLocaleLowerCase();
      return pairs.filter(row => {
        const { left, right } = row;
        if (state.correctness && right.correctness !== state.correctness) return false;
        const leftHit = left?.issue_types.includes(state.issue), rightHit = right.issue_types.includes(state.issue);
        if (state.issue && !rightHit && !(comparison() && leftHit)) return false;
        if (comparison() && state.change !== "all") {
          const list = changes(row)[state.change];
          if (state.issue ? !list.includes(state.issue) : !list.length) return false;
        }
        return !keyword || [right.index, right.item_id, right.query, right.rationale, left?.rationale].map(text).join(" ").toLocaleLowerCase().includes(keyword);
      });
    }
    function detail(row, group) {
      const fields = [["sessionid", row.sessionid], ["context", row.context], ["answer", row.answer], ["rationale", row.rationale], ["全部问题类型", row.issue_types.join("；") || "无"], ["是否低级", row.is_low_level], ["执行链路", row.execution_routes], ["维度分数", row.rubric], ["维度理由", row.rubric_reasons], ["评估耗时（秒）", row.duration_s || row.latency_s], ["video_path", row.video_path]];
      return `<div><strong>${esc(group)} · ${verdict(row)}</strong>${fields.filter(([, v]) => v !== "" && v != null).map(([k, v]) => `<p><span class="or-muted">${k}</span><br>${esc(text(v))}</p>`).join("")}<p><span class="or-muted">原录屏</span>${videoLinks(row)}</p>${link(row.share_url, "分享链接")}</div>`;
    }
    function renderCases() {
      const rows = filteredCases(), pageCount = Math.max(1, Math.ceil(rows.length / state.size));
      state.page = Math.max(1, Math.min(state.page, pageCount));
      $("case-count").textContent = `${state.issue || "全部有效 Case"} · ${rows.length} 条${comparison() ? "（两组共同有效集合）" : ""}`;
      $("tags").innerHTML = topIssues(5).map(r => `<button data-issue="${esc(r.name)}" aria-pressed="${state.issue === r.name}">${esc(r.name)}</button>`).join("");
      const heads = ["index / 题号", "query", ...(comparison() ? ["对照组", "实验组", "问题变化", "对照组录屏", "实验组录屏"] : ["判定", "理由摘要", "原录屏"]), "详情"];
      let body = `<table class="or-case-table"><thead><tr>${heads.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>`;
      for (const row of rows.slice((state.page - 1) * state.size, state.page * state.size)) {
        const { right, left } = row;
        let change = "";
        if (comparison()) {
          const all = changes(row);
          change = [["new", "新增问题"], ["resolved", "问题消失"], ["persistent", "持续存在"]].filter(([k]) => state.issue ? all[k].includes(state.issue) : all[k].length).map(([, v]) => v).join(" / ") || "无问题变化";
        }
        body += `<tr><td class="or-id">${esc(right.index === "" || right.index == null ? right.item_id : right.index)}</td><td class="or-query">${esc(right.query)}</td>${comparison()
          ? `<td>${verdict(left)}</td><td>${verdict(right)}</td><td>${esc(change)}</td><td>${videoLinks(left)}</td><td>${videoLinks(right)}</td>`
          : `<td>${verdict(right)}</td><td class="or-snippet">${esc(text(right.rationale).slice(0, 110))}${text(right.rationale).length > 110 ? "…" : ""}</td><td>${videoLinks(right)}</td>`
        }<td><button class="or-link" data-detail="${row.key}">${state.open === row.key ? "收起" : "查看"}</button></td></tr>`;
        if (state.open === row.key) body += `<tr><td colspan="${heads.length}"><div class="or-detail">${left ? detail(left, baseline.group_label) : ""}${detail(right, comparison() ? target.group_label : "当前批次")}</div></td></tr>`;
      }
      body += `</tbody></table>${rows.length ? "" : '<div class="or-empty">当前条件下没有 Case</div>'}`;
      $("cases").innerHTML = body;
      $("page-label").textContent = `${state.page} / ${pageCount}`;
      root.querySelector('[data-page="-1"]').disabled = state.page === 1;
      root.querySelector('[data-page="1"]').disabled = state.page === pageCount;
    }
    function renderConclusion() {
      if (!comparison()) { $("conclusion").textContent = payload.statistics.conclusion; return; }
      const n = pair.valid_pair_count;
      const leftNok = pairs.filter(r => r.left.correctness === "nok").length;
      const rightNok = pairs.filter(r => r.right.correctness === "nok").length;
      const nokDelta = n ? (rightNok - leftNok) / n : null;
      $("conclusion").innerHTML = `${esc(pair.target_label)} 相对对照组：<strong class="${rateClass(pair)}">${esc(pair.ok_rate_change_label)}</strong>，OK 率差值 ${signed(pair.ok_rate_delta, true)}；NOK 率差值 <span class="${deltaClass(nokDelta)}">${signed(nokDelta, true)}</span>。${nokDelta > 0 ? "需同时关注新增执行错误。" : ""}<span class="or-muted"> 两组共同有效集合；优化/劣化阈值为 ±${(payload.ok_rate_close_threshold * 100).toFixed(2)}pp，边界内为接近。</span>`;
    }
    function resetCasePage() { state.page = 1; state.open = null; }
    function onClick(event) {
      const issue = event.target.closest("[data-issue]");
      if (issue && root.contains(issue)) {
        state.issue = issue.dataset.issue; state.correctness = ""; resetCasePage(); render(); return;
      }
      const b = event.target.closest("button");
      if (!b || !root.contains(b)) return;
      if (b.dataset.view) { state.view = b.dataset.view; render(); }
      else if (b.dataset.change) { state.change = b.dataset.change; resetCasePage(); render(); }
      else if ("correctness" in b.dataset) { state.correctness = b.dataset.correctness; state.issue = ""; state.change = "all"; resetCasePage(); render(); }
      else if ("detail" in b.dataset) { const key = Number(b.dataset.detail); state.open = state.open === key ? null : key; renderCases(); }
      else if ("page" in b.dataset) { state.page += Number(b.dataset.page); state.open = null; renderCases(); }
    }
    function onChange(event) {
      const name = event.target.dataset.or;
      if (name === "group") { state.group = event.target.value; state.change = "all"; state.correctness = ""; resetCasePage(); prepare(); render(); }
      if (name === "issue") { state.issue = event.target.value; resetCasePage(); render(); }
      if (name === "correctness-filter") { state.correctness = event.target.value; resetCasePage(); renderCases(); }
      if (name === "size") { state.size = Number(event.target.value); resetCasePage(); renderCases(); }
      if (name === "sort") { state.sort = event.target.value; drawIssues(); }
    }
    function onInput(event) {
      if (event.target.dataset.or === "search") { state.query = event.target.value; resetCasePage(); renderCases(); }
    }
    function update(next) {
      if (disposed) return;
      payload = next;
      if (!payload) { root.replaceChildren(); baseline = target = pair = null; pairs = []; issueRows = []; return; }
      const nextGeneration = `${payload.kind}:${payload.task_id || payload.baseline_task_id}`;
      if (generation !== nextGeneration) {
        Object.assign(state, { group: "", issue: "", change: "all", correctness: "", query: "", page: 1, open: null });
      }
      generation = nextGeneration;
      prepare();
      if (!state.issue && !state.correctness) state.issue = topIssues(1)[0]?.name || "";
      render();
    }
    root.addEventListener("click", onClick);
    root.addEventListener("change", onChange);
    root.addEventListener("input", onInput);
    if (global.ResizeObserver) {
      observer = new ResizeObserver(() => {
        const width = root.clientWidth;
        if (width === observedWidth) return;
        observedWidth = width;
        global.cancelAnimationFrame(resizeFrame);
        resizeFrame = global.requestAnimationFrame(() => { if (!disposed && state.view === "radar") drawRadar(); });
      });
      observer.observe(root);
    }
    update(initialPayload);
    return {
      update,
      destroy() {
        disposed = true; observer?.disconnect(); global.cancelAnimationFrame(resizeFrame);
        root.removeEventListener("click", onClick); root.removeEventListener("change", onChange); root.removeEventListener("input", onInput);
        root.replaceChildren(); payload = baseline = target = pair = null; pairs = []; issueRows = [];
      },
    };
  }
  global.AutoEvalOperationReport = { mount };
})(globalThis);
