"""Validated, process-local settings; no secrets are included in public metadata."""
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / 'config' / 'agent.json'
MONITORING_PATH = ROOT / 'data' / 'monitoring.json'


@dataclass(frozen=True)
class AgentConfig:
    target_resolution_mode: str
    legacy_target: str
    target_required_pattern: str
    prompts: dict[str, str]
    sha256: str


def load_config() -> AgentConfig:
    mode = os.environ.get('TARGET_RESOLUTION_MODE', 'legacy')
    if mode not in ('legacy', 'strict'):
        raise ValueError('TARGET_RESOLUTION_MODE must be legacy or strict')
    path = Path(os.environ.get('AGENT_CONFIG_PATH', str(DEFAULT_CONFIG_PATH)))
    try:
        content = path.read_bytes()
        data = json.loads(content)
    except (OSError, ValueError) as exc:
        raise ValueError(f'Cannot load agent config: {path}') from exc
    if not isinstance(data, dict):
        raise ValueError('Agent config must be a JSON object')
    prompts = data.get('prompts')
    for name in ('route_ollama', 'route_openai', 'plan_ollama', 'plan_openai',
                 'plan_strict', 'analyze', 'chat', 'write', 'missing_target', 'unknown_target'):
        if not isinstance(prompts, dict) or not isinstance(prompts.get(name), str) or not prompts[name].strip():
            raise ValueError(f'Missing or empty prompt: {name}')
    pattern = data.get('target_required_pattern')
    try:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError('Empty target_required_pattern')
        re.compile(pattern)
    except re.error as exc:
        raise ValueError('Invalid target_required_pattern') from exc
    fallback = data.get('legacy_target')
    if not isinstance(fallback, str) or fallback not in load_known_systems():
        raise ValueError('legacy_target must exist in data/monitoring.json')
    return AgentConfig(mode, fallback, pattern, prompts, hashlib.sha256(content).hexdigest())


def load_known_systems() -> tuple[str, ...]:
    try:
        data = json.loads(MONITORING_PATH.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('Cannot load known systems from data/monitoring.json') from exc
    if not isinstance(data, dict) or not data or any(not isinstance(k, str) or not k for k in data):
        raise ValueError('data/monitoring.json must contain named systems')
    return tuple(data)
