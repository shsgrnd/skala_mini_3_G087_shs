import json
from pathlib import Path
import pytest
from agent.graph import _classify_incident_type


CASES = [json.loads(line) for line in (Path(__file__).parent / 'fixtures' / 'classifier_cases.jsonl').read_text().splitlines()]


@pytest.mark.parametrize('case', [pytest.param(case, id=case['id'], marks=(
    pytest.mark.xfail(strict=True, reason='Original keyword classifier falls back to db-down for Korean-only alerts')
    if case['id'].endswith('-ko') and case['expected_incident_type'] != 'db-down' else [])) for case in CASES])
def test_six_incident_types_and_korean_synonyms(case):
    assert _classify_incident_type(case['collected']) == case['expected_incident_type']
