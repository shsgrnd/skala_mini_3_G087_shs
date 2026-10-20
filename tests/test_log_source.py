"""서비스 단위 테스트: 외부 계정·실모델·ignored scripts 없이 실행되어야 한다.
"""
import json
from pathlib import Path

import pytest

from agent.log_source import read_recent_logs


def test_log_source_is_read_only_bounded_and_preserves_ids():
    result = read_recent_logs('payments-service', 1)
    assert result['synthetic'] is True
    assert len(result['entries']) == 1
    assert result['entries'][0]['system'] == 'payments-service'
    assert result['entries'][0]['synthetic'] is True
    assert result['entries'][0]['evidence_id']


@pytest.mark.parametrize('system,limit', [('../monitoring',20), ('ghost-service',20), ('payments-service',0), ('payments-service',101), ('payments-service',True)])
def test_invalid_target_or_limit_is_rejected(system, limit):
    with pytest.raises(ValueError):
        read_recent_logs(system, limit)


def test_corrupt_or_cross_system_log_is_not_evidence(tmp_path, monkeypatch):
    import agent.log_source as logs
    monkeypatch.setattr(logs, 'LOG_DIR', tmp_path)
    path=tmp_path/'payments-service.jsonl'
    path.write_text(json.dumps({'system':'auth-service','timestamp':'2026-10-20T09:00:00','level':'ERROR','message':'wrong system','evidence_id':'wrong','synthetic':True}))
    with pytest.raises(ValueError):
        read_recent_logs('payments-service')
    path.write_text('{broken')
    with pytest.raises(ValueError):
        read_recent_logs('payments-service')
