"""LogAgent: 합성 로그 읽기 Tool만 사용하고 시스템·시각·근거 ID를 보존한다.
현재 합성 예시를 실제 운영 로그로 전환할 때는 이 검증과 보고 정책을 함께 변경해야 한다.
빈 결과는 정상 로그가 없다는 증명이 아니라 partial 수집으로 반환한다.
"""
from datetime import datetime

from langgraph.graph import StateGraph, START, END
from langsmith import traceable

from agent.config import load_known_systems
from .common import ResearchState, collect, evidence, result


class LogState(ResearchState):
    logs: list[dict]


def build_log_graph(deps):
    @traceable(name='LogResearch')
    async def fetch(state):
        async def operation():
            if state['target'] not in load_known_systems():
                raise ValueError('Unknown log target')
            return await deps.mcp.call_tool('get_recent_logs', {'system_name': state['target'], 'limit': 20})
        return {'io': await collect(operation, deps.config.research_timeout_seconds)}

    def interpret(state):
        io, target = state['io'], state['target']
        if io['error']:
            return {'result': result('log', target, io)}
        try:
            data, evs = io['payload'], []
            if data['system_name'] != target or data['synthetic'] is not True or len(data['entries']) > 20:
                raise ValueError('Invalid log response')
            for line in data['entries']:
                if (line['system'] != target or line['synthetic'] is not True
                        or line['level'] not in ('DEBUG','INFO','WARN','ERROR')
                        or not isinstance(line['message'],str) or not 1 <= len(line['message']) <= 1000
                        or not isinstance(line['evidence_id'],str) or not line['evidence_id']):
                    raise ValueError('Invalid log record')
                datetime.fromisoformat(line['timestamp'])
                evs.append(evidence('synthetic_log', target, line['timestamp'], line, synthetic=True,
                                    identifier=f"log:{target}:{line['evidence_id']}", uri='tool://ops/get_recent_logs',
                                    location=f"data/logs/{target}.jsonl#{line['evidence_id']}"))
            if len({e['id'] for e in evs}) != len(evs):
                raise ValueError('Duplicate log IDs')
            findings = [f"합성 로그 {len(evs)}개, 오류/경고 {sum(e['payload']['level'] in ('ERROR','WARN') for e in evs)}개; 실제 운영 원인 증거가 아님"]
            return {'logs': data['entries'], 'result': result('log', target, io, evidence=evs, findings=findings,
                    status='ok' if evs else 'partial', error=None if evs else 'no_recent_logs')}
        except (KeyError,TypeError,ValueError):
            return {'result': result('log', target, io, error='invalid_log_evidence')}

    g = StateGraph(LogState)
    g.add_node('LogFetch',fetch); g.add_node('LogInterpret',interpret)
    g.add_node('LogSkipped',lambda s: {'result': result('log',s.get('target'),status='skipped')})
    g.add_conditional_edges(START,lambda s: 'LogFetch' if s.get('target') else 'LogSkipped')
    g.add_edge('LogFetch','LogInterpret'); g.add_edge('LogInterpret',END); g.add_edge('LogSkipped',END)
    return g.compile(name='LogAgent')
