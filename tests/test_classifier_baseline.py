"""서비스 단위 테스트: 외부 계정·실모델·ignored scripts 없이 실행되어야 한다.
원본의 알려진 실패는 XFAIL로 보존하며 새 모드의 개선 결과와 혼합하지 않는다.
"""
# 유지보수: strict XFAIL은 원본 한계를 기록한다. 새 검색 모드의 성공을 만들려고 baseline 기대값을 바꾸지 않는다.
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
