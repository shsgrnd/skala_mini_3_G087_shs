"""서비스 단위 테스트: 외부 계정·실모델·ignored scripts 없이 실행되어야 한다.
"""
# 유지보수: 설정 확장 시 정상값뿐 아니라 시작 시 거부할 값도 추가한다. monkeypatch로 환경을 원복한다.
import json
import pytest


def test_mode_defaults_to_legacy_and_invalid_mode_fails(monkeypatch):
    from agent.config import load_config
    monkeypatch.delenv('TARGET_RESOLUTION_MODE', raising=False)
    assert load_config().target_resolution_mode == 'legacy'
    monkeypatch.setenv('TARGET_RESOLUTION_MODE', 'typo')
    with pytest.raises(ValueError, match='TARGET_RESOLUTION_MODE'):
        load_config()


def test_external_prompt_override_and_bad_config_fail(tmp_path, monkeypatch):
    from agent.config import load_config, DEFAULT_CONFIG_PATH
    data = json.loads(DEFAULT_CONFIG_PATH.read_text())
    data['prompts']['chat'] = 'unit-test assistant'
    custom = tmp_path / 'agent.json'
    custom.write_text(json.dumps(data))
    monkeypatch.setenv('AGENT_CONFIG_PATH', str(custom))
    assert load_config().prompts['chat'] == 'unit-test assistant'
    data['prompts']['write'] = ''
    custom.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='write'):
        load_config()


def test_invalid_regex_fails_at_startup(tmp_path, monkeypatch):
    from agent.config import load_config, DEFAULT_CONFIG_PATH
    data = json.loads(DEFAULT_CONFIG_PATH.read_text())
    data['target_required_pattern'] = '['
    custom = tmp_path / 'agent.json'
    custom.write_text(json.dumps(data))
    monkeypatch.setenv('AGENT_CONFIG_PATH', str(custom))
    with pytest.raises(ValueError, match='target_required_pattern'):
        load_config()
