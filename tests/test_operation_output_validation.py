"""Format failures cannot become fabricated review or completion verdicts."""
import json
from pathlib import Path

import pytest

from auto_eval.config import JudgeConfig, load_config
from auto_eval.judges.base import JudgeOutputParseError, JudgeReply
from auto_eval.judges.rubric_judge import RubricJudge
from auto_eval.judges.skill_router import SkillRouter
from auto_eval.observability import current_context
from auto_eval.schema import EvalItem
from auto_eval.web import runner
from auto_eval.web.tasks import Task


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['只有证据摘要', '{}', '{"rubric":{}}',
    '{"correctness":"maybe"}', '[1,2]'])
async def test_operation_missing_verdict_does_not_use_blind_json_repair(content):
    cfg = load_config(Path('config'))

    class Client:
        cfg = JudgeConfig(name='fake', persona='end_user')
        model = 'fake-model'
        persona = '终端用户'

        async def complete(self, *args, **kwargs):
            return JudgeReply(content=content)

        async def repair_json(self, *args, **kwargs):
            pytest.fail('Text-only repair must not invent a missing visual verdict')

    judge = RubricJudge(Client(), cfg.domain_skills['operation'].rubrics,
        skill_router=SkillRouter(cfg.domain_skills))
    item = EvalItem(id='case', question='打开设置', category='operation')
    with pytest.raises(JudgeOutputParseError) as caught:
        await judge.score(item, 'model', '已打开', eval_mode='operation')
    assert caught.value.raw_output == content
    assert caught.value.repair_output == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,recover,expected', [
    ('operation', True, 2), ('operation', False, 2), ('single', False, 1),
])
async def test_format_retry_is_task_only_bounded_and_preserves_media(
    mode, recover, expected, monkeypatch, tmp_path,
):
    cfg = load_config(Path('config'))
    task = Task(id='format-retry', mode=mode,
        items=[{'id': 'case', 'query': '打开设置', 'context': '可信背景',
                'answer': '已打开', 'video_path': '/video.mp4'}],
        options={'judges': [cfg.judges[0].name], 'concurrency': 1})
    calls, prepared = [], []
    trace = tmp_path / 'calls.jsonl'

    def prepare(item, **kwargs):
        prepared.append(dict(item))
        return {**item, 'frames': ['/frame.jpg'], 'frame_count': 1}

    async def evaluate(*args, **kwargs):
        calls.append(dict(args[2]))
        ctx = current_context()
        ctx.judge_trace_callback(str(trace), {'item_id': 'case', 'status': 'success',
            'llm_rounds': [{'content': 'valid' if recover and len(calls) == 2 else 'invalid'}]})
        if not recover or len(calls) == 1:
            raise JudgeOutputParseError('missing correctness', raw_output='invalid',
                repair_output='', judge='fake', model='fake-model')
        return {'index': 0, 'item_id': 'case', 'query': '打开设置',
                'correctness': 'ok', 'rubric': {}, 'total': None}

    monkeypatch.setattr(runner, 'prepare_session_operation_item', prepare)
    monkeypatch.setattr(runner, '_eval_one', evaluate)
    monkeypatch.setattr(runner, '_persist_task', lambda task: True)
    monkeypatch.setattr(runner, '_write_eval_error', lambda *a, **kw: None)
    await runner._run(task, cfg)
    assert len(calls) == expected
    if mode == 'operation':
        assert len(prepared) == 1
        assert all(r['frames'] == ['/frame.jpg'] for r in calls)
        assert calls[0] == calls[-1]
    records = [json.loads(s) for s in trace.read_text().splitlines()]
    assert len(records) == expected
    if mode == 'operation':
        assert records[0]['status'] == 'invalid_output'
        assert records[0]['output_validation_error'] == 'missing correctness'
    if recover:
        assert task.results[0]['correctness'] == 'ok'
        assert records[-1]['model_raw_output'] == 'valid'
    else:
        assert task.results[0].get('correctness') is None
        assert 'error' in task.results[0]
