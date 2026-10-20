"""서비스 단위 테스트: 외부 계정·실모델·ignored scripts 없이 실행되어야 한다.
MCP 대역은 전송 경계만 대체하고 실제 compiled 그래프·검증·집계·재시도를 실행한다.
"""
import asyncio
from dataclasses import replace

import pytest

from agent.config import load_config
from agent.deps import SupervisorDeps
from agent.research.supervisor import build_research_graphs, research_multi


class StubMCP:
    def __init__(self):
        self.calls = []
        self.fail = set()

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.fail:
            raise ValueError('unavailable')
        system = arguments['system_name']
        if name == 'get_monitoring_status':
            return {'system_name': system, 'status': 'degraded', 'cpu_percent': 91.0,
                    'memory_percent': 55.0, 'last_checked': '2026-10-20T09:00:00', 'alerts': ['CPU high']}
        return {'system_name': system, 'synthetic': True, 'entries': [
            {'system': system, 'timestamp': '2026-10-20T09:00:00', 'level': 'ERROR',
             'message': 'Synthetic CPU retry sample', 'evidence_id': 'payments-log-1', 'synthetic': True}]}

    async def find_resource_template(self, name):
        return 'ops', 'runbook://incident-response/{incident_type}'

    async def read_resource(self, uri, server):
        self.calls.append(('read_resource', uri))
        if 'runbook' in self.fail:
            return '# Runbook Not Found\nNo runbook'
        return '# CPU response\n\n1. Check monitoring.\n2. Review logs before making changes.'


def deps():
    d = SupervisorDeps(None, None, StubMCP(), None, 'v2', replace(load_config(), research_strategy='multi'))
    d.research_graphs = build_research_graphs(d)
    return d


@pytest.mark.parametrize('agent,target', [('metric', 'payments-service'), ('log', 'payments-service'), ('runbook', 'cpu-spike')])
def test_each_specialist_is_a_compiled_graph_with_evidence(agent, target):
    d = deps()
    result = asyncio.run(d.research_graphs[agent].ainvoke({'target': target}))['result']
    assert len(d.research_graphs[agent].get_graph().nodes) >= 4
    assert result['agent'] == agent and result['status'] == 'ok'
    assert result['evidence'][0]['id']
    assert result['evidence'][0]['source'] == {'metric': 'monitoring', 'log': 'synthetic_log', 'runbook': 'runbook'}[agent]
    assert result['evidence'][0]['synthetic'] is (agent == 'log')


@pytest.mark.parametrize('agent', ['metric', 'log', 'runbook'])
def test_specialist_can_skip_without_touching_mcp(agent):
    d = deps()
    result = asyncio.run(d.research_graphs[agent].ainvoke({'target': None}))['result']
    assert result['status'] == 'skipped'
    assert not d.mcp.calls


def test_aggregation_replan_does_not_refetch_completed_metrics_or_logs():
    d = deps()
    state = {'tasks': [{'id': 'm', 'agent': 'research', 'kind': 'monitoring', 'target': 'payments-service', 'status': 'pending'},
                       {'id': 'l', 'agent': 'research', 'kind': 'logs', 'target': 'payments-service', 'status': 'pending'}],
             'events': [], 'collected': []}
    first = asyncio.run(research_multi(state, d))
    next_state = {**state, **first}
    next_state['tasks'] = first['tasks'] + [{'id': 'r', 'agent': 'research', 'kind': 'runbook', 'target': 'cpu-spike', 'status': 'pending'}]
    second = asyncio.run(research_multi(next_state, d))
    assert sum(c[0] == 'get_monitoring_status' for c in d.mcp.calls) == 1
    assert sum(c[0] == 'get_recent_logs' for c in d.mcp.calls) == 1
    assert {c['source'] for c in second['collected']} == {'monitoring', 'logs', 'runbook', 'research_status'}
    assert second['research_status'] == 'ok'
    assert len({e['id'] for e in second['evidence']}) == len(second['evidence'])


def test_log_failure_keeps_metric_and_runbook_results():
    d = deps(); d.mcp.fail.add('get_recent_logs')
    state = {'tasks': [{'id': kind, 'agent': 'research', 'kind': kind, 'target': target, 'status': 'pending'}
                       for kind, target in [('monitoring','payments-service'), ('logs','payments-service'), ('runbook','cpu-spike')]], 'events': []}
    result = asyncio.run(research_multi(state, d))
    assert result['research_status'] == 'partial'
    assert {e['source'] for e in result['evidence']} == {'monitoring', 'runbook'}
    assert next(t for t in result['tasks'] if t['kind']=='logs')['status'] == 'failed'


def test_missing_runbook_is_a_failure_and_unknown_system_is_never_queried():
    d = deps(); d.mcp.fail.add('runbook')
    result = asyncio.run(d.research_graphs['runbook'].ainvoke({'target':'cpu-spike'}))['result']
    assert result['status'] == 'failed' and not result['evidence']
    d.mcp.calls.clear()
    result = asyncio.run(d.research_graphs['log'].ainvoke({'target':'../secret'}))['result']
    assert result['status'] == 'failed' and not d.mcp.calls


