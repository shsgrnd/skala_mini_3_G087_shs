"""조사 공통 계약: Tool timeout/retry와 출처 envelope를 한 곳에서 관리한다.
각 그래프가 수집·검증 책임을 가지며 이 모듈은 수집 결과를 전역 보관하지 않는다.
원본 근거는 state/metadata에 보존하고 LLM 입력 사본만 길이를 제한한다.
"""
import asyncio
import hashlib
import json
import time
from typing import TypedDict

from agent.mcp_client import MCP_TIMINGS
from agent.retry import with_retry


class ResearchState(TypedDict, total=False):
    # target은 입력, io는 전송 결과, result는 집계에 넘기는 공개 조사 계약이다.
    target: str | None
    io: dict
    result: dict


async def collect(operation, timeout: float) -> dict:
    started, attempts, timings = time.perf_counter(), 0, []
    # timeout은 잠금 대기까지 포함한 시도별 제한이다. 일시적 TimeoutError만 공통 retry 정책으로 재시도한다.
    token = MCP_TIMINGS.set(timings)

    async def timed():
        nonlocal attempts
        attempts += 1
        return await asyncio.wait_for(operation(), timeout)

    try:
        payload, retries = await with_retry(timed)
        error = None
    # 수집 실패는 결과 계약으로 바꿔 다른 출처를 살린다. 요청 취소(CancelledError)는 삼키지 않는다.
    except Exception as exc:
        payload, retries, error = None, max(0, attempts - 1), type(exc).__name__
    finally:
        MCP_TIMINGS.reset(token)
    return {'payload': payload, 'error': error, 'retries': retries,
            'latency_ms': round((time.perf_counter()-started)*1000, 3),
            'mcp_wait_ms': round(sum(t['wait_ms'] for t in timings), 3),
            'mcp_service_ms': round(sum(t['service_ms'] for t in timings), 3)}


def result(agent, target, io=None, *, evidence=None, findings=None, error=None, status=None):
    io = io or {}
    error = error or io.get('error')
    return {'agent': agent, 'target': target, 'status': status or ('failed' if error else 'ok'),
            'findings': findings or [], 'evidence': evidence or [], 'error': error,
            **{k: io.get(k, 0) for k in ('latency_ms','mcp_wait_ms','mcp_service_ms','retries')}}


def evidence(source, system, timestamp, payload, *, synthetic=False, identifier=None, **metadata):
    # 출처·대상·시각·본문이 같은 관측만 같은 ID를 갖는다. 문서 권고와 관측값은 source로 구분한다.
    digest = hashlib.sha256(json.dumps([source,system,timestamp,payload], sort_keys=True).encode()).hexdigest()
    return {'source': source, 'id': identifier or f'{source}:{digest[:24]}', 'system': system,
            'timestamp': timestamp, 'payload': payload, 'synthetic': synthetic, **metadata}


def render_context(collected):
    """Preserve full evidence in state; bound only the model-facing copy."""
    status = next((c['data'] for c in collected if c['source']=='research_status'), None)
    if status is None:
        return None
    rows, used, omitted = [], 0, 0
    for c in collected:
        for ev in c.get('evidence', []):
            copy = {**ev, 'payload': json.dumps(ev['payload'], ensure_ascii=False)[:1800]}
            text = json.dumps(copy, ensure_ascii=False)
            if used + len(text) > 12000:
                omitted += 1
                continue
            rows.append(copy); used += len(text)
    # 상위 analysis 결과는 직접 관측한 evidence가 아니라 가설로만 전달한다.
    hypotheses = [{'text':str(c['data'])[:1000], 'basis':'monitoring-only unverified inference'}
                  for c in collected if c['source']=='analysis']
    return json.dumps({'research_status': status, 'evidence': rows, 'analysis_hypotheses':hypotheses,
                       'omitted_evidence_count': omitted,
                       'policy': 'synthetic=true is example data, not verified production evidence. Runbook steps are recommendations, not actions performed. Missing/failed sources were not inspected.'}, ensure_ascii=False)


def evidence_report(collected):
    """Use validated facts when a source failure prevents a complete diagnosis."""
    status = next(c['data'] for c in collected if c['source']=='research_status')
    lines = ['1) 모니터링:']
    metrics = [c for c in collected if c['source']=='monitoring']
    if not metrics:
        lines.append('지표 조회에 실패하여 현재 수치와 상태를 확정할 수 없습니다.')
    for c in metrics:
        ev, data = c['evidence'][0], c['data']
        label = '합성 지표 예시' if ev['synthetic'] else '조회 지표'
        lines.append(f"{label} {ev['system']}: {data['status']}, CPU {data['cpu_percent']}%, 메모리 {data['memory_percent']}% / {ev['timestamp']} [{ev['id']}]")
    logs = [e for c in collected for e in c.get('evidence',[]) if e['source']=='synthetic_log']
    lines.append('2) 로그: 실제 운영 로그는 미조회입니다.')
    for ev in logs[:4]:
        line=ev['payload']
        lines.append(f"합성 로그 예시 {ev['system']} / {ev['timestamp']} / {line['level']}: {line['message']} [{ev['id']}]")
    lines.append('3) 대응 매뉴얼의 권고(미실행):')
    books = [c for c in collected if c['source']=='runbook']
    if not books:
        lines.append('런북 근거가 없어 대응 절차를 확정하지 않았습니다.')
    for c in books:
        ev=c['evidence'][0]
        lines.append(f"{ev['uri']} [{ev['id']}]\n" + '\n'.join(c['data'].splitlines()[:5]))
    lines.append('4) 확인 한계: ' + ('; '.join(status['warnings']) or '확인된 원인과 수행된 조치를 뜻하지 않습니다.'))
    return '\n'.join(lines)


def source_notice(collected):
    status = next(c['data'] for c in collected if c['source']=='research_status')
    return ('조사 상태: ' + status['status'] + '. 로그 자료는 합성 예시이며 실제 운영 로그는 미조회입니다. '
            '합성 지표도 실제 운영 상태로 단정할 수 없습니다. 아래 조치는 권고이며 자동 실행하지 않았습니다.\n')
