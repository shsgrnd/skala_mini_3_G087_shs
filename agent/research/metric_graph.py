"""MetricAgent: 지정 시스템의 Tool 응답을 수집하고 이름·상태·유한한 수치·시각을 검증한다.
다른 시스템의 응답이나 잘못된 수치에는 근거를 부여하지 않는다.
수집→해석 또는 skip으로 끝나는 독립 compiled StateGraph다.
"""
import math
from datetime import datetime

from langgraph.graph import StateGraph, START, END
from langsmith import traceable

from agent.config import load_known_systems
from .common import ResearchState, collect, evidence, result


class MetricState(ResearchState):
    metric: dict


def build_metric_graph(deps):
    @traceable(name='MetricResearch')
    async def fetch(state):
        async def operation():
            if state['target'] not in load_known_systems():
                raise ValueError('Unknown metric target')
            return await deps.mcp.call_tool('get_monitoring_status', {'system_name': state['target']})
        return {'io': await collect(operation, deps.config.research_timeout_seconds)}

    def interpret(state):
        io, target = state['io'], state['target']
        if io['error']:
            return {'result': result('metric', target, io)}
        try:
            data = io['payload']
            if data['system_name'] != target or data['status'] not in ('healthy','degraded','down'):
                raise ValueError('Unexpected metric target/status')
            for field in ('cpu_percent','memory_percent'):
                if type(data[field]) not in (int,float) or not math.isfinite(data[field]) or not 0 <= data[field] <= 100:
                    raise ValueError('Invalid metric value')
            datetime.fromisoformat(data['last_checked'])
            if not isinstance(data['alerts'], list) or any(not isinstance(a,str) for a in data['alerts']):
                raise ValueError('Invalid alerts')
            if type(data.get('synthetic', False)) is not bool:
                raise ValueError('Invalid metric provenance')
            ev = evidence('monitoring', target, data['last_checked'], data, synthetic=data.get('synthetic', False),
                          uri='tool://ops/get_monitoring_status')
            findings = [f"{target}: {data['status']}, CPU {data['cpu_percent']}%, memory {data['memory_percent']}%"]
            return {'metric': data, 'result': result('metric', target, io, evidence=[ev], findings=findings)}
        except (KeyError,TypeError,ValueError):
            return {'result': result('metric', target, io, error='invalid_metric_evidence')}

    g = StateGraph(MetricState)
    g.add_node('MetricFetch', fetch); g.add_node('MetricInterpret', interpret)
    g.add_node('MetricSkipped', lambda s: {'result': result('metric',s.get('target'),status='skipped')})
    g.add_conditional_edges(START, lambda s: 'MetricFetch' if s.get('target') else 'MetricSkipped')
    g.add_edge('MetricFetch','MetricInterpret'); g.add_edge('MetricInterpret',END); g.add_edge('MetricSkipped',END)
    return g.compile(name='MetricAgent')
