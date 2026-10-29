"""Task actions distinguish selected reruns from independent new versions."""

from pathlib import Path
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / "src/auto_eval/web/static"


def run_actions_script(assertions: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the task action tests")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    modes = js[js.index("    const versionModes = ["):js.index("    const pipelineConfig =")]
    counters = js[js.index("    const activeTaskItemCount ="):js.index("    const unfinishedRerunRows =")]
    functions = js[js.index("    async function createTaskVersion("):js.index("    function exportCsv()")]
    selection = js[js.index("    function selectFailedResults()"):js.index("    function clearRerunSelection()")]
    unfinished = js[js.index("    const unfinishedRerunRows ="):js.index("    const allPagedResultsSelected =")]
    script = """
    const assert = require('assert').strict;
    const ref = value => ({value});
    const computed = fn => ({get value() { return fn(); }});
    const taskId = ref('source'), running = ref(false), versionBusy = ref(false);
    const versionMode = ref('evaluate_only'), versionScope = ref('all'), versionName = ref('');
    const selectedProviderId = ref(''), selectedProviderModel = ref('');
    const concurrency = ref(35), evalTimeout = ref(300), loadedTaskOptions = ref({});
    const items = ref([{id:'a'}, {id:'b'}, {id:'c'}, {id:'d',dataset_status:'excluded'}]);
    const results = ref([{index:0,correctness:'nok'}, {index:1,error:'timeout'}]);
    const selectedRerunIndices = ref(new Set());
    const itemProgress = ref({2:{status:'cancelled'}}), isMultiGroupMode = ref(false);
    const requests = [], alerts = [], confirmations = [];
    const alert = value => alerts.push(value);
    const confirm = value => { confirmations.push(value); return true; };
    const fetch = async (url, options) => {
      requests.push({url, body:JSON.parse(options.body)});
      return {ok:true, json:async () => ({task_id:'child'})};
    };
    const loadHistoryTask = async () => {}, loadHistory = () => {};
    const requestRateLimitPayload = () => ({max_requests:9,window_seconds:1});
    const providerApiErrorText = () => 'failure';
    """ + modes + counters + unfinished + functions + selection + "\n(async () => {\n" + assertions + "\n})().catch(error => {console.error(error);process.exit(1);});"
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_select_failed_includes_missing_and_cancelled_but_not_nok_or_excluded():
    run_actions_script("""
    assert.equal(activeTaskItemCount.value, 3);
    selectFailedResults();
    assert.deepEqual([...selectedRerunIndices.value], [1,2]);
    assert.equal(requests.length, 0);
    results.value = [{index:0,correctness:'nok'}, {index:1,correctness:'ok'}, {index:2,correctness:'no_support'}];
    selectFailedResults();
    assert.equal(selectedRerunIndices.value.size, 0);
    results.value[1].evaluation_status = 'not_evaluated';
    results.value.push({index:3,error:'excluded'}, {index:99,error:'invalid'});
    selectFailedResults();
    assert.deepEqual([...selectedRerunIndices.value], [1]);
    """)


def test_successful_extraction_is_not_selected_as_a_failure():
    run_actions_script("""
    loadedTaskOptions.value = {execution_mode:'extract_only'};
    results.value = [{index:0,evaluation_status:'not_evaluated',preparation_status:'ready'},
      {index:1,evaluation_status:'not_evaluated',preparation_status:'error',error:'抽帧失败'}];
    selectFailedResults();
    assert.deepEqual([...selectedRerunIndices.value], [1,2]);
    isMultiGroupMode.value = true;
    results.value = [{index:0,group_results:[{evaluation_status:'error'}]}, {index:1}];
    selectFailedResults();
    assert.deepEqual([...selectedRerunIndices.value], [0,2]);
    """)


def test_version_modes_use_correct_fork_payload_and_filter_selected_items():
    run_actions_script("""
    for (const [mode, action] of [['evaluate_only','reuse_frames'], ['extract_only','refresh_frames'], ['full','refresh_frames']]) {
      versionMode.value = mode;
      await startTaskVersion();
      const request = requests.at(-1);
      assert.equal(request.url, '/api/eval/source/fork');
      assert.equal(request.body.action, action);
      assert.equal(request.body.execution_mode, mode);
      assert.equal(request.body.item_indices, null);
    }
    versionScope.value = 'selected';
    selectedRerunIndices.value = new Set([3,2,99,-1,0]);
    versionName.value = '  标准V2  ';
    assert.equal(selectedVersionItemCount.value, 2);
    await startTaskVersion();
    assert.deepEqual(requests.at(-1).body.item_indices, [0,2]);
    assert.equal(requests.at(-1).body.dataset_name, '标准V2');
    selectedRerunIndices.value = new Set([3]);
    const count = requests.length;
    await startTaskVersion();
    assert.equal(requests.length, count);
    assert.equal(alerts.length, 1);
    assert.equal(versionBusy.value, false);
    """)


def test_extraction_quick_action_is_independent_of_new_version_form():
    run_actions_script("""
    loadedTaskOptions.value = {execution_mode:'extract_only'};
    versionMode.value = 'full'; versionScope.value = 'selected'; versionName.value = '未提交的名称';
    await evaluatePreparedFrames();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].body.action, 'reuse_frames');
    assert.equal(requests[0].body.execution_mode, 'evaluate_only');
    assert.equal(requests[0].body.item_indices, null);
    assert.equal(requests[0].body.dataset_name, '');
    assert.equal(versionName.value, '未提交的名称');
    assert.ok(confirmations[0].includes('3 条'));
    """)


def test_new_version_controls_are_grouped_and_bound():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<details class="task-version-tools" :open="versionPanelOpen"' in html
    assert 'v-model="versionMode"' in html
    assert '@click="startTaskVersion"' in html
    assert 'versionScope===\'selected\' && !selectedVersionItemCount' in html
    assert '继续未完成项' not in html
    assert '@click="evaluatePreparedFrames"' in html
    assert '@click="selectFailedResults"' in html
    assert '@click="startRerun([...selectedRerunIndices])"' in html
    assert html.count('重跑选中项（{{ selectedRerunCount }}）') == 2
    assert '重试抽帧选中项' not in html
    assert '重跑选中 Case' not in html
    assert '全量重跑（新任务）' not in html
    assert '复用抽帧新建评估</button>' not in html
