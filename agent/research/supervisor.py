"""전문 그래프의 fan-out/fan-in과 근거 통합을 담당한다.
같은 (agent,target)은 한 요청에서 한 번 수집하고 replan에서는 완료/실패 결과를 재사용한다.
metric와 log는 같은 wave에, gap runbook은 다음 wave에 실행될 수 있다.
종류별 status와 source별 synthetic/time을 유지하며 누락된 수치를 만들어 채우지 않는다.
"""
import asyncio
import time
from datetime import datetime

from langsmith import traceable

from .metric_graph import build_metric_graph
from .log_graph import build_log_graph
from .runbook_graph import build_runbook_graph

KINDS = {'monitoring':'metric', 'logs':'log', 'runbook':'runbook'}
EVENTS = {'metric':'MetricResearch', 'log':'LogResearch', 'runbook':'RunbookResearch'}


def build_research_graphs(deps):
    return {'metric':build_metric_graph(deps), 'log':build_log_graph(deps), 'runbook':build_runbook_graph(deps)}


@traceable(name='ResearchAggregate')
async def research_multi(state, deps):
    started = time.perf_counter()
    tasks = [dict(t) for t in state.get('tasks', [])]
    pending = [t for t in tasks if t.get('agent')=='research' and t['status']=='pending']
    # 실패도 종료 상태다. 같은 gap을 계속 pending으로 되살려 무한 조회하지 않는다.
    previous = {(r['agent'],r['target']):r for r in state.get('research_results', [])}
    jobs = {}
    for task in pending:
        agent = KINDS.get(task.get('kind'))
        if agent:
            key = (agent,task['target'])
            if key not in previous:
                jobs[key] = task
    # Skipped specialists still have their own graph/trace termination path.
    for agent in EVENTS:
        if not any(key[0]==agent for key in jobs):
            jobs[(agent,None)] = None

    async def run(key):
        wall = time.perf_counter()
        agent, target = key
        data = await deps.research_graphs[agent].ainvoke({'target':target})
        result = data['result']
        result['latency_ms'] = round((time.perf_counter()-wall)*1000,3)
        return result

    results = await asyncio.gather(*(run(key) for key in jobs))
    events = list(state.get('events', []))
    for result in results:
        events.append({'type':EVENTS[result['agent']], **{k:v for k,v in result.items() if k!='evidence'},
                       'evidence_ids':[e['id'] for e in result['evidence']]})
        if result['status']!='skipped':
            previous[(result['agent'],result['target'])] = result
    for task in pending:
        result = previous.get((KINDS.get(task.get('kind')), task.get('target')))
        task['status'] = 'done' if result and result['status']=='ok' else 'failed'
        events.append({'type':'TaskCompleted' if task['status']=='done' else 'TaskFailed',
                       'id':task['id'], 'source':{'monitoring':'monitoring','logs':'logs','runbook':'runbook'}.get(task.get('kind')),
                       'target':task.get('target'), 'retries':result['retries'] if result else 0})

    evidence, seen, collected, warnings = [], set(), [], []
    for result in previous.values():
        if result['status']!='ok':
            warnings.append(f"{result['agent']}({result['target']}): {result['status']} / {result['error']}")
        for ev in result['evidence']:
            if ev['id'] in seen:
                continue
            seen.add(ev['id']); evidence.append(ev)
        evs = result['evidence']
        if not evs:
            continue
        if result['agent']=='metric':
            collected.append({'source':'monitoring','data':evs[0]['payload'],'evidence':evs})
        elif result['agent']=='log':
            collected.append({'source':'logs','data':{'system_name':result['target'], 'synthetic':True,
                              'entries':[e['payload'] for e in evs]},'evidence':evs})
        else:
            collected.append({'source':'runbook','data':evs[0]['payload'],'evidence':evs})
    # 같은 시스템이어도 서로 다른 시각의 자료로 원인 상관관계를 확정하지 않는다.
    metric_times = {e['system']:e['timestamp'] for e in evidence if e['source']=='monitoring'}
    for ev in evidence:
        if ev['source']!='synthetic_log' or ev['system'] not in metric_times:
            continue
        a,b = datetime.fromisoformat(ev['timestamp']), datetime.fromisoformat(metric_times[ev['system']])
        if (a.tzinfo is None)!=(b.tzinfo is None) or abs((a-b).total_seconds())>300:
            warning=f"{ev['system']}: 로그와 지표의 시각이 일치하지 않아 원인 상관관계를 확정할 수 없음"
            if warning not in warnings:
                warnings.append(warning)
    if any(e['source']=='synthetic_log' for e in evidence):
        warnings.append('로그는 합성 예시이며 실제 운영 로그를 조회한 결과가 아님')
    statuses = [r['status'] for r in previous.values()]
    status = ('ok' if statuses and all(s=='ok' for s in statuses)
              else 'partial' if evidence else 'failed')
    summary = {'status':status, 'sources':[{'agent':r['agent'],'target':r['target'],'status':r['status'],
                                          'error':r['error']} for r in previous.values()], 'warnings':warnings}
    collected.append({'source':'research_status','data':summary})
    elapsed = time.perf_counter()-started
    events.append({'type':'ResearchAggregate', **summary, 'evidence_count':len(evidence),
                   'latency_ms':round(elapsed*1000,3)})
    return {'tasks':tasks, 'collected':collected, 'evidence':evidence, 'research_results':list(previous.values()),
            'research_status':status, 'retry_count':sum(r['retries'] for r in previous.values()),
            'parallel_elapsed':round(elapsed,3), 'events':events}
