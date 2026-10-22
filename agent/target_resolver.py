"""Target policy shared by the graph and all planners.

팀 유지보수 메모:
known systems의 단일 기준은 data/monitoring.json이다. strict는 명시한 이름만 허용하고
이전 대화나 유사 이름으로 추측하지 않는다. planner 보정과 Tool 호출 직전 검증을 함께 유지해야 한다.
"""
import re
from .config import ROOT, load_config, load_known_systems


def resolve_targets(query: str) -> dict:
    known = load_known_systems()
    incident_labels = {path.stem for path in (ROOT / "data/runbooks").glob("*.md")}
    targets, unknown = [], []
    # 영문 식별자 전체를 비교해 접두사만 같은 별도 시스템을 승인하지 않는다. 한글 조사는 경계로 허용한다.
    for match in re.finditer(r'(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9_.-]*', query):
        token = match.group().rstrip('.')
        if token in known:
            if token not in targets:
                targets.append(token)
        elif any(mark in token for mark in ('-', '_', '.')) and token not in incident_labels and token not in unknown:
            unknown.append(token)
    # 알려진 이름과 미등록 이름이 섞여도 전체 요청을 보류한다. 알려진 대상만 몰래 조회하지 않는다.
    return {'status': 'unknown' if unknown else ('resolved' if targets else 'missing'),
            'targets': targets, 'unknown': unknown}


def guess_target(query: str, config=None):
    config = config or load_config()
    if config.target_resolution_mode == 'strict':
        result = resolve_targets(query)
        return result['targets'][0] if result['status'] == 'resolved' else None
    matches = [(query.index(s), s) for s in load_known_systems() if s in query]
    return min(matches)[1] if matches else config.legacy_target


def guess_targets(query: str, config=None) -> list[str]:
    config = config or load_config()
    if config.target_resolution_mode == 'strict':
        result = resolve_targets(query)
        return result['targets'] if result['status'] == 'resolved' else []
    return sorted(s for s in load_known_systems() if s in query) or [config.legacy_target]


def guard_monitoring_tasks(tasks: list[dict], targets: list[str]) -> list[dict]:
    # 모델이 제시한 monitoring 목록을 필터링하는 대신 사용자 승인 대상 전체로 재생성해 누락도 복구한다.
    # analysis 등 다른 종류의 작업은 유지하며, monitoring/logs 실행 직전 검증은 graph.py가 담당한다.
    monitoring = [{'id': f'task-target-{i}', 'agent': 'research', 'kind': 'monitoring',
                   'target': target, 'status': 'pending'} for i, target in enumerate(targets)]
    other = [dict(t) for t in tasks if not (t.get('agent') == 'research' and t.get('kind') == 'monitoring')]
    return monitoring + other


def empty_plan(model: str) -> tuple[dict, dict]:
    return {'needs_analysis': False, 'tasks': []}, {'model': model, 'tokens': 0, 'usd': 0.0}
