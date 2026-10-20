"""RunbookAgent: Supervisor/S2가 선택한 유형의 본문을 MCP Resource에서 확인한다.
문서 미존재 문자열을 성공 근거로 취급하지 않는다. 절차는 수행 결과가 아닌 권고다.
source URI·본문 해시 ID·문서 줄 위치를 함께 보존한다.
"""
import re

from langgraph.graph import StateGraph, START, END
from langsmith import traceable

from agent.config import ROOT
from .common import ResearchState, collect, evidence, result


class RunbookState(ResearchState):
    body: str


def build_runbook_graph(deps):
    @traceable(name='RunbookResearch')
    async def fetch(state):
        async def operation():
            target = state['target']
            if target not in {p.stem for p in (ROOT/'data/runbooks').glob('*.md')}:
                raise ValueError('Unknown runbook target')
            server, template = await deps.mcp.find_resource_template('get_incident_runbook')
            uri = template.format(incident_type=target)
            body = await deps.mcp.read_resource(uri,server=server)
            if not isinstance(body,str) or not body.strip() or body.startswith('# Runbook Not Found'):
                raise ValueError('Runbook unavailable')
            return {'body': body, 'uri': uri}
        return {'io': await collect(operation, deps.config.research_timeout_seconds)}

    def interpret(state):
        io, target = state['io'], state['target']
        if io['error']:
            return {'result': result('runbook',target,io)}
        body, uri = io['payload']['body'], io['payload']['uri']
        ev = evidence('runbook', None, None, body, uri=uri,
                      location={'start_line':1,'end_line':len(body.splitlines())}, incident_type=target)
        findings = [line.strip() for line in body.splitlines() if re.match(r'^\d+\.',line)][:3]
        return {'body':body,'result':result('runbook',target,io,evidence=[ev],findings=findings)}

    g = StateGraph(RunbookState)
    g.add_node('RunbookFetch',fetch); g.add_node('RunbookInterpret',interpret)
    g.add_node('RunbookSkipped',lambda s: {'result':result('runbook',s.get('target'),status='skipped')})
    g.add_conditional_edges(START,lambda s:'RunbookFetch' if s.get('target') else 'RunbookSkipped')
    g.add_edge('RunbookFetch','RunbookInterpret'); g.add_edge('RunbookInterpret',END); g.add_edge('RunbookSkipped',END)
    return g.compile(name='RunbookAgent')
