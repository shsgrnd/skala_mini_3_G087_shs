"""Validated, process-local settings; no secrets are included in public metadata."""
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / 'config' / 'agent.json'
MONITORING_PATH = ROOT / 'data' / 'monitoring.json'


@dataclass(frozen=True)
class RAGConfig:
    model: str = 'intfloat/multilingual-e5-small'
    revision: str = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
    dimension: int = 384
    top_k: int = 3
    threshold: float = 0.8
    margin: float = 0.01
    index_path: Path = ROOT / 'data' / 'chroma' / 'runbooks'


@dataclass(frozen=True)
class AgentConfig:
    target_resolution_mode: str
    legacy_target: str
    target_required_pattern: str
    prompts: dict[str, str]
    sha256: str
    incident_classifier: str = 'keyword'
    rag: RAGConfig = field(default_factory=RAGConfig)


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
                 'plan_strict', 'analyze', 'chat', 'write', 'missing_target', 'unknown_target',
                 'retrieval_failed', 'retrieval_insufficient'):
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
    classifier = os.environ.get('INCIDENT_CLASSIFIER', 'keyword')
    if classifier not in ('keyword', 'rag'):
        raise ValueError('INCIDENT_CLASSIFIER must be keyword or rag')
    raw = data.get('rag', {})
    if not isinstance(raw, dict):
        raise ValueError('rag must be a JSON object')
    try:
        rag = RAGConfig(**raw)
    except TypeError as exc:
        raise ValueError('Invalid rag settings') from exc
    if (not isinstance(rag.model, str) or not rag.model.strip()
            or not isinstance(rag.revision, str) or not re.fullmatch(r'[0-9a-f]{40}', rag.revision)
            or not isinstance(rag.index_path, (str, Path)) or not str(rag.index_path).strip()
            or type(rag.dimension) is not int or rag.dimension < 1
            or type(rag.top_k) is not int or not 2 <= rag.top_k <= 6
            or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                   for v in (rag.threshold, rag.margin))):
        raise ValueError('Invalid rag model, dimension, top_k, threshold or margin')
    index_path = Path(os.environ.get('RAG_INDEX_PATH', str(rag.index_path)))
    rag = replace(rag, index_path=index_path if index_path.is_absolute() else ROOT / index_path)
    return AgentConfig(mode, fallback, pattern, prompts, hashlib.sha256(content).hexdigest(), classifier, rag)


def load_known_systems() -> tuple[str, ...]:
    try:
        data = json.loads(MONITORING_PATH.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('Cannot load known systems from data/monitoring.json') from exc
    if not isinstance(data, dict) or not data or any(not isinstance(k, str) or not k for k in data):
        raise ValueError('data/monitoring.json must contain named systems')
    return tuple(data)
