"""Results can be browsed together, without splitting a multi-turn conversation."""
from pathlib import Path
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / "src/auto_eval/web/static"


def run_script(assertions: str, *, bindings: bool = False) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend regression tests")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    helpers = js[js.index("function resultConversationMeta("):js.index("\ncreateApp({")]
    setup = """
const assert = require('assert').strict;
const ref = value => ({value});
const computed = fn => ({get value() {return fn();}});
const single = index => ({index,item_id:`s${index}`,query:`独立${index}`,correctness:'ok',category:'operation'});
const round = (index,id,turn,total=3) => ({...single(index),evaluation_scope:'latest_turn',conversation_id:id,turn_index:turn,turn_count:total});
"""
    if bindings:
        setup += """
const results = ref([]), items = ref([]), mode = ref('operation'), activeSkill = ref('');
const resultQuery = ref(''), correctnessFilter = ref(''), problemDimFilter = ref('');
const summary = ref({}), isSingleApiDataset = ref(false), isMultiGroupMode = ref(false);
const resultPageSize = ref(10), resultPage = ref(1);
"""
        helpers += js[js.index("    const skillTabs = computed("):js.index("    const rubricDims = computed(")]
        helpers += js[js.index("    const filteredResults = computed("):js.index("    const multiGroupColumns = computed(")]
    subprocess.run([node, "-e", setup + helpers + assertions], check=True, capture_output=True, text=True)


def test_soft_page_size_keeps_whole_conversations_and_actual_row_offsets():
    run_script("""
const rows = [...Array.from({length:9},(_,i)=>single(i)), ...[1,2,3].map(t=>round(8+t,'chat-a',t)),
  ...Array.from({length:11},(_,i)=>round(12+i,'chat-b',i+1,11)),single(23)];
const original = JSON.stringify(rows);
const pages = buildResultPages(rows,10,[],true);
assert.deepEqual(pages.map(p=>p.rows.length),[12,11,1]);
assert.deepEqual(pages.map(p=>p.offset),[0,12,23]);
assert.deepEqual(pages[0].headings.get(rows[9]),{id:'chat-a',visible:3,total:3});
assert.deepEqual(pages[1].headings.get(rows[12]),{id:'chat-b',visible:11,total:11});
assert.deepEqual(pages.flatMap(p=>p.rows), rows);
assert.equal(pages[0].rows[9],rows[9]);
assert.equal(JSON.stringify(rows),original);
for (const size of [20,50]) {
  const other = buildResultPages(rows,size,[],true);
  for (const id of ['chat-a','chat-b']) {
    assert.equal(other.filter(p=>p.rows.some(r=>r.conversation_id===id)).length,1);
  }
}
""")


def test_completion_order_and_failed_rounds_do_not_split_a_conversation():
    run_script("""
const rows = [round(2,'chat',3),single(3),round(0,'chat',1),{...round(1,'chat',2),error:'输入无效',category:undefined}];
const pages = buildResultPages(rows,2,[],true);
assert.deepEqual(pages.map(p=>p.rows.map(r=>r.index)),[[0,1,2],[3]]);
assert(pages[0].rows[1].error);
assert.equal(rows[0].index,2);
""")


def test_group_metadata_uses_items_for_older_or_failed_results():
    run_script("""
const items = [round(0,'actual',1),round(1,'actual',2),round(2,'actual',3)];
const rows = [single(2),{...single(1),error:'图片加载失败'},single(0)];
const pages = buildResultPages(rows,1,items,true);
assert.equal(pages.length,1);
assert.deepEqual(pages[0].rows.map(r=>resultConversationMeta(r,items).turnIndex),[1,2,3]);
assert.equal(pages[0].headings.get(rows[2]).id,'actual');
const legacy = {evaluation_scope:'latest_turn',turns:[{id:'first'}],turn_count:3,turn_index:2};
assert.equal(resultConversationMeta(single(0),[legacy]).id,'first');
""")


def test_business_ids_and_legacy_whole_conversations_are_not_grouping_keys():
    run_script("""
const rows = [0,1,2].map(i=>({...single(i),case_id:'same',session_id:'same'}));
rows.push({...single(3),evaluation_scope:'conversation',conversation_id:'same',turn_count:3});
const pages = buildResultPages(rows,2,[],true);
assert.deepEqual(pages.map(p=>p.rows.length),[2,2]);
assert(pages.every(p=>p.headings.size===0));
assert.equal(resultConversationMeta(rows[0],[]),null);
""")


def test_other_modes_and_plain_singles_keep_existing_paging_and_order():
    run_script("""
const rows = [single(5),single(1),single(2)];
assert.deepEqual(buildResultPages(rows,2,[],true).map(p=>p.rows.map(r=>r.index)),[[5,1],[2]]);
const rounds = [round(0,'c',1),round(1,'c',2),round(2,'c',3)];
assert.deepEqual(buildResultPages(rounds,2,[],false).map(p=>p.rows.length),[2,1]);
const empty = buildResultPages([],10,[],true);
assert.equal(empty.length,1); assert.equal(empty[0].offset,0); assert.equal(empty[0].rows.length,0);
""")


def test_all_tab_is_default_includes_failures_and_preserves_explicit_filters():
    run_script("""
results.value = [round(0,'chat',1),{...round(1,'chat',2),error:'图片加载失败',category:undefined,correctness:undefined},
  {...round(2,'chat',3),correctness:'nok'}, {...single(3),category:'another',category_display:'其他类'}];
assert.deepEqual(skillTabs.value[0],{key:'',label:'全部结果',count:4});
assert.equal(filteredResults.value.length,4);
assert.deepEqual(pagedResults.value.map(r=>r.index),[0,1,2,3]);
activeSkill.value='__error__';
assert.deepEqual(pagedResults.value.map(r=>r.index),[1]);
assert.deepEqual(resultConversationHeading(results.value[1]),{id:'chat',visible:1,total:3});
activeSkill.value=''; correctnessFilter.value='nok';
assert.deepEqual(pagedResults.value.map(r=>r.index),[2]);
assert.equal(resultTurnLabel(results.value[2]),'第 3 / 3 轮');
correctnessFilter.value=''; resultQuery.value='图片加载失败';
assert.deepEqual(pagedResults.value.map(r=>r.index),[1]);
resultQuery.value='chat';
assert.deepEqual(pagedResults.value.map(r=>r.index),[0,1,2]);
""", bindings=True)


def test_page_clamping_and_row_numbers_use_actual_pages_not_nominal_size():
    run_script("""
results.value = [1,2,3].map(t=>round(t-1,'chat',t));
results.value.push(single(3)); resultPageSize.value=2;
assert.equal(pageCount.value,2);
resultPage.value=2;
assert.equal(resultPageOffset.value,3);
assert.deepEqual(pagedResults.value.map(r=>r.index),[3]);
resultPage.value=99;
assert.equal(resultPageOffset.value,3);
correctnessFilter.value='nok';
assert.equal(pageCount.value,1); assert.equal(resultPageOffset.value,0); assert.equal(pagedResults.value.length,0);
""", bindings=True)


def test_template_exposes_group_headers_turns_and_all_view_stays_default_on_load():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'class="result-conversation-heading"' in html
    assert "resultTurnLabel(r)" in html
    assert "resultPageOffset+i+1" in html
    assert "(resultPage-1)*resultPageSize" not in html
    assert "每页目标" in html
    assert "activeSkill.value = skillTabs.value[0].key" not in js