def test_timeout_retries_are_bounded_and_other_sources_survive():
    d = deps()
    d.config = replace(d.config, research_timeout_seconds=0.01)
    original = d.mcp.call_tool
    calls = []
    async def slow(name, arguments):
        if name == 'get_recent_logs':
            calls.append(name)
            await asyncio.sleep(1)
        return await original(name, arguments)
    d.mcp.call_tool = slow
    result = asyncio.run(d.research_graphs['log'].ainvoke({'target':'payments-service'}))['result']
    assert result['status'] == 'failed' and result['error'] == 'TimeoutError'
    assert result['retries'] == 2 and len(calls) == 3
    metric = asyncio.run(d.research_graphs['metric'].ainvoke({'target':'payments-service'}))['result']
    assert metric['status'] == 'ok'


def test_transient_metric_timeout_recovers_with_one_retry():
    d=deps(); original=d.mcp.call_tool; attempts=[]
    async def flaky(name, arguments):
        attempts.append(name)
        if len(attempts)==1:
            raise asyncio.TimeoutError()
        return await original(name,arguments)
    d.mcp.call_tool=flaky
    result=asyncio.run(d.research_graphs['metric'].ainvoke({'target':'payments-service'}))['result']
    assert result['status']=='ok' and result['retries']==1


def test_wrong_metric_target_and_nonfinite_values_are_not_evidence():
    for field,value in [('system_name','auth-service'), ('cpu_percent',float('nan')),('cpu_percent',True)]:
        d=deps(); original=d.mcp.call_tool
        async def corrupt(name, arguments):
            data=await original(name,arguments)
            return {**data,field:value}
        d.mcp.call_tool=corrupt
        result=asyncio.run(d.research_graphs['metric'].ainvoke({'target':'payments-service'}))['result']
        assert result['status']=='failed' and not result['evidence']


def test_clock_mismatch_is_visible_and_source_evidence_is_preserved():
    d=deps(); original=d.mcp.call_tool
    async def old_logs(name, arguments):
        data=await original(name,arguments)
        if name=='get_recent_logs':
            data['entries'][0]['timestamp']='2026-10-19T09:00:00'
        return data
    d.mcp.call_tool=old_logs
    tasks=[{'id':kind,'agent':'research','kind':kind,'target':'payments-service','status':'pending'} for kind in ['monitoring','logs']]
    result=asyncio.run(research_multi({'tasks':tasks,'events':[]},d))
    summary=next(c['data'] for c in result['collected'] if c['source']=='research_status')
    assert any('시각' in w for w in summary['warnings'])
    assert len(result['evidence'])==2


def test_invalid_research_flag_and_timeout_fail_config(tmp_path, monkeypatch):
    import json
    from agent.config import DEFAULT_CONFIG_PATH
    monkeypatch.setenv('RESEARCH_STRATEGY','typo')
    with pytest.raises(ValueError,match='RESEARCH_STRATEGY'):
        load_config()
    monkeypatch.setenv('RESEARCH_STRATEGY','multi')
    data=json.loads(DEFAULT_CONFIG_PATH.read_text()); data['research_timeout_seconds']=0
    path=tmp_path/'agent.json'; path.write_text(json.dumps(data)); monkeypatch.setenv('AGENT_CONFIG_PATH',str(path))
    with pytest.raises(ValueError,match='research_timeout_seconds'):
        load_config()


def test_mcp_wait_timing_includes_cancelled_queue_without_unlocking_other_call():
    import json
    from types import SimpleNamespace
    from contextlib import asynccontextmanager
    from agent.mcp_client import MCPClient, MCP_TIMINGS
    async def scenario():
        entered,release=asyncio.Event(),asyncio.Event()
        class Session:
            async def call_tool(self,*args):
                entered.set(); await release.wait()
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({'ok':True}))],isError=False)
        mcp=MCPClient(flaky_first_tool_call=False); mcp.sessions={'ops':Session()}
        queued=asyncio.Event(); original_lock=mcp._timed_lock; calls=0
        @asynccontextmanager
        async def observe_queue():
            nonlocal calls
            calls+=1
            if calls==2: queued.set()
            async with original_lock(): yield
        mcp._timed_lock=observe_queue
        timings=[]; token=MCP_TIMINGS.set(timings)
        first=asyncio.create_task(mcp.call_tool('tool',{}))
        await entered.wait()
        second=asyncio.create_task(mcp.call_tool('tool',{})); await queued.wait()
        second.cancel()
        with pytest.raises(asyncio.CancelledError): await second
        assert mcp._lock.locked()
        release.set(); await first; MCP_TIMINGS.reset(token)
        return timings
    timings=asyncio.run(scenario())
    assert len(timings)==2
    assert any(item['service_ms']==0 for item in timings)


def test_model_context_keeps_analysis_as_inference_not_source_evidence():
    import json
    from agent.research.common import render_context
    collected=[{'source':'research_status','data':{'status':'ok','warnings':[]}},
               {'source':'analysis','data':'Possible shared dependency; not verified.'}]
    context=json.loads(render_context(collected))
    assert context['analysis_hypotheses'][0]['text']=='Possible shared dependency; not verified.'
    assert context['evidence']==[]
