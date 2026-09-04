// 可选浏览器回归。先运行 test_operation_report.py 生成 HTML；
// NODE_PATH 指向已安装 playwright 的 node_modules，无模型调用。
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");
const assert = require("assert");
const { chromium } = require("playwright");

(async () => {
  const fixtureDir = path.resolve(process.argv[2]);
  const output = path.resolve(process.argv[3]);
  fs.mkdirSync(output, { recursive: true });
  const singlePath = path.join(fixtureDir, "test_report_api_and_html_expor0/single.html");
  const comparisonPath = path.join(fixtureDir, "test_comparison_html_and_live_0/comparison.html");
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1180, height: 900 }, offline: true });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(pathToFileURL(singlePath).href);
    await page.waitForSelector(".operation-report");
    await page.selectOption('[data-or="issue"]', "回复语义重复");
    assert.equal(await page.locator(".or-case-table tbody > tr").count(), 10);
    assert.equal(await page.locator('.or-case-table a').filter({ hasText: "域名站" }).count(), 10);
    await page.locator('[data-page="1"]').click();
    assert.equal(await page.locator(".or-case-table tbody > tr").count(), 5);
    await page.selectOption('[data-or="size"]', "20");
    assert.equal(await page.locator(".or-case-table tbody > tr").count(), 15);
    await page.locator('[data-or="search"]').fill("simple_001");
    assert.equal(await page.locator(".or-case-table tbody > tr").count(), 1);
    await page.locator('[data-detail]').first().click();
    assert((await page.locator(".or-detail").innerText()).includes("session_0"));
    await page.locator('[data-or="search"]').fill("");
    await page.locator('[data-view="radar"]').click();
    assert.equal(await page.locator(".or-radar polygon").count(), 4);
    const axisIssue = await page.locator(".or-radar-label").first().getAttribute("data-issue");
    await page.locator(".or-radar-label").first().click();
    assert((await page.locator('[data-or="case-count"]').innerText()).includes(axisIssue));
    await page.screenshot({ path: path.join(output, "single-radar.png"), fullPage: true });

    await page.goto(pathToFileURL(comparisonPath).href);
    await page.selectOption('[data-or="issue"]', "任务结果错误");
    await page.locator('[data-change="resolved"]').click();
    assert((await page.locator('[data-or="case-count"]').innerText()).includes("1 条"));
    const links = await page.locator(".or-case-table a").evaluateAll(as => as.map(a => a.href));
    assert(links.some(url => url.includes("/control/1.mp4")));
    assert(links.some(url => url.includes("/experiment/1.mp4")));
    await page.selectOption('[data-or="issue"]', "内部过程信息泄露");
    await page.locator('[data-change="new"]').click();
    assert((await page.locator('[data-or="case-count"]').innerText()).includes("1 条"));
    await page.locator('[data-detail]').first().click();
    assert.equal(await page.locator(".or-detail > div").count(), 2);
    await page.locator('[data-view="table"]').click();
    assert((await page.locator('[data-or="issues"]').innerText()).includes("占比差值"));
    await page.locator('[data-view="radar"]').click();
    assert.equal(await page.locator(".or-radar polygon").count(), 5);
    for (const width of [1180, 736, 360]) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(60);
      const overflow = await page.locator(".operation-report").evaluate(el => el.scrollWidth > el.clientWidth + 1);
      assert(!overflow, "report overflow at " + width);
      await page.screenshot({ path: path.join(output, "comparison-" + width + ".png"), fullPage: true });
    }
    await page.goto(pathToFileURL(path.join(fixtureDir, "test_html_exports_are_self_con0/single.html")).href);
    assert.equal(await page.evaluate(() => globalThis.INJECTED), undefined);
    assert.equal(await page.locator("img").count(), 0);
    assert.deepEqual(errors, []);
    await context.close();
    console.log("PASS offline: charts, both recording links, pagination, filters, paired cases, XSS, 1180/736/360px.");

    if (process.argv[4]) {
      const live = await browser.newContext({ viewport: { width: 1280, height: 1000 }, acceptDownloads: true });
      const web = await live.newPage(), liveErrors = [];
      web.on("pageerror", e => liveErrors.push(e.message));
      await web.goto(process.argv[4], { waitUntil: "domcontentloaded" });
      await web.getByRole("button", { name: "任务类对比分析", exact: true }).click();
      const html = fs.readFileSync(comparisonPath, "utf8");
      const payload = JSON.parse(html.match(/id="operation-report-data" type="application\/json">(.*?)<\/script>/s)[1]);
      for (const group of payload.groups) {
        const rows = group.cases.map(row => ({
          index: row.index, query: row.query, context: row.context, answer: row.answer,
          correctness: row.correctness, issue_types: row.issue_types, rationale: row.rationale,
          video_url_domain: row.video_url_domain, video_url_ip: row.video_url_ip,
        }));
        await web.locator(".comparison-page input[type=file]").setInputFiles({
          name: group.task_id + ".jsonl", mimeType: "application/x-ndjson",
          buffer: Buffer.from(rows.map(r => JSON.stringify(r)).join("\n")),
        });
        await web.waitForFunction(n => document.querySelectorAll(".comparison-source-card").length >= n, payload.groups.indexOf(group) + 1);
      }
      await web.getByRole("button", { name: "生成对比分析", exact: true }).click();
      await web.waitForSelector(".history-comparison-result .operation-report");
      await web.locator('.operation-report [data-view="radar"]').click();
      await web.screenshot({ path: path.join(output, "web-comparison.png"), fullPage: true });
      const downloadEvent = web.waitForEvent("download");
      await web.locator(".history-comparison-result").getByRole("button", { name: "导出 HTML 报告" }).click();
      const download = await downloadEvent;
      assert(download.suggestedFilename().endsWith(".html"));
      await download.saveAs(path.join(output, download.suggestedFilename()));
      assert.deepEqual(liveErrors, []);
      console.log("PASS Web: uploaded two result sets, generated live report, switched radar, downloaded .html.");
      // 只模拟只读历史响应，不写真实历史、不触发模型任务。
      const singleWeb = await live.newPage();
      singleWeb.on("pageerror", e => liveErrors.push(e.message));
      const singleHtml = fs.readFileSync(singlePath, "utf8");
      const singleReport = JSON.parse(singleHtml.match(/id="operation-report-data" type="application\/json">(.*?)<\/script>/s)[1]);
      const history = {
        task_id: singleReport.task_id, dataset_name: singleReport.dataset_name,
        mode: "operation", status: "done", options: {}, total: 60, done: 60, done_total: 60,
        items: singleReport.cases.map((r, i) => ({ id: r.item_id, query: r.query, source_data: { index: r.index } })),
        results: singleReport.cases.map((r, i) => ({ ...r, index: i })),
        summary: { operation_statistics: singleReport.statistics },
      };
      let reportLoads = 0;
      await singleWeb.route("**/api/history?*", route => route.fulfill({ json: { items: [history], total: 1, page: 1, page_size: 10 } }));
      await singleWeb.route("**/api/history/report-control?*", route => route.fulfill({ json: history }));
      await singleWeb.route("**/api/eval/report-control/report", route => {
        reportLoads++;
        return route.fulfill({ json: { ...singleReport, dataset_name: "报告刷新 " + reportLoads } });
      });
      await singleWeb.goto(process.argv[4], { waitUntil: "domcontentloaded" });
      await singleWeb.locator(".history-section > summary").click();
      await singleWeb.locator(".history-section").getByRole("button", { name: "加载", exact: true }).click();
      await singleWeb.waitForSelector(".operation-statistics .operation-report");
      await singleWeb.getByText("报告刷新 1", { exact: true }).waitFor();
      await singleWeb.locator(".history-section").getByRole("button", { name: "加载", exact: true }).click();
      await singleWeb.getByText("报告刷新 2", { exact: true }).waitFor();
      await singleWeb.locator(".operation-statistics").screenshot({ path: path.join(output, "web-single.png") });
      assert.equal(reportLoads, 2);
      assert.deepEqual(liveErrors, []);
      console.log("PASS Web single: history load, report refresh on reloaded snapshot, recording links.");
      await live.close();
    }
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
