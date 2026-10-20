"""Target policy shared by the graph and all planners."""
import re
from .config import ROOT, load_config, load_known_systems


def resolve_targets(query: str) -> dict:
    known = load_known_systems()
    incident_labels = {path.stem for path in (ROOT / "data/runbooks").glob("*.md")}
    targets, unknown = [], []
    for match in re.finditer(r'(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9_.-]*', query):
        token = match.group().rstrip('.')
        if token in known:
            if token not in targets:
                targets.append(token)
        elif any(mark in token for mark in ('-', '_', '.')) and token not in incident_labels and token not in unknown:
            unknown.append(token)
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
    # Only the user's resolved systems may reach the monitoring Tool.
    monitoring = [{'id': f'task-target-{i}', 'agent': 'research', 'kind': 'monitoring',
                   'target': target, 'status': 'pending'} for i, target in enumerate(targets)]
    other = [dict(t) for t in tasks if not (t.get('agent') == 'research' and t.get('kind') == 'monitoring')]
    return monitoring + other


def empty_plan(model: str) -> tuple[dict, dict]:
    return {'needs_analysis': False, 'tasks': []}, {'model': model, 'tokens': 0, 'usd': 0.0}
